"""Bounded, replayable observation event journal."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any

from coordinator.schemas import utc_now
from shared.models import ObservationEvent


def contains_token_ids(value: Any) -> bool:
    """Return true when a control-plane object contains a token payload key."""

    if isinstance(value, dict):
        return any(
            str(key).lower() in {"token_ids", "tokenids"} or contains_token_ids(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_token_ids(child) for child in value)
    return False


def public_event(event: ObservationEvent) -> dict[str, Any]:
    """Serialize a journal event using the WebUI-facing ``payload`` name."""

    result = event.model_dump(mode="json")
    result["payload"] = result.pop("data")
    return result


@dataclass(slots=True)
class JournalSubscription:
    replay: list[ObservationEvent]
    queue: asyncio.Queue[ObservationEvent]
    resync_required: bool
    earliest_seq: int
    latest_seq: int


class EventJournal:
    """An in-memory ordered event log with atomic replay/subscription."""

    def __init__(self, max_events: int = 10_000) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._events: deque[ObservationEvent] = deque(maxlen=max_events)
        self._next_seq = 1
        self._subscribers: set[asyncio.Queue[ObservationEvent]] = set()
        self._lock = asyncio.Lock()

    @property
    def latest_seq(self) -> int:
        return self._next_seq - 1

    @property
    def earliest_seq(self) -> int:
        return self._events[0].seq if self._events else self._next_seq

    async def publish(
        self,
        event_type: str,
        *,
        data: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        turn_id: int | None = None,
        transfer_id: str | None = None,
        agent_id: str | None = None,
    ) -> ObservationEvent:
        payload = data or {}
        if contains_token_ids(payload):
            raise ValueError("observation events must not contain token_ids")
        async with self._lock:
            event = ObservationEvent(
                seq=self._next_seq,
                timestamp=utc_now(),
                type=event_type,
                conversation_id=conversation_id,
                turn_id=turn_id,
                transfer_id=transfer_id,
                agent_id=agent_id,
                data=payload,
            )
            self._next_seq += 1
            self._events.append(event)
            for queue in tuple(self._subscribers):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    # A slow UI must resync instead of exerting backpressure on
                    # the session state machine.
                    self._subscribers.discard(queue)
            return event

    async def snapshot(self, after_seq: int = 0) -> tuple[list[ObservationEvent], bool]:
        async with self._lock:
            earliest = self.earliest_seq
            resync = after_seq > self.latest_seq or bool(self._events and after_seq < earliest - 1)
            if resync:
                return [], True
            return [event for event in self._events if event.seq > after_seq], False

    async def subscribe(self, after_seq: int = 0) -> JournalSubscription:
        queue: asyncio.Queue[ObservationEvent] = asyncio.Queue(maxsize=512)
        async with self._lock:
            earliest = self.earliest_seq
            latest = self.latest_seq
            resync = after_seq > latest or bool(self._events and after_seq < earliest - 1)
            replay = [] if resync else [event for event in self._events if event.seq > after_seq]
            if not resync:
                self._subscribers.add(queue)
            return JournalSubscription(replay, queue, resync, earliest, latest)

    async def unsubscribe(self, queue: asyncio.Queue[ObservationEvent]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)
