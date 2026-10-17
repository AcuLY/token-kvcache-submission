"""Wire codec for TOKEN/FULL peer-to-peer snapshots."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Iterable
from typing import Any

from .contracts import TokenTransferManifest

MAX_MANIFEST_BYTES = 1024 * 1024
_UINT32_MAX = 2**32 - 1


def encode_token_payload(token_ids: Iterable[int]) -> bytes:
    tokens = list(token_ids)
    if any(not isinstance(token, int) or isinstance(token, bool) for token in tokens):
        raise TypeError("token IDs must be integers")
    if any(token < 0 or token > _UINT32_MAX for token in tokens):
        raise ValueError("token IDs must fit an unsigned 32-bit integer")
    if not tokens:
        return b""
    return struct.pack(f"!{len(tokens)}I", *tokens)


def decode_token_payload(payload: bytes, *, expected_count: int | None = None) -> list[int]:
    if len(payload) % 4:
        raise ValueError("token payload length must be divisible by four")
    count = len(payload) // 4
    if expected_count is not None and count != expected_count:
        raise ValueError(f"expected {expected_count} token IDs, received {count}")
    if not payload:
        return []
    return list(struct.unpack(f"!{count}I", payload))


def _manifest_bytes(manifest: TokenTransferManifest) -> bytes:
    data = manifest.model_dump(mode="json")
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds maximum supported size")
    return encoded


def encode_token_transfer(
    manifest: TokenTransferManifest, token_ids: Iterable[int]
) -> bytes:
    payload = encode_token_payload(token_ids)
    if len(payload) != manifest.payload_bytes:
        raise ValueError("encoded payload length does not match manifest.payload_bytes")
    if len(payload) // 4 != manifest.token_count:
        raise ValueError("encoded token count does not match manifest.token_count")
    manifest_bytes = _manifest_bytes(manifest)
    return struct.pack("!I", len(manifest_bytes)) + manifest_bytes + payload


def decode_token_transfer(data: bytes) -> tuple[TokenTransferManifest, list[int]]:
    if len(data) < 4:
        raise ValueError("transfer frame is missing the manifest length prefix")
    manifest_size = struct.unpack("!I", data[:4])[0]
    if manifest_size > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds maximum supported size")
    payload_start = 4 + manifest_size
    if len(data) < payload_start:
        raise ValueError("transfer frame contains a truncated manifest")
    try:
        raw: Any = json.loads(data[4:payload_start].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("transfer frame contains invalid manifest JSON") from exc
    manifest = TokenTransferManifest.model_validate(raw)
    payload = data[payload_start:]
    if len(payload) != manifest.payload_bytes:
        raise ValueError("transfer frame payload length does not match manifest")
    return manifest, decode_token_payload(payload, expected_count=manifest.token_count)


async def write_token_transfer(
    writer: asyncio.StreamWriter,
    manifest: TokenTransferManifest,
    token_ids: Iterable[int],
) -> int:
    frame = encode_token_transfer(manifest, token_ids)
    writer.write(frame)
    await writer.drain()
    return len(frame)


async def read_token_transfer(
    reader: asyncio.StreamReader,
) -> tuple[TokenTransferManifest, list[int]]:
    try:
        length_bytes = await reader.readexactly(4)
    except asyncio.IncompleteReadError as exc:
        raise ValueError("connection closed before manifest length") from exc
    manifest_size = struct.unpack("!I", length_bytes)[0]
    if manifest_size > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds maximum supported size")
    try:
        manifest_bytes = await reader.readexactly(manifest_size)
    except asyncio.IncompleteReadError as exc:
        raise ValueError("connection closed during manifest") from exc
    try:
        manifest = TokenTransferManifest.model_validate_json(manifest_bytes)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("connection sent an invalid transfer manifest") from exc
    try:
        payload = await reader.readexactly(manifest.payload_bytes)
    except asyncio.IncompleteReadError as exc:
        raise ValueError("connection closed during token payload") from exc
    return manifest, decode_token_payload(payload, expected_count=manifest.token_count)


__all__ = [
    "MAX_MANIFEST_BYTES",
    "decode_token_payload",
    "decode_token_transfer",
    "encode_token_payload",
    "encode_token_transfer",
    "read_token_transfer",
    "write_token_transfer",
]

