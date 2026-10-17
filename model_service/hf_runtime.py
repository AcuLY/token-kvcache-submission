"""Real HuggingFace KV-aware model runtime behind the shared HTTP contract.

This is the server side of the "real Model Service" described in
``docs/architecture.md`` (P4). It loads one causal LM plus tokenizer, keeps one
isolated logical cache session per Agent, and exports/imports the actual
per-layer K/V tensors over the same HTTP endpoints the Mock runtime exposes.

torch/transformers stay optional. Importing this module never requires them;
they are only needed by :meth:`HuggingFaceKvModelRuntime.load` and by the
methods that touch a real cache. The deterministic Mock runtime remains the
default backend and needs neither dependency.

Wire layout: a KV payload is the uint32 Token sidecar for the transferred
range followed by the serialized cache bytes. The bytes are produced by
``_pack`` as one stacked ``(layers, 2, batch, kv_heads, seq, head_dim)``
tensor, which keeps the whole suffix in a single host transfer and preserves
the model dtype exactly. ``RuntimeDescriptor.kv_bytes_per_token`` and
``kv_container_bytes`` describe the layout so the coordinator can size the
P2P payload before the export happens.
"""

from __future__ import annotations

import asyncio
import base64
import struct
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, cast
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

from .runtime import ModelRuntimeError, SessionNotFoundError, StateConflictError

HF_CACHE_FORMAT = "hf-stacked-kv-v1"
CHAT_TEMPLATE_FORMAT = "chat_template"
PLAIN_FORMAT = "plain"
PROMPT_FORMATS = {"auto", CHAT_TEMPLATE_FORMAT, PLAIN_FORMAT}
DEFAULT_TURN_TEMPLATE = "[Agent {agent_id} turn {turn_id}]"

# Fixed-size container header so the serialized KV payload length is exactly
# count * kv_bytes_per_token + KV_CONTAINER_BYTES. A torch pickle header would
# drift by a byte or two as the storage size crosses integer-encoding limits,
# which would break the coordinator's payload prediction.
KV_MAGIC = b"HFSKV1\x00\x00"
_KV_HEADER = struct.Struct("<8sB5I3x")
KV_CONTAINER_BYTES = _KV_HEADER.size
_DTYPE_CODES = {"float32": 0, "float16": 1, "bfloat16": 2}
_DTYPE_NAMES_BY_CODE = {code: name for name, code in _DTYPE_CODES.items()}

_DTYPE_NAMES = {
    "float32": "float32",
    "float": "float32",
    "fp32": "float32",
    "float16": "float16",
    "half": "float16",
    "fp16": "float16",
    "bfloat16": "bfloat16",
    "bf16": "bfloat16",
}


@dataclass(slots=True)
class _LogicalSession:
    ref: ModelSessionRef
    token_ids: list[int] = field(default_factory=list)
    cache: Any = None
    state_id: str | None = None
    parent_state_id: str | None = None
    sealed: bool = False
    state_counter: int = 0
    generated_turns: dict[int, GenerateResponse] = field(default_factory=dict)
    context_epoch: int = 0
    pinned_prefix_tokens: int = 0
    initialization_ms: float = 0
    token_imports: dict[str, tuple[TokenImportRequest, CacheImportResponse]] = field(
        default_factory=dict
    )
    imports: dict[str, tuple[CachePayload, CacheImportResponse]] = field(default_factory=dict)


