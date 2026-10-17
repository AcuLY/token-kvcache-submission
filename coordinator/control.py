"""HTTP client for coordinator-to-Agent control commands."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from coordinator.errors import AgentControlError
from coordinator.journal import contains_token_ids
from coordinator.schemas import AgentCommand
from shared.kv import CacheInventory


class AgentControlClient(Protocol):
    async def cache_status(self, control_url: str, conversation_id: str) -> CacheInventory: ...

    async def send(self, control_url: str, command: AgentCommand) -> None: ...

    async def close(self) -> None: ...


class HttpAgentControlClient:
    def __init__(self, client: httpx.AsyncClient | None = None, timeout: float = 10.0) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def cache_status(self, control_url: str, conversation_id: str) -> CacheInventory:
        try:
            response = await self._client.get(
                f"{control_url.rstrip('/')}/internal/v1/cache-status/{conversation_id}"
            )
            response.raise_for_status()
            return CacheInventory.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise AgentControlError(f"CACHE_STATUS failed: {exc}") from exc

    async def send(self, control_url: str, command: AgentCommand) -> None:
        body: dict[str, Any] = command.model_dump(mode="json")
        if contains_token_ids(body):
            raise AgentControlError("agent control command must not contain token_ids")
        endpoint = f"{control_url.rstrip('/')}/internal/v1/commands"
        try:
            response = await self._client.post(endpoint, json=body, timeout=45.0)
            if response.status_code not in {200, 201, 202, 204} or (
                response.content and response.json().get("accepted") is False
            ):
                raise AgentControlError(
                    f"agent command {command.type} failed with HTTP {response.status_code}"
                )
        except httpx.HTTPError as exc:
            raise AgentControlError(f"agent command {command.type} failed: {exc}") from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
