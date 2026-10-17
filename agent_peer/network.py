"""Deterministic application-layer link shaping for real TCP bytes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import perf_counter

from shared.contracts import NetworkState

Sleep = Callable[[float], Awaitable[None]]


class DeterministicLinkConditionApplier:
    """Apply the immutable network snapshot bound to a transfer plan.

    The RTT (including the Coordinator-sampled jitter) is paid once before the
    first byte. Serialization time is paid before every chunk is written, so a
    receiver measuring the actual socket sees the requested link conditions.
    """

    def __init__(self, *, sleep: Sleep | None = None) -> None:
        self._sleep = sleep or self._sleep_until_elapsed

    @staticmethod
    async def _sleep_until_elapsed(seconds: float) -> None:
        # Windows event-loop timers can fire within one coarse clock tick of
        # their deadline. Measure the actual elapsed delay with a monotonic clock.
        deadline = perf_counter() + seconds
        while (remaining := deadline - perf_counter()) > 0:  # noqa: ASYNC110 - duration shaper
            await asyncio.sleep(remaining)

    @staticmethod
    def predicted_p2p_ms(network: NetworkState, payload_bytes: int) -> float:
        serialization_ms = payload_bytes * 8 / (network.bandwidth_mbps * 1_000_000) * 1000
        return network.effective_rtt_ms + serialization_ms

    async def write(
        self,
        writer: asyncio.StreamWriter,
        frame: bytes,
        network: NetworkState,
        *,
        chunk_bytes: int,
    ) -> None:
        if network.effective_rtt_ms:
            await self._sleep(network.effective_rtt_ms / 1000.0)

        bytes_per_second = network.bandwidth_mbps * 1_000_000 / 8
        for offset in range(0, len(frame), chunk_bytes):
            chunk = frame[offset : offset + chunk_bytes]
            await self._sleep(len(chunk) / bytes_per_second)
            writer.write(chunk)
            await writer.drain()


__all__ = ["DeterministicLinkConditionApplier"]