class HuggingFaceKvModelRuntime:
    """A real causal-LM runtime with range KV export/import and continuation."""

    def __init__(
        self,
        *,
        model: Any,
        tokenizer: Any,
        descriptor: RuntimeDescriptor | None = None,
        prefill_chunk_tokens: int = 512,
        device: str | None = None,
        cache_format: str = HF_CACHE_FORMAT,
        prompt_format: str = "auto",
        turn_template: str = DEFAULT_TURN_TEMPLATE,
        repetition_penalty: float = 1.0,
        no_repeat_ngram_size: int = 0,
        behavior: ModelBehaviorConfig | None = None,
    ) -> None:
        if prefill_chunk_tokens < 1:
            raise ValueError("prefill_chunk_tokens must be positive")
        if prompt_format not in PROMPT_FORMATS:
            raise ValueError(f"prompt_format must be one of {sorted(PROMPT_FORMATS)}")
        if repetition_penalty <= 0:
            raise ValueError("repetition_penalty must be positive")
        if no_repeat_ngram_size < 0:
            raise ValueError("no_repeat_ngram_size must not be negative")
        try:
            rendered_turn = turn_template.format(
                agent_id="agent-a", turn_id=1, system_prompt="Agent instructions"
            )
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                "turn_template may only reference {agent_id}, {turn_id}, and {system_prompt}"
            ) from exc
        if not rendered_turn.strip():
            raise ValueError("turn_template must render a non-empty turn marker")
        self._model = model.eval()
        self._tokenizer = tokenizer
        self._cache_format = cache_format
        # A model's own chat template is the only portable role signal; keeping
        # a plain-text fallback means base models and template-free tokenizers
        # still work, just without an instruction-tuned turn structure.
        self._prompt_format = self._resolve_prompt_format(prompt_format)
        self._turn_template = turn_template
        self._repetition_penalty = float(repetition_penalty)
        self._no_repeat_ngram_size = int(no_repeat_ngram_size)
        self._prefill_chunk_tokens = prefill_chunk_tokens
        self._device = device or self._detect_device()
        self._model.to(self._device)
        self._dtype = self._model_dtype()
        self._vocab_size = int(getattr(self._model.config, "vocab_size", 0) or 0)
        self._kv_bytes_per_token = self._compute_kv_bytes_per_token()
        self._kv_container_bytes = KV_CONTAINER_BYTES
        self._descriptor = descriptor or self._build_descriptor()
        self._behavior = behavior or ModelBehaviorConfig(response_prefix="HF")
        self._sessions: dict[str, _LogicalSession] = {}
        # One model instance and one device: serialize every forward so two
        # logical sessions cannot interleave cache access or CUDA kernels.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup
    @classmethod
    def load(
        cls,
        *,
        model_id: str,
        revision: str | None = None,
        tokenizer_id: str | None = None,
        tokenizer_revision: str | None = None,
        dtype: str = "float32",
        device: str = "auto",
        prefill_chunk_tokens: int = 512,
        max_context_tokens: int | None = None,
        local_files_only: bool = False,
        cache_format: str = HF_CACHE_FORMAT,
        prompt_format: str = "auto",
        turn_template: str = DEFAULT_TURN_TEMPLATE,
        repetition_penalty: float = 1.0,
        no_repeat_ngram_size: int = 0,
    ) -> HuggingFaceKvModelRuntime:
        torch, auto_model, auto_tokenizer = _import_transformers()
        torch_dtype = getattr(torch, _resolve_dtype_name(dtype))
        tokenizer_source = tokenizer_id or model_id
        tokenizer = auto_tokenizer.from_pretrained(
            tokenizer_source,
            revision=tokenizer_revision or revision,
            local_files_only=local_files_only,
        )
        model = auto_model.from_pretrained(
            model_id,
            revision=revision,
            dtype=torch_dtype,
            local_files_only=local_files_only,
        )
        resolved_device = _resolve_device(torch, device)
        runtime = cls(
            model=model,
            tokenizer=tokenizer,
            prefill_chunk_tokens=prefill_chunk_tokens,
            device=resolved_device,
            cache_format=cache_format,
            prompt_format=prompt_format,
            turn_template=turn_template,
            repetition_penalty=repetition_penalty,
            no_repeat_ngram_size=no_repeat_ngram_size,
        )
        runtime._descriptor = runtime._build_descriptor(
            model_id=model_id,
            revision=revision,
            tokenizer_id=tokenizer_source,
            tokenizer_revision=tokenizer_revision or revision,
            max_context_tokens=max_context_tokens,
        )
        return runtime

    @classmethod
    def from_env(cls) -> HuggingFaceKvModelRuntime:
        import os

        model_id = os.getenv("HF_MODEL_ID")
        if not model_id:
            raise RuntimeError("HF_MODEL_ID is required for MODEL_BACKEND=huggingface")
        max_context = os.getenv("HF_MAX_CONTEXT_TOKENS")
        return cls.load(
            model_id=model_id,
            revision=os.getenv("HF_MODEL_REVISION") or None,
            tokenizer_id=os.getenv("HF_TOKENIZER_ID") or None,
            tokenizer_revision=os.getenv("HF_TOKENIZER_REVISION") or None,
            dtype=os.getenv("HF_DTYPE", "float32"),
            device=os.getenv("HF_DEVICE", "auto"),
            prefill_chunk_tokens=int(os.getenv("HF_PREFILL_CHUNK_TOKENS", "512")),
            max_context_tokens=int(max_context) if max_context else None,
            local_files_only=os.getenv("HF_LOCAL_FILES_ONLY", "").lower() in {"1", "true", "yes"},
            cache_format=os.getenv("HF_CACHE_FORMAT", HF_CACHE_FORMAT),
            prompt_format=os.getenv("HF_PROMPT_FORMAT", "auto"),
            turn_template=os.getenv("HF_TURN_TEMPLATE", DEFAULT_TURN_TEMPLATE),
            repetition_penalty=float(os.getenv("HF_REPETITION_PENALTY", "1.0")),
            no_repeat_ngram_size=int(os.getenv("HF_NO_REPEAT_NGRAM", "0")),
        )

    # ---------------------------------------------------------------- describe
    async def describe(self) -> RuntimeDescriptor:
        return self._descriptor.model_copy(deep=True)

    async def get_behavior(self) -> ModelBehaviorConfig:
        async with self._lock:
            return self._behavior.model_copy(deep=True)

    async def set_behavior(self, behavior: ModelBehaviorConfig) -> ModelBehaviorConfig:
        # The real backend has no Mock delay knobs. Echo the stored behavior so
        # the shared /behavior endpoint stays consistent for the WebUI.
        async with self._lock:
            return self._behavior.model_copy(deep=True)

    # ---------------------------------------------------------------- sessions
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
        started = perf_counter()
        token_ids = self._encode_shared_prompt(
            request.text,
            add_special_tokens=bool(request.add_bos or request.add_eos),
        )
        return TokenizeResponse(
            token_ids=token_ids,
            token_count=len(token_ids),
            elapsed_ms=_elapsed(started),
        )

    async def render_local_input(
        self, request: RenderLocalInputRequest
    ) -> RenderLocalInputResponse:
        session = await self._require_session(request.session_id)
        self._ensure_agent(session, request.profile)
        if request.local_context:
            raise StateConflictError("private per-turn input is not supported by shared-context v2")
        token_ids = self._render_suffix(request.profile, request.turn_id)
        return RenderLocalInputResponse(token_ids=token_ids, token_count=len(token_ids))

    async def prefill(self, request: PrefillRequest) -> PrefillResponse:
        started = perf_counter()
        tokens = self._validate_tokens(request.token_ids)
        async with self._lock:
            session = self._require_session_unlocked(request.session_id)
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
                base_cache = None
            else:
                if request.parent_state_id != session.state_id:
                    raise StateConflictError(
                        "append prefill parent_state_id does not match the active state"
                    )
                next_tokens = session.token_ids + tokens
                base_cache = session.cache
            self._ensure_context_limit(next_tokens)
            if request.pinned_prefix_tokens is not None:
                if session.state_id is not None or request.pinned_prefix_tokens != len(next_tokens):
                    raise StateConflictError("shared prompt can only be pinned at initialization")
                session.pinned_prefix_tokens = request.pinned_prefix_tokens
            cache = await asyncio.to_thread(self._forward_cache, tokens, base_cache)
            if request.pinned_prefix_tokens is not None:
                # Initialization is the shared prompt prefill both Agents run.
                session.initialization_ms = _elapsed(started)
            session.token_ids = next_tokens
            session.cache = cache
            session.context_epoch = request.context_epoch
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
            elapsed_ms=_elapsed(started),
        )

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        async with self._lock:
            session = self._require_session_unlocked(request.session_id)
            cached = session.generated_turns.get(request.turn_id)
            if cached is not None:
                return cached.model_copy(deep=True)
            if request.profile is not None:
                self._ensure_agent(session, request.profile)
            if request.suffix_token_ids is not None:
                suffix = self._validate_tokens(request.suffix_token_ids)
            else:
                assert request.profile is not None
                if request.local_context:
                    raise StateConflictError(
                        "private per-turn input is not supported by shared-context v2"
                    )
                suffix = self._render_suffix(request.profile, request.turn_id)
            if not suffix:
                raise StateConflictError("generation requires a non-empty turn prompt")
            config = request.generation_config or (
                request.profile.generation_config
                if request.profile is not None
                else GenerationConfig()
            )
            original_tokens = session.token_ids.copy()
            parent_state_id = session.state_id
            original_epoch = session.context_epoch
            base_tokens, trimmed = self._windowed_base(
                session,
                original_tokens,
                suffix,
                config,
                request.history_window_tokens,
            )
            rebuild_started = perf_counter()
            if trimmed:
                base_cache, _ = await asyncio.to_thread(self._forward, base_tokens, None)
            else:
                base_cache = session.cache
            rebuild_ms = _elapsed(rebuild_started)
            prefill_started = perf_counter()
            if len(suffix) > 1:
                # Prefill the turn context, then hand the final prompt token to
                # generate() so the returned cache still covers the whole
                # canonical sequence.
                base_cache = await asyncio.to_thread(
                    self._forward_cache, suffix[:-1], base_cache
                )
            prefill_ms = _elapsed(prefill_started)
            generation_started = perf_counter()
            cache, emitted = await asyncio.to_thread(
                self._sample, base_cache, suffix[-1:], config
            )
            generate_ms = _elapsed(generation_started)
            eos_token = self._eos_token_id()
            ended = bool(emitted) and eos_token is not None and emitted[-1] == eos_token
            ending = [] if ended else ([eos_token] if eos_token is not None else [])
            all_tokens = base_tokens + suffix + emitted + ending
            # generate() stops right after selecting the final token, so that
            # token (and the turn terminator) may still be absent from the
            # returned cache. Materialize exactly what is missing so the cache
            # length always equals the canonical token length.
            missing = len(all_tokens) - self._cache_seq_length(cache)
            if missing > 0:
                cache = await asyncio.to_thread(
                    self._forward_cache, all_tokens[-missing:], cache
                )
            elif missing < 0:
                raise StateConflictError("generation cache exceeds the canonical sequence")
            self._ensure_context_limit(all_tokens)
            if (
                session.state_id != parent_state_id
                or session.token_ids != original_tokens
                or session.context_epoch != original_epoch
            ):
                raise StateConflictError("logical session changed while generation was running")
            session.state_counter += 1
            state_id = f"{session.ref.session_id}:turn:{request.turn_id}:{session.state_counter}"
            session.token_ids = all_tokens
            session.cache = cache
            session.context_epoch = original_epoch + int(trimmed > 0)
            session.parent_state_id = parent_state_id
            session.state_id = state_id
            session.sealed = False
            text = self._decode_text(emitted)
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
                text=text,
                token_ids=all_tokens.copy(),
                generated_token_ids=emitted,
                token_count=len(all_tokens),
                eos=ended,
                timings=ModelTimings(
                    context_rebuild_ms=rebuild_ms,
                    prefill_ms=prefill_ms,
                    generate_ms=generate_ms,
                ),
            )
            session.generated_turns[request.turn_id] = response.model_copy(deep=True)
            return response

    async def seal(self, request: SealRequest) -> SealResponse:
        started = perf_counter()
        async with self._lock:
            session = self._require_session_unlocked(request.session_id)
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
            elapsed_ms=_elapsed(started),
        )

    # --------------------------------------------------------------- inventory
    async def cache_status(self, request: CacheStatusRequest) -> CacheInventory:
        async with self._lock:
            session = self._require_session_unlocked(request.session_id)
            return self._inventory(session)

    async def evict_cache(self, request: CacheStatusRequest) -> CacheInventory:
        async with self._lock:
            session = self._require_session_unlocked(request.session_id)
            covered = self._cache_seq_length(session.cache)
            keep = min(request.retain_prefix_tokens, covered)
            session.cache = await asyncio.to_thread(self._truncate_cache, session.cache, keep)
            session.imports.clear()
            session.token_imports.clear()
            return self._inventory(session)

    async def probe_logits(self, session_id: str, token_ids: list[int]) -> list[float]:
        """Last-position logits after appending ``token_ids`` to a cache clone.

        Validation helper: the caller can compare an imported KV state with a
        from-scratch prefill of the same canonical tokens without mutating the
        session. The served HTTP contract does not expose this method.
        """

        async with self._lock:
            session = self._require_session_unlocked(session_id)
            if session.cache is None:
                raise StateConflictError("session has no cache to probe")
            tokens = self._validate_tokens(token_ids)
            if not tokens:
                raise StateConflictError("probe requires at least one token")
            cache = await asyncio.to_thread(self._clone_cache, session.cache)
            _, logits = await asyncio.to_thread(self._forward, tokens, cache)
            assert logits is not None
            return [float(value) for value in logits[0].float().cpu().tolist()]

    def _clone_cache(self, cache: Any) -> Any:
        from transformers import DynamicCache

        legacy = cache.to_legacy_cache()
        cloned = tuple(tuple(tensor.clone() for tensor in layer) for layer in legacy)
        return DynamicCache.from_legacy_cache(cast(Any, cloned))

    # ------------------------------------------------------------ import/export
    async def import_tokens(self, request: TokenImportRequest) -> CacheImportResponse:
        started = perf_counter()
        manifest = request.manifest
        incoming = self._validate_tokens(request.token_ids)
        async with self._lock:
            session = self._require_session_unlocked(request.session_id)
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
                next_tokens = session.token_ids + incoming
                base_cache = session.cache
            elif manifest.scope == "RECOVERY":
                if (
                    manifest.start_token != 0
                    or manifest.base_state_id is not None
                    or manifest.context_epoch < session.context_epoch
                ):
                    raise StateConflictError("invalid recovery checkpoint")
                next_tokens, base_cache = incoming, None
            else:
                raise StateConflictError("normal Token handoff must use DELTA")
            if (
                len(next_tokens) != manifest.token_count
                or prefix_digest(next_tokens) != manifest.target_digest
            ):
                raise StateConflictError("Token target length or digest mismatch")
            self._ensure_context_limit(next_tokens)
            cache = await asyncio.to_thread(self._forward_cache, incoming, base_cache)
            session.token_ids = next_tokens.copy()
            session.cache = cache
            session.state_id = manifest.state_id
            session.parent_state_id = manifest.parent_state_id
            session.context_epoch = manifest.context_epoch
            session.generated_turns.clear()
            session.sealed = True
            response = CacheImportResponse(
                inventory=self._inventory(session),
                token_ids=next_tokens.copy(),
                elapsed_ms=_elapsed(started),
            )
            session.token_imports[manifest.transfer_id] = (
                request.model_copy(deep=True),
                response.model_copy(deep=True),
            )
            return response

    async def export_cache(self, request: CacheExportRequest) -> CachePayload:
        async with self._lock:
            session = self._require_session_unlocked(request.session_id)
            k = request.start_token
            covered = self._cache_seq_length(session.cache)
            if (
                session.state_id != request.state_id
                or not session.sealed
                or request.context_epoch != session.context_epoch
                or covered != len(session.token_ids)
                or k > len(session.token_ids)
            ):
                raise StateConflictError("cache export state, epoch or range mismatch")
            if k and (
                not request.base_state_id
                or prefix_digest(session.token_ids[:k]) != request.base_digest
            ):
                raise StateConflictError("cache export prefix mismatch")
            kv_bytes = await asyncio.to_thread(self._pack_cache, session.cache, k)
            return CachePayload(
                target_digest=prefix_digest(session.token_ids),
                cache_format=self._cache_format,
                runtime=self._descriptor,
                state_id=request.state_id,
                parent_state_id=session.parent_state_id,
                base_state_id=request.base_state_id,
                start_token=k,
                token_count=len(session.token_ids),
                context_epoch=session.context_epoch,
                base_digest=request.base_digest,
                token_ids=session.token_ids[k:],
                kv_base64=base64.b64encode(kv_bytes).decode("ascii"),
            )

    async def import_cache(self, request: CacheImportRequest) -> CacheImportResponse:
        started = perf_counter()
        payload = request.payload
        async with self._lock:
            session = self._require_session_unlocked(request.session_id)
            if request.transfer_id in session.token_imports:
                raise StateConflictError("transfer_id already used for Token")
            previous = session.imports.get(request.transfer_id)
            if previous:
                if previous[0] != payload:
                    raise StateConflictError("transfer_id reused with different cache payload")
                return previous[1].model_copy(deep=True)
            if (
                payload.runtime.compatibility_key() != self._descriptor.compatibility_key()
                or payload.cache_format != self._cache_format
                or payload.context_epoch < session.context_epoch
                or (payload.start_token > 0 and payload.context_epoch != session.context_epoch)
            ):
                raise StateConflictError("incompatible cache runtime, format or epoch")
            k = payload.start_token
            if k and (
                session.state_id != payload.base_state_id
                or self._cache_seq_length(session.cache) != k
                or prefix_digest(session.token_ids[:k]) != payload.base_digest
            ):
                raise StateConflictError("cache import base is stale or missing")
            if not k and payload.base_state_id is not None:
                raise StateConflictError("full cache must not bind a base state")
            tokens = self._validate_tokens(payload.token_ids)
            if k + len(tokens) != payload.token_count:
                raise StateConflictError("cache sidecar length mismatch")
            next_tokens = session.token_ids[:k] + tokens
            if payload.target_digest and prefix_digest(next_tokens) != payload.target_digest:
                raise StateConflictError("KV target digest mismatch")
            self._ensure_context_limit(next_tokens)
            try:
                kv_bytes = base64.b64decode(payload.kv_base64, validate=True)
            except ValueError as exc:
                raise StateConflictError("invalid cache encoding") from exc
            cache = await asyncio.to_thread(
                self._unpack_cache,
                kv_bytes,
                session.cache if k else None,
                k,
                payload.token_count,
            )
            if self._cache_seq_length(cache) != payload.token_count:
                raise StateConflictError("restored cache length differs from the target state")
            # All checks finish before changing the active model state.
            session.token_ids = next_tokens
            session.cache = cache
            session.context_epoch = payload.context_epoch
            session.parent_state_id = payload.parent_state_id
            session.state_id = payload.state_id
            session.sealed = True
            session.generated_turns.clear()
            response = CacheImportResponse(
                inventory=self._inventory(session),
                token_ids=next_tokens.copy(),
                elapsed_ms=_elapsed(started),
            )
            session.imports[request.transfer_id] = (
                payload.model_copy(deep=True),
                response.model_copy(deep=True),
            )
            return response

    # ------------------------------------------------------------------ helpers
    async def _require_session(self, session_id: str) -> _LogicalSession:
        async with self._lock:
            return self._require_session_unlocked(session_id)

    def _require_session_unlocked(self, session_id: str) -> _LogicalSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"unknown logical session: {session_id}")
        return session

    @staticmethod
    def _ensure_agent(session: _LogicalSession, profile: AgentProfile) -> None:
        if session.ref.agent_id != profile.agent_id:
            raise StateConflictError("profile agent_id does not own this logical session")

    def _encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        return list(self._tokenizer.encode(text, add_special_tokens=add_special_tokens))

    def _resolve_prompt_format(self, requested: str) -> str:
        if requested == "auto":
            return CHAT_TEMPLATE_FORMAT if self._has_chat_template() else PLAIN_FORMAT
        if requested == CHAT_TEMPLATE_FORMAT and not self._has_chat_template():
            raise StateConflictError("tokenizer has no chat template")
        return requested

    def _has_chat_template(self) -> bool:
        template = getattr(self._tokenizer, "chat_template", None)
        return bool(template) and hasattr(self._tokenizer, "apply_chat_template")

    def _chat_template_ids(
        self, messages: list[dict[str, str]], *, add_generation_prompt: bool
    ) -> list[int]:
        rendered = self._tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=add_generation_prompt,
        )
        return [int(token) for token in rendered]

    def _strip_leading_bos(self, token_ids: list[int]) -> list[int]:
        # Every turn renders through the same template, and Llama-style
        # templates start with the BOS token. The canonical sequence already
        # carries exactly one BOS from P0, so a per-turn repeat must go.
        bos = getattr(self._tokenizer, "bos_token_id", None)
        if token_ids and bos is not None and token_ids[0] == bos:
            return token_ids[1:]
        return token_ids

    def _encode_shared_prompt(self, text: str, *, add_special_tokens: bool) -> list[int]:
        if self._prompt_format != CHAT_TEMPLATE_FORMAT:
            return self._encode(text, add_special_tokens=add_special_tokens)
        # The shared P0 is carried as the first user message. Using "user"
        # rather than "system" keeps this portable: some families reject a
        # system role, and every family accepts a leading user turn.
        return self._chat_template_ids(
            [{"role": "user", "content": text}],
            add_generation_prompt=False,
        )

    def _decode_text(self, token_ids: list[int]) -> str:
        if not token_ids:
            return ""
        return self._tokenizer.decode(token_ids, skip_special_tokens=True)

    def _validate_tokens(self, token_ids: list[int]) -> list[int]:
        tokens = [int(token) for token in token_ids]
        if any(token < 0 for token in tokens):
            raise StateConflictError("token ids must be non-negative")
        if self._vocab_size and max(tokens, default=0) >= self._vocab_size:
            raise StateConflictError("token id exceeds the runtime vocabulary")
        return tokens

    def _ensure_context_limit(self, token_ids: list[int]) -> None:
        if len(token_ids) > self._descriptor.max_context_tokens:
            raise StateConflictError(
                f"context contains {len(token_ids)} tokens, exceeding runtime limit "
                f"{self._descriptor.max_context_tokens}"
            )

    def _render_suffix(self, profile: AgentProfile, turn_id: int) -> list[int]:
        if profile.local_context_provider != "static":
            raise StateConflictError("shared-context v2 requires synchronized static context")
        # profile.local_context is part of the shared P0, so it must not be
        # rejected here; only the per-turn request local_context is private.
        marker = self._turn_template.format(
            agent_id=profile.agent_id, turn_id=turn_id, system_prompt=profile.system_prompt
        )
        if self._prompt_format != CHAT_TEMPLATE_FORMAT:
            return self._encode(f"{marker}\n", add_special_tokens=False)
        # A user turn that ends in the assistant header, so the next tokens the
        # model writes are the reply itself.
        return self._strip_leading_bos(
            self._chat_template_ids(
                [{"role": "user", "content": marker}],
                add_generation_prompt=True,
            )
        )

    def _windowed_base(
        self,
        session: _LogicalSession,
        base_tokens: list[int],
        suffix: list[int],
        config: GenerationConfig,
        window: int | None,
    ) -> tuple[list[int], int]:
        if window is None:
            return base_tokens, 0
        available = window - len(suffix) - config.max_new_tokens - 1
        if available < 0:
            raise StateConflictError("CONTEXT_LIMIT: local input and output budget exceed window")
        pinned = session.pinned_prefix_tokens
        if available < pinned:
            raise StateConflictError("CONTEXT_LIMIT: shared prompt and output budget exceed window")
        if len(base_tokens) <= available:
            return base_tokens, 0
        keep_tail = (available - pinned) // 2
        kept = base_tokens[:pinned] + (base_tokens[-keep_tail:] if keep_tail else [])
        return kept, len(base_tokens) - len(kept)

    def _state_descriptor(self, session: _LogicalSession) -> StateDescriptor:
        if session.state_id is None:
            raise StateConflictError("logical session has no active state")
        return StateDescriptor(
            state_id=session.state_id,
            parent_state_id=session.parent_state_id,
            token_count=len(session.token_ids),
            sealed=session.sealed,
        )

    def _inventory(self, session: _LogicalSession) -> CacheInventory:
        covered = self._cache_seq_length(session.cache)
        return CacheInventory(
            known_state_id=session.state_id,
            known_token_count=len(session.token_ids),
            known_token_digest=prefix_digest(session.token_ids),
            runtime_fingerprint=runtime_fingerprint(self._descriptor),
            initialization_ms=session.initialization_ms,
            state_id=session.state_id if covered else None,
            token_count=covered,
            context_epoch=session.context_epoch,
            prefix_digest=prefix_digest(session.token_ids[:covered]),
            cache_format=self._cache_format,
        )

    # --------------------------------------------------------------- HF tensor
    def _forward(self, tokens: list[int], cache: Any) -> tuple[Any, Any]:
        torch = _torch()
        if not tokens:
            return cache, None
        output = None
        with torch.inference_mode():
            for offset in range(0, len(tokens), self._prefill_chunk_tokens):
                chunk = tokens[offset : offset + self._prefill_chunk_tokens]
                output = self._model(
                    torch.tensor([chunk], dtype=torch.long, device=self._device),
                    past_key_values=cache,
                    use_cache=True,
                    logits_to_keep=1,
                )
                cache = output.past_key_values
        assert output is not None
        return cache, output.logits[:, -1]

    def _forward_cache(self, tokens: list[int], cache: Any) -> Any:
        return self._forward(tokens, cache)[0]

    def _sample(
        self, cache: Any, prompt_tokens: list[int], config: GenerationConfig
    ) -> tuple[Any, list[int]]:
        """Decode through the HF generation pipeline.

        Using ``model.generate`` keeps decoding options model-agnostic
        (repetition penalty, no-repeat n-grams, sampling) instead of a
        hand-written greedy loop. The prompt token is appended to the
        already-prefilled cache, so the returned cache still spans the entire
        canonical sequence and the KV length invariant holds.
        """
        torch = _torch()
        if not prompt_tokens:
            raise StateConflictError("generation requires at least one prompt token")
        eos_token = self._eos_token_id()
        past_length = self._cache_seq_length(cache)
        attention_length = past_length + len(prompt_tokens)
        kwargs: dict[str, Any] = {
            "input_ids": torch.tensor([prompt_tokens], dtype=torch.long, device=self._device),
            "attention_mask": torch.ones(
                (1, attention_length), dtype=torch.long, device=self._device
            ),
            "past_key_values": cache,
            # transformers computes an empty cache_position when a cache is
            # passed in, which makes generation fail; supply it explicitly.
            "cache_position": torch.arange(
                past_length,
                past_length + len(prompt_tokens),
                dtype=torch.long,
                device=self._device,
            ),
            "max_new_tokens": config.max_new_tokens,
            "use_cache": True,
            "return_dict_in_generate": True,
            "repetition_penalty": self._repetition_penalty,
            "no_repeat_ngram_size": self._no_repeat_ngram_size,
            "logits_processor": [_finite_logits_processor()],
        }
        if config.temperature and config.temperature > 0:
            kwargs["do_sample"] = True
            kwargs["temperature"] = config.temperature
        else:
            kwargs["do_sample"] = False
        if eos_token is not None:
            kwargs["eos_token_id"] = eos_token
            kwargs["pad_token_id"] = eos_token
        # Transformers generate() does not accept a torch.Generator argument.
        # Preserve RNG state so seeded sampling remains repeatable per request.
        with torch.inference_mode(), torch.random.fork_rng():
            if kwargs["do_sample"]:
                torch.manual_seed(int(config.seed))
            output = self._model.generate(**kwargs)
        generated = [int(token) for token in output.sequences[0][len(prompt_tokens) :]]
        return output.past_key_values, generated

    def _pack_cache(self, cache: Any, start: int) -> bytes:
        torch = _torch()
        legacy = cache.to_legacy_cache()
        layers = torch.stack(
            [torch.stack([tensor[:, :, start:, :] for tensor in layer]) for layer in legacy]
        )
        layers = layers.to("cpu").contiguous()
        if layers.ndim != 6 or layers.shape[1] != 2:
            raise ModelRuntimeError("unexpected cache tensor layout")
        code = _DTYPE_CODES.get(str(layers.dtype).replace("torch.", ""))
        if code is None:
            raise ModelRuntimeError(f"unsupported KV dtype {layers.dtype}")
        header = _KV_HEADER.pack(
            KV_MAGIC,
            code,
            layers.shape[0],
            layers.shape[2],
            layers.shape[3],
            layers.shape[4],
            layers.shape[5],
        )
        return header + layers.reshape(-1).view(torch.uint8).numpy().tobytes()

    def _unpack_cache(self, data: bytes, base: Any, start: int, count: int) -> Any:
        torch = _torch()
        from transformers import DynamicCache

        if len(data) < KV_CONTAINER_BYTES:
            raise StateConflictError("KV container is truncated")
        magic, code, layer_count, batch, heads, seq, head_dim = _KV_HEADER.unpack(
            data[:KV_CONTAINER_BYTES]
        )
        if magic != KV_MAGIC:
            raise StateConflictError("KV container magic differs")
        dtype_name = _DTYPE_NAMES_BY_CODE.get(code)
        if dtype_name is None:
            raise StateConflictError("KV container dtype is unknown")
        dtype = getattr(torch, dtype_name)
        raw = data[KV_CONTAINER_BYTES:]
        expected_bytes = layer_count * 2 * batch * heads * seq * head_dim * dtype.itemsize
        if len(raw) != expected_bytes:
            raise StateConflictError("KV container payload size differs")
        if dtype != self._dtype or layer_count != self._num_layers or seq != count - start:
            raise StateConflictError("KV tensor layout, shape or precision differs")
        incoming = (
            torch.frombuffer(bytearray(raw), dtype=torch.uint8)
            .view(dtype)
            .view(layer_count, 2, batch, heads, seq, head_dim)
            .to(self._device)
        )
        expected_legacy = base.to_legacy_cache() if base is not None else ()
        if start and (
            not expected_legacy or expected_legacy[0][0].shape[2] != start
        ):
            raise StateConflictError("KV delta base length does not match start_token")
        restored = []
        for index in range(layer_count):
            pair: list[Any] = []
            for position in range(2):
                value = incoming[index][position]
                old = expected_legacy[index][position] if expected_legacy else None
                joined = torch.cat([old, value], dim=2) if (start and old is not None) else value
                pair.append(joined)
            restored.append(tuple(pair))
        return DynamicCache.from_legacy_cache(cast(Any, tuple(restored)))

    def _truncate_cache(self, cache: Any, keep: int) -> Any:
        from transformers import DynamicCache

        legacy = cache.to_legacy_cache()
        truncated = tuple(
            tuple(tensor[:, :, :keep, :] for tensor in layer) for layer in legacy
        )
        return DynamicCache.from_legacy_cache(cast(Any, truncated))

    def _cache_seq_length(self, cache: Any) -> int:
        if cache is None:
            return 0
        try:
            return int(cache.get_seq_length())
        except Exception:  # noqa: BLE001 - fall back to the legacy representation
            legacy = cache.to_legacy_cache()
            if not legacy:
                return 0
            return int(legacy[0][0].shape[2])

    def _eos_token_id(self) -> int | None:
        value = getattr(self._tokenizer, "eos_token_id", None)
        return int(value) if value is not None else None

    # -------------------------------------------------------------- descriptor
    @property
    def _num_layers(self) -> int:
        return int(self._model.config.num_hidden_layers)

    @property
    def _num_kv_heads(self) -> int:
        config = self._model.config
        return int(getattr(config, "num_key_value_heads", None) or config.num_attention_heads)

    @property
    def _head_dim(self) -> int:
        config = self._model.config
        head_dim = getattr(config, "head_dim", None)
        if head_dim:
            return int(head_dim)
        return int(config.hidden_size) // int(config.num_attention_heads)

    def _compute_kv_bytes_per_token(self) -> int:
        return 2 * self._num_layers * self._num_kv_heads * self._head_dim * self._dtype.itemsize

    def _build_descriptor(
        self,
        *,
        model_id: str | None = None,
        revision: str | None = None,
        tokenizer_id: str | None = None,
        tokenizer_revision: str | None = None,
        max_context_tokens: int | None = None,
    ) -> RuntimeDescriptor:
        config = self._model.config
        limit = int(getattr(config, "max_position_embeddings", 32768))
        resolved_max = min(limit, max_context_tokens) if max_context_tokens else limit
        tokenizer_source = tokenizer_id or getattr(self._tokenizer, "name_or_path", None)
        revision_value = (
            revision
            or getattr(config, "_commit_hash", None)
            or getattr(self._tokenizer, "init_kwargs", {}).get("revision")
            or "unknown"
        )
        return RuntimeDescriptor(
            provider="huggingface",
            model_id=model_id or getattr(config, "_name_or_path", None) or "huggingface-model",
            revision=str(revision_value),
            tokenizer_id=tokenizer_source or model_id or "huggingface-tokenizer",
            tokenizer_revision=tokenizer_revision or revision or "unknown",
            dtype=str(self._dtype).replace("torch.", ""),
            max_context_tokens=max(1, resolved_max),
            kv_cache_format=self._cache_format,
            kv_bytes_per_token=self._kv_bytes_per_token,
            kv_container_bytes=self._kv_container_bytes,
            prompt_format=self._prompt_format,
            capabilities=[
                "exact_tokenize",
                "logical_sessions",
                "token_prefill",
                "sampled_generate",
                "seal",
                "kv_full",
                "kv_delta",
                "shared_context_v2",
                "token_delta",
                "real_kv_tensors",
                "hf_generate",
            ],
        )

    def _detect_device(self) -> str:
        torch = _torch()
        return "cuda" if torch.cuda.is_available() else "cpu"

    def _model_dtype(self) -> Any:
        try:
            return next(self._model.parameters()).dtype
        except StopIteration:
            return _torch().float32


