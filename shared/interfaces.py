"""Stable extension ports for Token and KV transfer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from shared.kv import (
    CacheExportRequest,
    CacheImportRequest,
    CacheImportResponse,
    CacheInventory,
    CachePayload,
    CacheStatusRequest,
    TokenImportRequest,
)

from .contracts import (
    CreateSessionRequest,
    CreateSessionResponse,
    DataPlaneSendResult,
    GenerateRequest,
    GenerateResponse,
    NetworkState,
    PrefillRequest,
    PrefillResponse,
    RenderLocalInputRequest,
    RenderLocalInputResponse,
    RuntimeDescriptor,
    SealRequest,
    SealResponse,
    StateOffer,
    TokenizeRequest,
    TokenizeResponse,
    TransferCandidate,
    TransferManifest,
    WireReceipt,
)


class ModelRuntimeClient(Protocol):
    async def import_tokens(self, request: TokenImportRequest) -> CacheImportResponse: ...

    async def evict_cache(self, request: CacheStatusRequest) -> CacheInventory: ...

    async def cache_status(self, request: CacheStatusRequest) -> CacheInventory: ...

    async def export_cache(self, request: CacheExportRequest) -> CachePayload: ...

    async def import_cache(self, request: CacheImportRequest) -> CacheImportResponse: ...

    async def close(self) -> None: ...

    async def describe(self) -> RuntimeDescriptor: ...

    async def create_session(self, request: CreateSessionRequest) -> CreateSessionResponse: ...

    async def tokenize(self, request: TokenizeRequest) -> TokenizeResponse: ...

    async def render_local_input(
        self, request: RenderLocalInputRequest
    ) -> RenderLocalInputResponse: ...

    async def prefill(self, request: PrefillRequest) -> PrefillResponse: ...

    async def generate(self, request: GenerateRequest) -> GenerateResponse: ...

    async def seal(self, request: SealRequest) -> SealResponse: ...


class TransferModeHandler(Protocol):
    provider_id: str
    mode: str
    scope: str

    def candidate(self, offer: StateOffer, network: NetworkState) -> TransferCandidate: ...


class ModeSelector(Protocol):
    def select(self, candidates: Sequence[TransferCandidate]) -> tuple[TransferCandidate, str]: ...


class DataPlaneTransport(Protocol):
    async def send(
        self,
        endpoint: str,
        manifest: TransferManifest,
        payload: bytes,
        network: NetworkState,
    ) -> DataPlaneSendResult: ...

    async def receive(
        self, reader: asyncio.StreamReader
    ) -> tuple[TransferManifest, bytes, float]: ...

    async def write_receipt(self, writer: asyncio.StreamWriter, receipt: WireReceipt) -> None: ...


class NetworkStateProvider(Protocol):
    provider_id: str
    contributes_mock_ui: bool

    def snapshot(self, source_agent: str, target_agent: str, transfer_id: str) -> NetworkState: ...


class LinkConditionApplier(Protocol):
    async def write(
        self,
        writer: asyncio.StreamWriter,
        frame: bytes,
        network: NetworkState,
        *,
        chunk_bytes: int,
    ) -> None: ...


Clock = Callable[[float], Awaitable[None]]


__all__ = [
    "Clock",
    "DataPlaneTransport",
    "LinkConditionApplier",
    "ModeSelector",
    "ModelRuntimeClient",
    "NetworkStateProvider",
    "TransferModeHandler",
]
