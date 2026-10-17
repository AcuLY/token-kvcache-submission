"""FastAPI surface for the coordinator control plane and WebUI."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Body, FastAPI, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter, ValidationError

from coordinator.errors import CoordinatorError, ModelServiceError
from coordinator.journal import public_event
from coordinator.model_connection import ConnectionRequest, local_interfaces, prepare_connection
from coordinator.schemas import (
    AgentErrorReport,
    ConfigUpdate,
    EnvironmentDocument,
    MetricReport,
    MockConfig,
    TransferCommitAck,
)
from coordinator.service import CoordinatorService
from shared.models import (
    AgentErrorReport as SharedAgentErrorReport,
)
from shared.models import (
    AgentRegistration,
    CommitAck,
    Heartbeat,
    ModelBehaviorConfig,
    SessionConfig,
    StateOffer,
)


def _positive_env(name: str, default: int) -> int:
    try:
        parsed = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _event_json(event: Any) -> dict[str, Any]:
    return public_event(event)


def _config_document_json(document: Any) -> dict[str, Any]:
    config = document.config.model_dump(mode="json")
    config["version"] = document.version
    return {"version": document.version, "config": config}


def _expected_version(if_match: str | None, query_version: int | None) -> int | None:
    if query_version is not None:
        return query_version
    if if_match is None:
        return None
    candidate = if_match.strip().strip("W/").strip('"')
    try:
        return int(candidate)
    except ValueError as exc:
        raise CoordinatorError("If-Match must contain an integer configuration version") from exc


def _parse_ack(body: dict[str, Any]) -> TransferCommitAck:
    try:
        if "receiver_agent" in body:
            return TransferCommitAck.model_validate(body)
        shared = CommitAck.model_validate(body)
        return TransferCommitAck(
            transfer_id=shared.transfer_id,
            conversation_id=shared.conversation_id,
            receiver_agent=shared.target_agent,
            state_id=shared.state_id,
            turn_id=shared.turn_id,
            token_count=shared.token_count,
            metrics=shared.metrics.model_dump(mode="python"),
        )
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def _parse_error_report(body: dict[str, Any]) -> AgentErrorReport:
    try:
        if "phase" in body or "message" in body:
            return AgentErrorReport.model_validate(body)
        shared = SharedAgentErrorReport.model_validate(body)
        return AgentErrorReport(
            conversation_id=shared.conversation_id,
            agent_id=shared.agent_id,
            phase=shared.code,
            message=shared.detail,
            code=shared.code,
            turn_id=shared.turn_id,
            transfer_id=shared.transfer_id,
        )
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def create_app(
    service: CoordinatorService | None = None,
    *,
    frontend_dist: str | Path | None = None,
) -> FastAPI:
    owns_service = service is None
    coordinator = service or CoordinatorService(
        heartbeat_timeout_seconds=float(os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "6")),
        event_retention=_positive_env("EVENT_RETENTION", 10_000),
        model_service_url=os.getenv("MODEL_SERVICE_URL", "http://127.0.0.1:8100"),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_service:
            await coordinator.close()

    application = FastAPI(
        title="Token KV Cache Coordinator",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.coordinator = coordinator

    @application.exception_handler(CoordinatorError)
    async def coordinator_error_handler(_: Request, exc: CoordinatorError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {"code": exc.code, "message": exc.message},
                "code": exc.code,
                "message": exc.message,
                "detail": exc.message,
            },
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = jsonable_encoder(exc.errors())
        return JSONResponse(
            status_code=422,
            content={
                "error": {"code": "validation_error", "details": details},
                "code": "validation_error",
                "detail": details,
            },
        )

    @application.get("/health")
    async def health() -> dict[str, Any]:
        agents = await coordinator.registry.list()
        return {
            "status": "ok",
            "registered_agents": len(agents),
            "ready_agents": sum(agent.ready for agent in agents),
            "latest_seq": coordinator.journal.latest_seq,
        }

    @application.get("/api/v1/bootstrap")
    async def bootstrap() -> dict[str, Any]:
        return await coordinator.bootstrap()

    @application.get("/api/v1/capabilities")
    async def capabilities() -> dict[str, Any]:
        return coordinator.capabilities().model_dump(mode="json")

    @application.get("/api/v1/model-connection")
    async def model_connection() -> dict[str, Any]:
        return await coordinator.model_connection_status()

    @application.get("/api/v1/model-connection/interfaces")
    async def model_interfaces() -> list[dict[str, Any]]:
        return await asyncio.to_thread(local_interfaces)

    @application.post("/api/v1/model-connection/test")
    async def test_model_connection(config: ConnectionRequest) -> dict[str, Any]:
        try:
            candidate = await prepare_connection(config)
            try:
                return candidate.descriptor.model_dump(mode="json")
            finally:
                await candidate.close()
        except (OSError, ValueError, httpx.HTTPError, TimeoutError) as exc:
            raise ModelServiceError(str(exc)) from exc

    @application.put("/api/v1/model-connection")
    async def set_model_connection(config: ConnectionRequest) -> dict[str, Any]:
        return await coordinator.connect_model(config)

    @application.get("/api/v1/config")
    async def get_config() -> dict[str, Any]:
        return _config_document_json(await coordinator.get_config())

    @application.put("/api/v1/config")
    async def put_config(
        body: Annotated[dict[str, Any], Body()],
        expected_version: Annotated[int | None, Query(ge=1)] = None,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> dict[str, Any]:
        expected: int | None
        try:
            if "config" in body:
                update = ConfigUpdate.model_validate(body)
                config = update.config
                expected = update.expected_version
            else:
                config_body = dict(body)
                embedded_version = config_body.pop("version", None)
                config = TypeAdapter(SessionConfig).validate_python(config_body)
                expected = _expected_version(if_match, expected_version)
                if expected is None and isinstance(embedded_version, int):
                    expected = embedded_version
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        result = await coordinator.update_config(config, expected)
        return _config_document_json(result)

    @application.get("/api/v1/agents")
    async def agents() -> list[dict[str, Any]]:
        return [agent.model_dump(mode="json") for agent in await coordinator.registry.list()]

    @application.get("/api/v1/sessions")
    async def sessions() -> list[dict[str, Any]]:
        return [session.model_dump(mode="json") for session in await coordinator.list_sessions()]

    @application.post("/api/v1/sessions", status_code=201)
    async def create_session(
        body: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict[str, Any]:
        config: SessionConfig | None = None
        if body is not None:
            raw_config = body.get("config")
            if raw_config is not None:
                if not isinstance(raw_config, dict):
                    raise CoordinatorError("session config must be an object")
                config_body = dict(raw_config)
                config_body.pop("version", None)
                try:
                    config = TypeAdapter(SessionConfig).validate_python(config_body)
                except ValidationError as exc:
                    raise RequestValidationError(exc.errors()) from exc
        return (await coordinator.create_session(config)).model_dump(mode="json")

    @application.get("/api/v1/sessions/{conversation_id}")
    async def session_detail(conversation_id: str) -> dict[str, Any]:
        return (await coordinator.get_session(conversation_id)).model_dump(mode="json")

    @application.post("/api/v1/sessions/{conversation_id}/pause")
    async def pause(conversation_id: str) -> dict[str, Any]:
        return (await coordinator.pause_session(conversation_id)).model_dump(mode="json")

    @application.post("/api/v1/sessions/{conversation_id}/resume")
    async def resume(conversation_id: str) -> dict[str, Any]:
        return (await coordinator.resume_session(conversation_id)).model_dump(mode="json")

    @application.post("/api/v1/sessions/{conversation_id}/stop")
    async def stop(conversation_id: str) -> dict[str, Any]:
        return (await coordinator.stop_session(conversation_id)).model_dump(mode="json")

    @application.get("/api/v1/environment")
    async def environment() -> dict[str, Any]:
        return (await coordinator.environment()).model_dump(mode="json")

    @application.put("/api/v1/environment")
    async def update_environment(document: EnvironmentDocument) -> dict[str, Any]:
        return (await coordinator.update_environment(document)).model_dump(mode="json")

    @application.get("/api/v1/mock")
    async def mock_config() -> dict[str, Any]:
        return (await coordinator.mock_config()).model_dump(mode="json")

    @application.put("/api/v1/mock")
    async def update_mock_config(document: MockConfig) -> dict[str, Any]:
        return (await coordinator.update_mock_config(document)).model_dump(mode="json")

    @application.get("/api/v1/mock/model-behavior")
    async def model_behavior() -> dict[str, Any]:
        return (await coordinator.model_behavior()).model_dump(mode="json")

    @application.put("/api/v1/mock/model-behavior")
    async def update_model_behavior(behavior: ModelBehaviorConfig) -> dict[str, Any]:
        return (await coordinator.update_model_behavior(behavior)).model_dump(mode="json")

    @application.get("/api/v1/experiments.csv")
    async def export_experiments() -> PlainTextResponse:
        return PlainTextResponse(
            await coordinator.experiments_csv(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="experiments.csv"'},
        )

    @application.post("/api/v1/internal/agents/register", status_code=201)
    async def register_agent(registration: AgentRegistration) -> dict[str, Any]:
        return await coordinator.register_agent(registration)

    @application.post("/api/v1/internal/agents/heartbeat")
    async def heartbeat_agent(heartbeat: Heartbeat) -> dict[str, Any]:
        return await coordinator.heartbeat_agent(heartbeat)

    @application.post("/api/v1/internal/agents/{agent_id}/heartbeat")
    async def heartbeat_agent_alias(agent_id: str, heartbeat: Heartbeat) -> dict[str, Any]:
        if heartbeat.agent_id != agent_id:
            raise CoordinatorError("heartbeat path and body agent_id do not match")
        return await coordinator.heartbeat_agent(heartbeat)

    @application.post("/api/v1/internal/state-offers", status_code=202)
    async def state_offer(offer: StateOffer) -> dict[str, Any]:
        plan = await coordinator.accept_state_offer(offer)
        return {
            "status": "completed" if plan is None else "planned",
            "plan": plan.model_dump(mode="json") if plan else None,
        }

    @application.post("/api/v1/internal/transfers/{transfer_id}/ack")
    async def transfer_ack(
        transfer_id: str, body: Annotated[dict[str, Any], Body()]
    ) -> dict[str, Any]:
        ack = _parse_ack(body)
        if transfer_id != ack.transfer_id:
            raise CoordinatorError("transfer path and body transfer_id do not match")
        return (await coordinator.commit_transfer(ack)).model_dump(mode="json")

    @application.post("/api/v1/internal/transfer-acks")
    async def transfer_ack_alias(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        ack = _parse_ack(body)
        return (await coordinator.commit_transfer(ack)).model_dump(mode="json")

    @application.post("/api/v1/internal/metrics", status_code=202)
    async def record_metric(report: MetricReport) -> dict[str, Any]:
        return (await coordinator.record_metric(report)).model_dump(mode="json")

    @application.post("/api/v1/internal/errors", status_code=202)
    async def record_error(body: Annotated[dict[str, Any], Body()]) -> dict[str, str]:
        report = _parse_error_report(body)
        return await coordinator.record_error(report)

    @application.websocket("/api/v1/observations")
    async def observations(websocket: WebSocket, after_seq: int = 0) -> None:
        await websocket.accept()
        subscription = await coordinator.journal.subscribe(after_seq)
        if subscription.resync_required:
            await websocket.send_json(
                {
                    "type": "resync_required",
                    "earliest_seq": subscription.earliest_seq,
                    "latest_seq": subscription.latest_seq,
                }
            )
            await websocket.close(code=4409, reason="bootstrap required")
            return
        try:
            await websocket.send_json(
                {
                    "events": [_event_json(event) for event in subscription.replay],
                    "latest_seq": subscription.latest_seq,
                }
            )
            while True:
                event = await subscription.queue.get()
                await websocket.send_json(_event_json(event))
        except WebSocketDisconnect:
            pass
        finally:
            await coordinator.journal.unsubscribe(subscription.queue)

    dist_value = frontend_dist or os.getenv("FRONTEND_DIST")
    dist = Path(dist_value).resolve() if dist_value else None
    if dist and (dist / "assets").is_dir():
        application.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @application.get("/")
    async def root() -> Any:
        if dist and (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        return {
            "service": "token-kvcache-coordinator",
            "api": "/api/v1/bootstrap",
            "health": "/health",
        }

    @application.get("/{full_path:path}", include_in_schema=False)
    async def frontend_fallback(full_path: str) -> Any:
        if full_path.startswith(("api/", "health")):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        if dist and (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        return JSONResponse(status_code=404, content={"detail": "WebUI is not built"})

    return application


app = create_app()
