"""Mode-specific preparation and restoration, independent of the peer FSM."""

import base64
from typing import Protocol

from shared.codec import decode_token_payload, encode_token_payload
from shared.contracts import PrefillRequest, PrefillResponse, TransferCandidate, TransferManifest
from shared.interfaces import ModelRuntimeClient
from shared.kv import (
    CacheExportRequest,
    CacheImportRequest,
    CachePayload,
    TokenImportRequest,
    kv_wire_bytes,
    runtime_fingerprint,
)


class AgentTransferHandler(Protocol):
    capability: str
    mode: str
    scope: str

    def candidate(self, token_ids: list[int]) -> TransferCandidate: ...
    def prepare(self, token_ids: list[int]) -> bytes: ...
    def decode(self, manifest: TransferManifest, payload: bytes) -> list[int]: ...
    async def restore(
        self,
        model: ModelRuntimeClient,
        session_id: str,
        manifest: TransferManifest,
        payload: bytes,
        token_ids: list[int],
    ) -> PrefillResponse: ...


class TokenSnapshotHandler:
    capability = "TOKEN_SNAPSHOT_V1"
    mode = "TOKEN"
    scope = "FULL"

    def candidate(self, token_ids: list[int]) -> TransferCandidate:
        return TransferCandidate(token_count=len(token_ids), payload_bytes=len(token_ids) * 4)

    def prepare(self, token_ids: list[int]) -> bytes:
        return encode_token_payload(token_ids)

    def decode(self, manifest: TransferManifest, payload: bytes) -> list[int]:
        return decode_token_payload(payload, expected_count=manifest.token_count)

    async def restore(
        self,
        model: ModelRuntimeClient,
        session_id: str,
        manifest: TransferManifest,
        payload: bytes,
        token_ids: list[int],
    ) -> PrefillResponse:
        return await model.prefill(
            PrefillRequest(
                session_id=session_id,
                token_ids=token_ids,
                state_id=manifest.state_id,
                parent_state_id=manifest.parent_state_id,
                replace=True,
                context_epoch=manifest.context_epoch,
            )
        )


class TokenMessageHandler(TokenSnapshotHandler):
    """Output-level Token append, or an explicitly requested recovery checkpoint."""

    def __init__(self, scope: str = "DELTA") -> None:
        self.scope = scope
        self.capability = f"TOKEN_{scope}_V2"

    def decode(self, manifest: TransferManifest, payload: bytes) -> list[int]:
        return decode_token_payload(
            payload, expected_count=manifest.token_count - manifest.start_token
        )

    async def restore(
        self,
        model: ModelRuntimeClient,
        session_id: str,
        manifest: TransferManifest,
        payload: bytes,
        token_ids: list[int],
    ) -> PrefillResponse:
        imported = await model.import_tokens(
            TokenImportRequest(
                session_id=session_id,
                manifest=manifest,
                token_ids=token_ids[manifest.start_token :],
            )
        )
        from shared.contracts import StateDescriptor

        return PrefillResponse(
            session_id=session_id,
            elapsed_ms=imported.elapsed_ms,
            state=StateDescriptor(
                state_id=manifest.state_id,
                parent_state_id=manifest.parent_state_id,
                token_count=imported.inventory.known_token_count,
                sealed=True,
            ),
        )


class KvCacheHandler:
    """uint32 Token sidecar plus opaque cached bytes; FULL and DELTA share the layout.

    The sidecar keeps the transferred range verifiable on the wire, while the
    cache bytes are produced and consumed by the model service. The Mock
    runtime emits 16 deterministic bytes per position; a real HF backend emits
    its serialized tensor bytes, and both are sized from the RuntimeDescriptor.
    """

    mode = "KV"

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self.capability = f"KV_{scope}_V2"

    def candidate(self, token_ids: list[int]) -> TransferCandidate:
        return TransferCandidate(
            mode=self.mode,
            scope=self.scope,
            token_count=len(token_ids),
            payload_bytes=kv_wire_bytes(len(token_ids), 16),
        )

    def prepare(self, token_ids: list[int]) -> bytes:
        raise ValueError("KV payload must be exported from the model service")

    async def export(
        self,
        model: ModelRuntimeClient,
        session_id: str,
        manifest: TransferManifest,
    ) -> bytes:
        exported = await model.export_cache(
            CacheExportRequest(
                session_id=session_id,
                state_id=manifest.state_id,
                base_state_id=manifest.base_state_id,
                start_token=manifest.start_token,
                base_digest=manifest.base_digest,
                context_epoch=manifest.context_epoch,
            )
        )
        if (
            runtime_fingerprint(exported.runtime) != manifest.runtime_fingerprint
            or exported.token_count != manifest.token_count
            or exported.parent_state_id != manifest.parent_state_id
            or exported.cache_format != exported.runtime.kv_cache_format
        ):
            raise ValueError("exported cache differs from transfer plan")
        return encode_token_payload(exported.token_ids) + base64.b64decode(
            exported.kv_base64, validate=True
        )

    def decode(self, manifest: TransferManifest, payload: bytes) -> list[int]:
        count = manifest.token_count - manifest.start_token
        if count < 0:
            raise ValueError("KV wire size mismatch")
        expected = kv_wire_bytes(
            count,
            manifest.kv_bytes_per_token,
            manifest.kv_container_bytes,
        )
        if len(payload) != expected:
            raise ValueError("KV wire size mismatch")
        return decode_token_payload(payload[: count * 4], expected_count=count)

    async def restore(
        self,
        model: ModelRuntimeClient,
        session_id: str,
        manifest: TransferManifest,
        payload: bytes,
        token_ids: list[int],
    ) -> PrefillResponse:
        count = manifest.token_count - manifest.start_token
        descriptor = await model.describe()
        imported = await model.import_cache(
            CacheImportRequest(
                session_id=session_id,
                transfer_id=manifest.transfer_id,
                payload=CachePayload(
                    target_digest=manifest.target_digest,
                    cache_format=descriptor.kv_cache_format,
                    runtime=descriptor,
                    state_id=manifest.state_id,
                    parent_state_id=manifest.parent_state_id,
                    base_state_id=manifest.base_state_id,
                    start_token=manifest.start_token,
                    token_count=manifest.token_count,
                    context_epoch=manifest.context_epoch,
                    base_digest=manifest.base_digest,
                    token_ids=token_ids[manifest.start_token :],
                    kv_base64=base64.b64encode(payload[count * 4 :]).decode("ascii"),
                ),
            )
        )
        if imported.token_ids != token_ids or imported.inventory.state_id != manifest.state_id:
            raise ValueError("imported cache does not match the target state")
        from shared.contracts import StateDescriptor

        return PrefillResponse(
            session_id=session_id,
            elapsed_ms=imported.elapsed_ms,
            state=StateDescriptor(
                state_id=manifest.state_id,
                parent_state_id=manifest.parent_state_id,
                token_count=imported.inventory.token_count,
                sealed=True,
            ),
        )
