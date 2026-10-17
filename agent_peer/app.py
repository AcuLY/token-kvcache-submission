"""FastAPI control surface for an independent Agent peer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .config import AgentSettings
from .runtime import AgentRuntime


class IncomingAgentCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    payload: dict[str, Any]


def create_app(
    runtime: AgentRuntime | None = None,
    *,
    settings: AgentSettings | None = None,
) -> FastAPI:
    peer = runtime or AgentRuntime(settings or AgentSettings())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await peer.start()
        try:
            yield
        finally:
            await peer.stop()

    application = FastAPI(
        title=f"Token KVCache Agent {peer.settings.agent_id}",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.runtime = peer

    @application.get("/internal/v1/cache-status/{conversation_id}")
    async def cache_status(conversation_id: str) -> dict[str, Any]:
        return (await peer.cache_status(conversation_id)).model_dump(mode="json")

    @application.get("/health")
    async def health() -> dict[str, Any]:
        if not peer.registered:
            raise HTTPException(status_code=503, detail="Agent is not registered")
        return {
            "status": "ok",
            "agent_id": peer.settings.agent_id,
            "instance_id": peer.settings.instance_id,
            "registered": peer.registered,
            "phase": peer.phase,
            "sessions": len(peer.sessions),
            "data_endpoint": peer.settings.data_advertise,
        }

    @application.post("/internal/v1/commands", status_code=202)
    async def command(body: IncomingAgentCommand) -> dict[str, Any]:
        try:
            result = await peer.enqueue_command(body.command_id, body.type, body.payload)
            if not result.get("accepted", False):
                raise HTTPException(status_code=409, detail=result.get("detail"))
            return result
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.get("/internal/v1/commands/{command_id}")
    async def command_status(command_id: str) -> dict[str, Any]:
        result = peer.command_results.get(command_id)
        if result is None:
            raise HTTPException(status_code=404, detail="command not found")
        return result.copy()

    return application


app = create_app()


__all__ = ["app", "create_app"]