def _import_transformers() -> tuple[Any, Any, Any]:
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:  # pragma: no cover - optional dependency path
        raise RuntimeError(
            "the HuggingFace backend requires torch and transformers; "
            "install scripts/hf-model-service-requirements.txt"
        ) from exc
    return torch, AutoModelForCausalLM, AutoTokenizer


def _torch() -> Any:
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - optional dependency path
        raise RuntimeError(
            "the HuggingFace backend requires torch; "
            "install scripts/hf-model-service-requirements.txt"
        ) from exc
    return torch


def _finite_logits_processor() -> Any:
    """Fail fast on non-finite logits without storing every step's logits."""

    torch = _torch()
    from transformers import LogitsProcessor

    class _FiniteLogitsProcessor(LogitsProcessor):
        def __call__(self, input_ids: Any, scores: Any) -> Any:
            if not torch.isfinite(scores).all():
                raise ModelRuntimeError("non-finite generation logits")
            return scores

    return _FiniteLogitsProcessor()


def _resolve_dtype_name(dtype: str) -> str:
    name = _DTYPE_NAMES.get(dtype.strip().lower())
    if name is None:
        raise ValueError(f"unsupported dtype '{dtype}'")
    return name


def _resolve_device(torch: Any, device: str) -> str:
    if device and device != "auto":
        return device
    return "cuda" if torch.cuda.is_available() else "cpu"


def _elapsed(started: float) -> float:
    return max(0.0, (perf_counter() - started) * 1000.0)


__all__ = [
    "CHAT_TEMPLATE_FORMAT",
    "DEFAULT_TURN_TEMPLATE",
    "HF_CACHE_FORMAT",
    "KV_CONTAINER_BYTES",
    "PLAIN_FORMAT",
    "HuggingFaceKvModelRuntime",
]
