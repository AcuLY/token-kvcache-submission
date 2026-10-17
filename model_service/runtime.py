"""In-memory deterministic Token and Mock KV model runtime."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
from dataclasses import dataclass, field
from time import perf_counter
from uuid import uuid4

from shared.contracts import (
    AgentProfile,
    CreateSessionRequest,
    CreateSessionResponse,
    GenerateRequest,
    GenerateResponse,
    GenerationConfig,
    ModelBehaviorConfig,
    ModelSessionRef,
    ModelTimings,
    PrefillRequest,
    PrefillResponse,
    RenderLocalInputRequest,
    RenderLocalInputResponse,
    RuntimeDescriptor,
    SealRequest,
    SealResponse,
    StateDescriptor,
    TokenizeRequest,
    TokenizeResponse,
)
from shared.kv import (
    CacheExportRequest,
    CacheImportRequest,
    CacheImportResponse,
    CacheInventory,
    CachePayload,
    CacheStatusRequest,
    TokenImportRequest,
    prefix_digest,
    runtime_fingerprint,
)

from .tokenizer import ControlToken, MockByteTokenizer


class ModelRuntimeError(Exception):
    """Base exception translated to an HTTP error by the FastAPI layer."""


class SessionNotFoundError(ModelRuntimeError):
    pass


class StateConflictError(ModelRuntimeError):
    pass


@dataclass(slots=True)
class _LogicalSession:
    ref: ModelSessionRef
    token_ids: list[int] = field(default_factory=list)
    state_id: str | None = None
    parent_state_id: str | None = None
    sealed: bool = False
    state_counter: int = 0
    generated_turns: dict[int, GenerateResponse] = field(default_factory=dict)
    kv: bytes = b""
    context_epoch: int = 0
    pinned_prefix_tokens: int = 0
    initialization_ms: float = 0
    token_imports: dict[str, tuple[TokenImportRequest, CacheImportResponse]] = field(
        default_factory=dict
    )
    imports: dict[str, tuple[CachePayload, CacheImportResponse]] = field(default_factory=dict)


class MockModelRuntime:
    """A deterministic stand-in for a remote model server.

    The runtime stores a separate canonical token list for every logical
    session, including deterministic mock cache blocks for export and import.
    """

    def __init__(
        self,
        *,
        descriptor: RuntimeDescriptor | None = None,
        behavior: ModelBehaviorConfig | None = None,
        tokenizer: MockByteTokenizer | None = None,
    ) -> None:
        self._descriptor = descriptor or RuntimeDescriptor()
        self._behavior = behavior or ModelBehaviorConfig()
        self._tokenizer = tokenizer or MockByteTokenizer()
        self._sessions: dict[str, _LogicalSession] = {}
        self._lock = asyncio.Lock()

    @property
    def tokenizer(self) -> MockByteTokenizer:
        return self._tokenizer

    async def describe(self) -> RuntimeDescriptor:
        return self._descriptor.model_copy(deep=True)

    async def get_behavior(self) -> ModelBehaviorConfig:
        async with self._lock:
            return self._behavior.model_copy(deep=True)

    async def set_behavior(self, behavior: ModelBehaviorConfig) -> ModelBehaviorConfig:
        async with self._lock:
            self._behavior = behavior.model_copy(deep=True)
            return self._behavior.model_copy(deep=True)

    async def create_session(self, request: CreateSessionRequest) -> CreateSessionResponse:
        session_id = str(uuid4())
        ref = ModelSessionRef(
            session_id=session_id,
            agent_id=request.agent_id,
            conversation_id=request.conversation_id,
        )
        async with self._lock:
            self._sessions[session_id] = _LogicalSession(ref=ref)
        return CreateSessionResponse(session=ref)

    async def tokenize(self, request: TokenizeRequest) -> TokenizeResponse:
        behavior = await self.get_behavior()
        started = perf_counter()
        await self._delay(behavior.tokenize_delay_ms)
        tokens = self._tokenizer.encode(
            request.text,
            add_bos=request.add_bos,
            add_eos=request.add_eos,
        )
        return TokenizeResponse(
            token_ids=tokens,
            token_count=len(tokens),
            elapsed_ms=self._elapsed_ms(started),
        )

    async def render_local_input(
        self, request: RenderLocalInputRequest
    ) -> RenderLocalInputResponse:
        session = await self._get_session(request.session_id)
        self._ensure_agent(session, request.profile)
        tokens = self._render_suffix(
            request.profile,
            request.local_context,
            request.turn_id,
        )
        return RenderLocalInputResponse(token_ids=tokens, token_count=len(tokens))

    async def prefill(self, request: PrefillRequest) -> PrefillResponse:
        behavior = await self.get_behavior()
        tokens = self._tokenizer.validate(request.token_ids)
        started = perf_counter()
        await self._delay(behavior.prefill_delay_ms + behavior.prefill_per_token_ms * len(tokens))
        async with self._lock:
            session = self._sessions.get(request.session_id)
            if session is None:
                raise SessionNotFoundError(f"unknown logical session: {request.session_id}")
            if session.pinned_prefix_tokens and session.state_id is not None:
                raise StateConflictError(
                    "initialized shared context requires versioned message import"
                )
            if request.context_epoch < session.context_epoch:
                raise StateConflictError("prefill epoch is stale")
            if not request.replace and request.context_epoch != session.context_epoch:
                raise StateConflictError("append prefill cannot change epoch")
            if request.replace:
                next_tokens = tokens.copy()
            else:
                if request.parent_state_id != session.state_id:
                    raise StateConflictError(
                        "append prefill parent_state_id does not match the active state"
                    )
                next_tokens = session.token_ids + tokens
            self._ensure_context_limit(next_tokens)
            if request.pinned_prefix_tokens is not None:
                if session.state_id is not None or request.pinned_prefix_tokens != len(next_tokens):
                    raise StateConflictError("shared prompt can only be pinned at initialization")
                session.pinned_prefix_tokens = request.pinned_prefix_tokens
                session.initialization_ms = self._elapsed_ms(started)
            session.token_ids = next_tokens
            session.context_epoch = request.context_epoch
            session.kv = self._cache_bytes(next_tokens)
            session.parent_state_id = request.parent_state_id
            session.state_counter += 1
            session.state_id = request.state_id or (
                f"{session.ref.session_id}:prefill:{session.state_counter}"
            )
            session.sealed = False
            session.generated_turns.clear()
            state = self._state_descriptor(session)
        return PrefillResponse(
            session_id=request.session_id,
            state=state,
            elapsed_ms=self._elapsed_ms(started),
        )

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        behavior = await self.get_behavior()
        async with self._lock:
            session = self._sessions.get(request.session_id)
            if session is None:
                raise SessionNotFoundError(f"unknown logical session: {request.session_id}")
            cached = session.generated_turns.get(request.turn_id)
            if cached is not None:
                return cached.model_copy(deep=True)
            if request.profile is not None:
                self._ensure_agent(session, request.profile)
            original_tokens = session.token_ids.copy()
            base_tokens = original_tokens.copy()
            original_kv = session.kv
            original_epoch = session.context_epoch
            parent_state_id = session.state_id

        suffix_started = perf_counter()
        if request.suffix_token_ids is not None:
            suffix = self._tokenizer.validate(request.suffix_token_ids)
        else:
            assert request.profile is not None
            suffix = self._render_suffix(
                request.profile,
                request.local_context,
                request.turn_id,
            )
        generation_config = request.generation_config or (
            request.profile.generation_config if request.profile is not None else GenerationConfig()
        )
        window = request.history_window_tokens
        trimmed = 0
        if window is not None:
            available = window - len(suffix) - generation_config.max_new_tokens - 1
            if available < 0:
                raise StateConflictError(
                    "CONTEXT_LIMIT: local input and output budget exceed window"
                )
            pinned = session.pinned_prefix_tokens
            if available < pinned:
                raise StateConflictError(
                    "CONTEXT_LIMIT: shared prompt and output budget exceed window"
                )
            if len(base_tokens) > available:
                keep_tail = (available - pinned) // 2
                kept = base_tokens[:pinned] + (base_tokens[-keep_tail:] if keep_tail else [])
                trimmed = len(base_tokens) - len(kept)
                base_tokens = kept
        rebuild_started = perf_counter()
        rebuilt = trimmed > 0 or len(original_kv) != len(original_tokens) * 16
        if rebuilt:
            await self._delay(
                behavior.prefill_delay_ms + behavior.prefill_per_token_ms * len(base_tokens)
            )
            base_kv = self._cache_bytes(base_tokens)
        else:
            base_kv = original_kv
        rebuild_ms = self._elapsed_ms(rebuild_started) if rebuilt else 0.0
        suffix_started = perf_counter()
        await self._delay(behavior.prefill_delay_ms + behavior.prefill_per_token_ms * len(suffix))
        suffix_ms = self._elapsed_ms(suffix_started)

        generation_started = perf_counter()
        await self._delay(behavior.generate_delay_ms)
        generation_config = request.generation_config or (
            request.profile.generation_config if request.profile is not None else GenerationConfig()
        )
        agent_id = session.ref.agent_id
        response_text = self._response_text(
            behavior=behavior,
            agent_id=agent_id,
            turn_id=request.turn_id,
            context_tokens=base_tokens + suffix,
            max_new_tokens=generation_config.max_new_tokens,
        )
        generated_tokens = self._tokenizer.encode(response_text)
        all_tokens = base_tokens + suffix + generated_tokens + [int(ControlToken.ASSISTANT_END)]
        self._ensure_context_limit(all_tokens)
        generation_ms = self._elapsed_ms(generation_started)

        async with self._lock:
            session = self._sessions.get(request.session_id)
            if session is None:
                raise SessionNotFoundError(f"unknown logical session: {request.session_id}")
            # A concurrent mutation would make the generated lineage invalid.
            if (
                session.state_id != parent_state_id
                or session.token_ids != original_tokens
                or session.context_epoch != original_epoch
            ):
                raise StateConflictError("logical session changed while generation was running")
            session.state_counter += 1
            state_id = f"{session.ref.session_id}:turn:{request.turn_id}:{session.state_counter}"
            session.token_ids = all_tokens
            session.kv = self._cache_bytes(
                all_tokens[len(base_tokens) :],
                base_kv,
                len(base_tokens),
            )
            session.context_epoch = original_epoch + int(trimmed > 0)
            session.parent_state_id = parent_state_id
            session.state_id = state_id
            session.sealed = False
            response = GenerateResponse(
                context_epoch=session.context_epoch,
                shared_prefix_tokens=len(base_tokens),
                trimmed_tokens=trimmed,
                session_id=session.ref.session_id,
                conversation_id=session.ref.conversation_id,
                agent_id=session.ref.agent_id,
                turn_id=request.turn_id,
                state_id=state_id,
                parent_state_id=parent_state_id,
                text=response_text,
                token_ids=all_tokens.copy(),
                generated_token_ids=generated_tokens,
                token_count=len(all_tokens),
                eos=False,
                timings=ModelTimings(
                    context_rebuild_ms=rebuild_ms,
                    prefill_ms=suffix_ms,
                    generate_ms=generation_ms,
                ),
            )
            session.generated_turns[request.turn_id] = response.model_copy(deep=True)
            return response

    async def seal(self, request: SealRequest) -> SealResponse:
        started = perf_counter()
        async with self._lock:
            session = self._sessions.get(request.session_id)
            if session is None:
                raise SessionNotFoundError(f"unknown logical session: {request.session_id}")
            if session.state_id != request.state_id:
                raise StateConflictError("seal state_id does not match the active state")
            if len(session.token_ids) != request.expected_token_count:
                raise StateConflictError(
                    "seal expected_token_count does not match the active token length"
                )
            session.sealed = True
            state = self._state_descriptor(session)
        return SealResponse(
            session_id=request.session_id,
            state=state,
            elapsed_ms=self._elapsed_ms(started),
        )

    async def _get_session(self, session_id: str) -> _LogicalSession:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(f"unknown logical session: {session_id}")
            return session

    @staticmethod
    def _validate_shared_state(session: _LogicalSession, tokens: list[int], epoch: int) -> None:
        pinned = session.pinned_prefix_tokens
        if tokens[:pinned] != session.token_ids[:pinned] or len(tokens) < pinned:
            raise StateConflictError("shared initial prompt mismatch")
        if epoch == session.context_epoch:
            known = len(session.token_ids)
            if len(tokens) < known or tokens[:known] != session.token_ids:
                raise StateConflictError("known canonical Token prefix differs")

    async def import_tokens(self, request: TokenImportRequest) -> CacheImportResponse:
        started = perf_counter()
        behavior = await self.get_behavior()
        manifest = request.manifest
        tokens = self._tokenizer.validate(request.token_ids)
        async with self._lock:
            session = self._sessions.get(request.session_id)
            if session is None:
                raise SessionNotFoundError(request.session_id)
            if manifest.transfer_id in session.imports:
                raise StateConflictError("transfer_id already used for KV")
            previous = session.token_imports.get(manifest.transfer_id)
            if previous:
                if previous[0] != request:
                    raise StateConflictError("transfer_id reused with different Token message")
                return previous[1].model_copy(deep=True)
            if (
                manifest.protocol_version != "2"
                or manifest.mode != "TOKEN"
                or manifest.runtime_fingerprint != runtime_fingerprint(self._descriptor)
                or manifest.conversation_id != session.ref.conversation_id
                or manifest.target_agent != session.ref.agent_id
            ):
                raise StateConflictError("Token import protocol/runtime/session mismatch")
            if manifest.scope == "DELTA":
                if (
                    manifest.context_epoch != session.context_epoch
                    or manifest.base_state_id != session.state_id
                    or manifest.start_token != len(session.token_ids)
                    or manifest.base_digest != prefix_digest(session.token_ids)
                ):
                    raise StateConflictError("Token delta base is stale or missing")
                next_tokens = session.token_ids + tokens
                cached = len(session.kv) // 16
                base_kv = session.kv
            elif manifest.scope == "RECOVERY":
                if (
                    manifest.start_token != 0
                    or manifest.base_state_id is not None
                    or manifest.context_epoch < session.context_epoch
                ):
                    raise StateConflictError("invalid recovery checkpoint")
                next_tokens, cached, base_kv = tokens, 0, b""
            else:
                raise StateConflictError("normal Token handoff must use DELTA")
            if (
                len(next_tokens) != manifest.token_count
                or prefix_digest(next_tokens) != manifest.target_digest
            ):
                raise StateConflictError("Token target length or digest mismatch")
            self._validate_shared_state(session, next_tokens, manifest.context_epoch)
            self._ensure_context_limit(next_tokens)
            # Historical Token IDs are already local: rebuild only the missing KV plus new input.
            await self._delay(
                behavior.prefill_delay_ms
                + behavior.prefill_per_token_ms * (len(next_tokens) - cached)
            )
            next_kv = self._cache_bytes(next_tokens[cached:], base_kv, cached)
            session.token_ids = next_tokens.copy()
            session.kv = next_kv
            session.state_id = manifest.state_id
            session.parent_state_id = manifest.parent_state_id
            session.context_epoch = manifest.context_epoch
            session.generated_turns.clear()
            session.sealed = True
            response = CacheImportResponse(
                inventory=self._inventory(session),
                token_ids=next_tokens.copy(),
                elapsed_ms=self._elapsed_ms(started),
            )
            session.token_imports[manifest.transfer_id] = (
                request.model_copy(deep=True),
                response.model_copy(deep=True),
            )
            return response

    @staticmethod
    def _cache_bytes(tokens: list[int], prefix: bytes = b"", start: int = 0) -> bytes:
        # Deterministic mock K/V blocks, dependent on the whole preceding context.
        blocks = bytearray(prefix)
        previous = prefix[-16:] if prefix else bytes(16)
        for position, token in enumerate(tokens, start):
            previous = hashlib.sha256(previous + struct.pack("!II", position, token)).digest()[:16]
            blocks.extend(previous)
        return bytes(blocks)

    def _inventory(self, session: _LogicalSession) -> CacheInventory:
        return CacheInventory(
            known_state_id=session.state_id,
            known_token_count=len(session.token_ids),
            known_token_digest=prefix_digest(session.token_ids),
            runtime_fingerprint=runtime_fingerprint(self._descriptor),
            initialization_ms=session.initialization_ms,
            state_id=session.state_id if session.kv else None,
            token_count=len(session.kv) // 16,
            context_epoch=session.context_epoch,
            prefix_digest=prefix_digest(session.token_ids[: len(session.kv) // 16]),
        )

    async def evict_cache(self, request: CacheStatusRequest) -> CacheInventory:
        async with self._lock:
            session = self._sessions.get(request.session_id)
            if session is None:
                raise SessionNotFoundError(request.session_id)
            keep = min(request.retain_prefix_tokens, len(session.kv) // 16)
            session.kv = session.kv[: keep * 16]
            session.imports.clear()
            session.token_imports.clear()
            return self._inventory(session)

    async def cache_status(self, request: CacheStatusRequest) -> CacheInventory:
        async with self._lock:
            session = self._sessions.get(request.session_id)
            if session is None:
                raise SessionNotFoundError(request.session_id)
            return self._inventory(session)

    async def export_cache(self, request: CacheExportRequest) -> CachePayload:
        behavior = await self.get_behavior()
        await self._delay(behavior.cache_export_delay_ms)
        async with self._lock:
            session = self._sessions.get(request.session_id)
            if session is None:
                raise SessionNotFoundError(request.session_id)
            k = request.start_token
            if (
                session.state_id != request.state_id
                or not session.sealed
                or request.context_epoch != session.context_epoch
                or len(session.kv) != len(session.token_ids) * 16
                or k > len(session.token_ids)
            ):
                raise StateConflictError("cache export state, epoch or range mismatch")
            if k and (
                not request.base_state_id
                or prefix_digest(session.token_ids[:k]) != request.base_digest
            ):
                raise StateConflictError("cache export prefix mismatch")
            return CachePayload(
                target_digest=prefix_digest(session.token_ids),
                runtime=self._descriptor,
                state_id=request.state_id,
                parent_state_id=session.parent_state_id,
                base_state_id=request.base_state_id,
                start_token=k,
                token_count=len(session.token_ids),
                context_epoch=session.context_epoch,
                base_digest=request.base_digest,
                token_ids=session.token_ids[k:],
                kv_base64=base64.b64encode(session.kv[k * 16 :]).decode("ascii"),
            )

    async def import_cache(self, request: CacheImportRequest) -> CacheImportResponse:
        started = perf_counter()
        payload = request.payload
        behavior = await self.get_behavior()
        await self._delay(behavior.cache_import_delay_ms)
        async with self._lock:
            session = self._sessions.get(request.session_id)
            if session is None:
                raise SessionNotFoundError(request.session_id)
            if request.transfer_id in session.token_imports:
                raise StateConflictError("transfer_id already used for Token")
            previous = session.imports.get(request.transfer_id)
            if previous:
                if previous[0] != payload:
                    raise StateConflictError("transfer_id reused with different cache payload")
                return previous[1].model_copy(deep=True)
            if (
                payload.runtime.compatibility_key() != self._descriptor.compatibility_key()
                or payload.cache_format != "mock-kv-v1"
                or payload.context_epoch < session.context_epoch
                or (payload.start_token > 0 and payload.context_epoch != session.context_epoch)
            ):
                raise StateConflictError("incompatible cache runtime, format or epoch")
            k = payload.start_token
            if k and (
                session.state_id != payload.base_state_id
                or len(session.kv) != k * 16
                or prefix_digest(session.token_ids[:k]) != payload.base_digest
            ):
                raise StateConflictError("cache import base is stale or missing")
            if not k and payload.base_state_id is not None:
                raise StateConflictError("full cache must not bind a base state")
            tokens = self._tokenizer.validate(payload.token_ids)
            if k + len(tokens) != payload.token_count:
                raise StateConflictError("cache sidecar length mismatch")
            next_tokens = session.token_ids[:k] + tokens
            if payload.target_digest and prefix_digest(next_tokens) != payload.target_digest:
                raise StateConflictError("KV target digest mismatch")
            self._validate_shared_state(session, next_tokens, payload.context_epoch)
            self._ensure_context_limit(next_tokens)
            try:
                suffix = base64.b64decode(payload.kv_base64, validate=True)
            except ValueError as exc:
                raise StateConflictError("invalid cache encoding") from exc
            prefix = session.kv[: k * 16]
            if prefix + suffix != self._cache_bytes(tokens, prefix, k):
                raise StateConflictError("cache blocks do not match token sidecar")
            # All checks finish before changing the active model state.
            session.token_ids = next_tokens
            session.kv = prefix + suffix
            session.context_epoch = payload.context_epoch
            session.parent_state_id = payload.parent_state_id
            session.state_id = payload.state_id
            session.sealed = True
            session.generated_turns.clear()
            response = CacheImportResponse(
                inventory=self._inventory(session),
                token_ids=next_tokens.copy(),
                elapsed_ms=self._elapsed_ms(started),
            )
            session.imports[request.transfer_id] = (
                payload.model_copy(deep=True),
                response.model_copy(deep=True),
            )
            return response

    @staticmethod
    def _ensure_agent(session: _LogicalSession, profile: AgentProfile) -> None:
        if session.ref.agent_id != profile.agent_id:
            raise StateConflictError("profile agent_id does not own this logical session")

    def _ensure_context_limit(self, token_ids: list[int]) -> None:
        if len(token_ids) > self._descriptor.max_context_tokens:
            raise StateConflictError(
                f"context contains {len(token_ids)} tokens, exceeding runtime limit "
                f"{self._descriptor.max_context_tokens}"
            )

    def _render_suffix(
        self,
        profile: AgentProfile,
        local_context: str,
        turn_id: int,
    ) -> list[int]:
        if local_context:
            raise StateConflictError("private per-turn input is not supported by shared-context v2")
        return [
            int(ControlToken.USER_START),
            *self._tokenizer.encode(f"Agent {profile.agent_id}, turn {turn_id}"),
            int(ControlToken.USER_END),
            int(ControlToken.ASSISTANT_START),
        ]

    def _response_text(
        self,
        *,
        behavior: ModelBehaviorConfig,
        agent_id: str,
        turn_id: int,
        context_tokens: list[int],
        max_new_tokens: int,
    ) -> str:
        text = (
            f"{behavior.response_prefix} {agent_id} turn {turn_id}: "
            f"context={len(context_tokens)} tokens."
        )
        encoded = self._tokenizer.encode(text)
        if len(encoded) <= max_new_tokens:
            return text
        encoded = encoded[:max_new_tokens]
        while encoded:
            try:
                return self._tokenizer.decode(encoded)
            except UnicodeDecodeError:
                encoded.pop()
        return ""

    @staticmethod
    def _state_descriptor(session: _LogicalSession) -> StateDescriptor:
        if session.state_id is None:
            raise StateConflictError("logical session has no active state")
        return StateDescriptor(
            state_id=session.state_id,
            parent_state_id=session.parent_state_id,
            token_count=len(session.token_ids),
            sealed=session.sealed,
        )

    @staticmethod
    async def _delay(delay_ms: float) -> None:
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000.0)

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return max(0.0, (perf_counter() - started) * 1000.0)


__all__ = [
    "MockModelRuntime",
    "ModelRuntimeError",
    "SessionNotFoundError",
    "StateConflictError",
]
