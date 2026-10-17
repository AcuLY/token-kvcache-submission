"""In-memory Agent registration and liveness tracking."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from coordinator.errors import ConflictError, NotFoundError
from coordinator.schemas import RegisteredAgentView, utc_now
from shared.models import AgentRegistration, Heartbeat


@dataclass(slots=True)
class AgentRecord:
    registration: AgentRegistration
    last_heartbeat_at: datetime
    status: str
    conversation_id: str | None = None
    phase: str | None = None


class AgentRegistry:
    def __init__(self, heartbeat_timeout_seconds: float = 6.0) -> None:
        if heartbeat_timeout_seconds <= 0:
            raise ValueError("heartbeat_timeout_seconds must be positive")
        self._heartbeat_timeout = timedelta(seconds=heartbeat_timeout_seconds)
        self._records: dict[str, AgentRecord] = {}
        self._lock = asyncio.Lock()

    async def register(self, registration: AgentRegistration) -> RegisteredAgentView:
        now = utc_now()
        async with self._lock:
            current = self._records.get(registration.agent_id)
            if current and current.registration.instance_id != registration.instance_id:
                if self._is_online(current, now):
                    raise ConflictError(
                        f"agent '{registration.agent_id}' already has a live instance"
                    )
            record = AgentRecord(
                registration=registration,
                last_heartbeat_at=now,
                status=str(getattr(registration.status, "value", registration.status)),
            )
            self._records[registration.agent_id] = record
            return self._view(record, now)

    async def heartbeat(self, heartbeat: Heartbeat) -> RegisteredAgentView:
        now = utc_now()
        async with self._lock:
            record = self._records.get(heartbeat.agent_id)
            if record is None:
                raise NotFoundError(f"agent '{heartbeat.agent_id}' is not registered")
            if record.registration.instance_id != heartbeat.instance_id:
                raise ConflictError("heartbeat instance_id does not match the registered instance")
            record.last_heartbeat_at = now
            record.status = str(getattr(heartbeat.status, "value", heartbeat.status))
            record.conversation_id = heartbeat.conversation_id
            record.phase = heartbeat.phase
            return self._view(record, now)

    async def get(self, agent_id: str) -> AgentRecord:
        async with self._lock:
            record = self._records.get(agent_id)
            if record is None:
                raise NotFoundError(f"agent '{agent_id}' is not registered")
            return record

    async def require_ready(self, agent_ids: list[str]) -> list[AgentRecord]:
        now = utc_now()
        async with self._lock:
            records: list[AgentRecord] = []
            for agent_id in agent_ids:
                record = self._records.get(agent_id)
                if record is None:
                    raise ConflictError(f"agent '{agent_id}' is not registered")
                if not self._is_ready(record, now):
                    raise ConflictError(f"agent '{agent_id}' is not ready")
                records.append(record)
            return records

    async def list(self) -> list[RegisteredAgentView]:
        now = utc_now()
        async with self._lock:
            return [self._view(record, now) for _, record in sorted(self._records.items())]

    def _is_online(self, record: AgentRecord, now: datetime) -> bool:
        return now - record.last_heartbeat_at <= self._heartbeat_timeout

    def _is_ready(self, record: AgentRecord, now: datetime) -> bool:
        status = record.status.upper().split(".")[-1]
        return self._is_online(record, now) and status == "READY"

    def _view(self, record: AgentRecord, now: datetime) -> RegisteredAgentView:
        registration = record.registration
        runtime = registration.runtime_descriptor.model_dump(mode="json")
        capabilities: Any = registration.transport_capabilities
        if isinstance(capabilities, set):
            capabilities = sorted(capabilities)
        return RegisteredAgentView(
            agent_id=registration.agent_id,
            instance_id=registration.instance_id,
            control_url=str(registration.control_url),
            data_bind=registration.data_bind,
            data_advertise=registration.data_advertise,
            runtime_descriptor=runtime,
            transport_capabilities=list(capabilities),
            status=record.status,
            online=self._is_online(record, now),
            ready=self._is_ready(record, now),
            registered_at=registration.registered_at,
            last_heartbeat_at=record.last_heartbeat_at,
            conversation_id=record.conversation_id,
            phase=record.phase,
        )
