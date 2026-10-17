"""Application-owned connections to existing model services."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.contracts import RuntimeDescriptor


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["http", "ssh"] = "http"
    url: str = "http://127.0.0.1:18081"
    ssh_host: str = "token-kv"
    remote_port: int = Field(default=18100, ge=1, le=65535)
    source_address: str = ""
    interface_index: int = Field(default=0, ge=0)

    @field_validator("ssh_host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        if not value or value.startswith("-") or any(c.isspace() for c in value):
            raise ValueError("SSH 主机必须是配置别名或 user@host")
        return value

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = httpx.URL(value)
        if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.userinfo:
            raise ValueError("请输入不含账号密码的 HTTP 模型服务地址")
        return value.rstrip("/")

    @field_validator("source_address")
    @classmethod
    def valid_source(cls, value: str) -> str:
        if value:
            ipaddress.IPv4Address(value)
        return value


@dataclass
class Connection:
    url: str
    config: ConnectionRequest
    descriptor: RuntimeDescriptor
    process: asyncio.subprocess.Process | None = None

    async def close(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()


async def describe_url(url: str) -> RuntimeDescriptor:
    async with httpx.AsyncClient(trust_env=False, timeout=8) as client:
        response = await client.get(f"{url}/v1/descriptor")
        response.raise_for_status()
        descriptor = RuntimeDescriptor.model_validate(response.json())
        if "shared_context_v2" not in descriptor.capabilities:
            raise ValueError("目标模型不支持共享上下文协议")
        return descriptor


RETRY_DELAYS = (5, 15, 30)


def retryable(exc: Exception) -> bool:
    message = str(exc).lower()
    if any(
        word in message
        for word in (
            "permission denied",
            "host key",
            "host identification",
            "bad configuration",
            "could not resolve hostname",
            "no such identity",
        )
    ):
        return False
    return isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError)) or any(
        word in message
        for word in (
            "connection refused",
            "connection closed",
            "connection reset",
            "timed out",
            "timeout",
            "kex_exchange_identification",
            "banner exchange",
        )
    )


async def prepare_connection(
    config: ConnectionRequest,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> Connection:
    delays = RETRY_DELAYS if config.mode == "ssh" else ()
    for attempt in range(len(delays) + 1):
        state = {
            "attempt": attempt + 1,
            "max_attempts": len(delays) + 1,
            "next_retry_at": None,
            "error": None,
        }
        if progress:
            progress(state)
        try:
            return await _prepare_once(config)
        except Exception as exc:
            if attempt == len(delays) or not retryable(exc):
                raise
            if progress:
                progress(
                    {**state, "next_retry_at": time.time() + delays[attempt], "error": str(exc)}
                )
            await asyncio.sleep(delays[attempt])
    raise AssertionError("unreachable")


async def _prepare_once(config: ConnectionRequest) -> Connection:
    process = None
    url = config.url
    if config.mode == "ssh":
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        command = [
            "ssh",
            "-N",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ConnectTimeout=12",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if config.source_address:
            if os.name != "nt" or not config.interface_index:
                raise ValueError("指定网卡直连需要 Windows 网卡地址和接口编号")
            proxy = subprocess.list2cmdline(
                [
                    sys.executable,
                    str(Path(__file__).with_name("ssh_proxy.py")),
                    config.source_address,
                    str(config.interface_index),
                    "%h",
                    "%p",
                ]
            )
            command += ["-o", f"ProxyCommand={proxy}"]
        command += ["-L", f"127.0.0.1:{port}:127.0.0.1:{config.remote_port}", config.ssh_host]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        url = f"http://127.0.0.1:{port}"
    try:
        deadline = asyncio.get_running_loop().time() + 25
        for attempt in range(50 if process else 1):
            if process and process.returncode is not None:
                error = await process.stderr.read() if process.stderr else b""
                raise ValueError(f"SSH 连接失败：{error.decode(errors='replace')[-1200:]}")
            try:
                async with asyncio.timeout(max(0.1, deadline - asyncio.get_running_loop().time())):
                    descriptor = await describe_url(url)
                return Connection(url, config, descriptor, process)
            except httpx.TransportError:
                if not process or attempt == 49 or asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(0.5)
        raise ValueError("模型服务连接超时")
    except BaseException:
        if process and process.returncode is None:
            process.terminate()
            await process.wait()
        raise


def local_interfaces() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' "
            "-and $_.IPAddress -notlike '169.254.*' } | "
            "Select-Object InterfaceAlias,InterfaceIndex,IPAddress "
            "| ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode or not result.stdout.strip():
        return []
    data = json.loads(result.stdout)
    return data if isinstance(data, list) else [data]
