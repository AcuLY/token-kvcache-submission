"""Control-plane client used by an Agent peer."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from shared.contracts import AgentErrorReport, AgentRegistration, Heartbeat, StateOffer


class CoordinatorClientError(RuntimeError):
    pass


class CoordinatorClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            trust_env=False,
            timeout=timeout_seconds,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def register(self, registration: AgentRegistration) -> dict[str, Any]:
        return await self._post("/api/v1/internal/agents/register", registration)

    async def heartbeat(self, heartbeat: Heartbeat) -> dict[str, Any]:
        return await self._post("/api/v1/internal/agents/heartbeat", heartbeat)

    async def offer(self, offer: StateOffer) -> dict[str, Any]:
        return await self._post("/api/v1/internal/state-offers", offer)

    async def commit_transfer(
        self,
        transfer_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._post_json(
            f"/api/v1/internal/transfers/{transfer_id}/ack",
            body,
        )

    async def metric(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json("/api/v1/internal/metrics", body)

    async def error(self, report: AgentErrorReport, *, phase: str) -> dict[str, Any]:
        body = {
            "conversation_id": report.conversation_id,
            "agent_id": report.agent_id,
            "phase": phase,
            "message": f"{report.code}: {report.detail}",
            "turn_id": report.turn_id,
            "transfer_id": report.transfer_id,
        }
        return await self._post_json("/api/v1/internal/errors", body)

    async def _post(self, path: str, body: BaseModel) -> dict[str, Any]:
        return await self._post_json(path, body.model_dump(mode="json"))

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=body)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {"data": payload}
        except (httpx.HTTPError, ValueError) as exc:
            raise CoordinatorClientError(f"coordinator POST {path} failed: {exc}") from exc


__all__ = ["CoordinatorClient", "CoordinatorClientError"]
