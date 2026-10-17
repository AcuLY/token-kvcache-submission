"""Pydantic models shared by the coordinator, peers, and model service.

The control-plane models intentionally contain metadata only.  In particular,
``StateOffer`` and ``ObservationEvent`` have no token payload field; token IDs
are accepted only by the peer/model-service and P2P data-plane contracts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class ContractModel(BaseModel):
    """Strict base model used for every wire contract."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class TransferMode(StrEnum):
    TOKEN = "TOKEN"
    KV = "KV"


class TransferScope(StrEnum):
    FULL = "FULL"
    DELTA = "DELTA"
    RECOVERY = "RECOVERY"


class ModePolicy(StrEnum):
    ALWAYS_TOKEN = "always_token"
    ALWAYS_KV = "always_kv"
    ADAPTIVE = "adaptive"


class PaperModelConfig(ContractModel):
    layers: int = Field(default=32, ge=1)
    heads: int = Field(default=32, ge=1)
    head_dim: int = Field(default=128, ge=1)
    model_dim: int = Field(default=4096, ge=1)
    ff_dim: int = Field(default=11008, ge=1)
    vocab_size: int = Field(default=32000, ge=1)
    receiver_flops: float = Field(default=1e12, gt=0, allow_inf_nan=False)
    token_bits: int = Field(default=32, ge=1)
    kv_precision_bits: int = Field(default=16, ge=1)
    compression_ratio: float = Field(default=1, ge=1, allow_inf_nan=False)


class DecisionSettings(ContractModel):
    objective: Literal["paper", "mode_e2e", "p2p_only"] = "paper"
    paper: PaperModelConfig = Field(default_factory=PaperModelConfig)
    # Explicit cold-start/calibrated overhead estimates, including HTTP and serialization.
    token_prepare_ms: float = Field(default=0.1, ge=0, le=60000, allow_inf_nan=False)
    token_restore_overhead_ms: float = Field(default=2, ge=0, le=60000, allow_inf_nan=False)
    kv_export_overhead_ms: float = Field(default=2, ge=0, le=60000, allow_inf_nan=False)
    kv_import_overhead_ms: float = Field(default=2, ge=0, le=60000, allow_inf_nan=False)
    history_window_tokens: int | None = Field(default=1024, ge=64)


class CandidateCost(ContractModel):
    paper_ms: float = Field(default=0, ge=0)
    paper_bits: float = Field(default=0, ge=0)
    paper_applicable: bool = True
    prefill_tokens: int = Field(default=0, ge=0)
    mode: str
    scope: str
    payload_bytes: int = Field(ge=0)
    frame_bytes: int = Field(ge=0)
    sender_ms: float = Field(ge=0)
    p2p_ms: float = Field(ge=0)
    receiver_ms: float = Field(ge=0)
    mode_e2e_ms: float = Field(ge=0)
    objective_ms: float = Field(ge=0)


class DecisionRecord(ContractModel):
    policy: ModePolicy
    objective: Literal["paper", "mode_e2e", "p2p_only"]
    effective_objective: Literal["paper", "mode_e2e", "p2p_only"] = "mode_e2e"
    estimator: str = "shared_context_v2"
    theta_tokens: int = Field(default=0, ge=0)
    wrapper_tokens: int = Field(default=0, ge=0)
    candidates: list[CandidateCost]
    token_minus_kv_ms: float
    selected_mode: str
    selected_scope: str
    reason: str
    cache_prefix_tokens: int = Field(ge=0)
    xi_tokens: int = Field(ge=0)
    current_tokens: int = Field(ge=0)
    model_behavior: dict[str, float]
    overheads: DecisionSettings


class AgentLifecycleStatus(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    BUSY = "BUSY"
    OFFLINE = "OFFLINE"
    ERROR = "ERROR"


class ConversationStatus(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class TransferStatus(StrEnum):
    PLANNED = "PLANNED"
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"


class GenerationConfig(ContractModel):
    max_new_tokens: int = Field(default=64, ge=1, le=4096)
    temperature: float = Field(default=0.0, ge=0.0)
    seed: int = 0


class AgentProfile(ContractModel):
    agent_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    role_description: str = ""
    system_prompt: str = "You are a cooperative agent."
    prompt_version: str = "v1"
    local_context_provider: str = "static"
    local_context: str = ""
    generation_config: GenerationConfig = Field(default_factory=GenerationConfig)


class SessionConfig(ContractModel):
    agent_a_profile: AgentProfile
    agent_b_profile: AgentProfile
    initial_input: str = Field(min_length=1)
    first_speaker: str
    max_agent_turns: int = Field(default=6, ge=1, le=1000)
    max_context_tokens: int = Field(default=4096, ge=1)
    stop_on_eos: bool = True
    mode_policy: ModePolicy = ModePolicy.ADAPTIVE
    decision: DecisionSettings = Field(default_factory=DecisionSettings)

    @model_validator(mode="after")
    def validate_agents(self) -> SessionConfig:
        agent_ids = {
            self.agent_a_profile.agent_id,
            self.agent_b_profile.agent_id,
        }
        if len(agent_ids) != 2:
            raise ValueError(
                "agent_a_profile and agent_b_profile must use distinct agent_id values"
            )
        if any(
            profile.local_context_provider != "static"
            for profile in (self.agent_a_profile, self.agent_b_profile)
        ):
            raise ValueError("shared-context v2 requires static synchronized background")
        if self.first_speaker not in agent_ids:
            raise ValueError("first_speaker must match one of the two configured agents")
        return self


class RuntimeDescriptor(ContractModel):
    protocol_version: Literal["2"] = "2"
    provider: str = "mock_http"
    model_id: str = "mock-model-v1"
    revision: str = "1"
    tokenizer_id: str = "mock-byte-v1"
    tokenizer_revision: str = "1"
    dtype: str = "uint32-token-ids"
    max_context_tokens: int = Field(default=65536, ge=1)
    # Cache wire layout. The Mock runtime keeps 16 deterministic bytes per
    # position; a real HF backend reports its actual tensor size and the fixed
    # serialization overhead so the coordinator can size the P2P payload.
    kv_cache_format: str = "mock-kv-v1"
    kv_bytes_per_token: int = Field(default=16, ge=0)
    kv_container_bytes: int = Field(default=0, ge=0)
    # How text turns into the model's own turn structure: the Mock runtime uses
    # its control tokens, a real backend reports "chat_template" or "plain".
    prompt_format: str = "mock_control_tokens"
    capabilities: list[str] = Field(
        default_factory=lambda: [
            "exact_tokenize",
            "logical_sessions",
            "token_prefill",
            "deterministic_generate",
            "seal",
            "kv_full",
            "kv_delta",
            "shared_context_v2",
            "token_delta",
        ]
    )

    def compatibility_key(self) -> tuple[Any, ...]:
        """Fields that must match before two peers can share model state."""

        return (
            self.model_id,
            self.revision,
            self.tokenizer_id,
            self.tokenizer_revision,
            self.dtype,
        )


class AgentRegistration(ContractModel):
    agent_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    control_url: str = Field(min_length=1)
    data_bind: str = Field(min_length=1)
    data_advertise: str = Field(min_length=1)
    runtime_descriptor: RuntimeDescriptor
    transport_capabilities: list[str] = Field(
        default_factory=lambda: ["TOKEN_DELTA_V2", "TOKEN_RECOVERY_V2"]
    )
    status: AgentLifecycleStatus = AgentLifecycleStatus.READY
    registered_at: datetime = Field(default_factory=utc_now)


class Heartbeat(ContractModel):
    agent_id: str
    instance_id: str
    status: AgentLifecycleStatus
    conversation_id: str | None = None
    phase: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)


class NetworkState(ContractModel):
    snapshot_id: str = Field(default_factory=new_id)
    source_agent: str
    target_agent: str
    bandwidth_mbps: float = Field(gt=0.0)
    rtt_ms: float = Field(default=0.0, ge=0.0)
    jitter_ms: float = Field(default=0.0, ge=0.0)
    sampled_jitter_ms: float = 0.0
    effective_rtt_ms: float = Field(default=0.0, ge=0.0)
    version: int = Field(default=1, ge=1)
    captured_at: datetime = Field(default_factory=utc_now)
    seed: int = 0

    @model_validator(mode="after")
    def validate_sample(self) -> NetworkState:
        if abs(self.sampled_jitter_ms) > self.jitter_ms:
            raise ValueError("sampled_jitter_ms must be within the configured jitter range")
        expected = max(0.0, self.rtt_ms + self.sampled_jitter_ms)
        if self.effective_rtt_ms == 0.0 and expected != 0.0:
            object.__setattr__(self, "effective_rtt_ms", expected)
        return self

    @property
    def direction(self) -> str:
        return f"{self.source_agent}->{self.target_agent}"


class TransferCandidate(ContractModel):
    start_token: int = Field(default=0, ge=0)
    mode: str = "TOKEN"
    scope: str = "FULL"
    token_count: int = Field(ge=0)
    payload_bytes: int = Field(ge=0)
    predicted_p2p_ms: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def validate_token_payload_size(self) -> TransferCandidate:
        if self.mode == "TOKEN" and self.payload_bytes != (self.token_count - self.start_token) * 4:
            raise ValueError("Token payload size must match the transferred range")
        return self


class StateOffer(ContractModel):
    output_token_count: int = Field(default=0, ge=0)
    wrapper_token_count: int = Field(default=0, ge=0)
    target_digest: str = ""
    runtime_fingerprint: str = ""
    shared_prefix_digest: str = ""
    """Control-plane description of a state; never carries token IDs."""

    offer_id: str = Field(default_factory=new_id)
    context_epoch: int = Field(default=0, ge=0)
    shared_prefix_tokens: int | None = Field(default=None, ge=0)
    trimmed_tokens: int = Field(default=0, ge=0)
    conversation_id: str
    turn_id: int = Field(ge=0)
    source_agent: str
    target_agent: str
    state_id: str
    parent_state_id: str | None = None
    token_count: int = Field(ge=0)
    payload_bytes: int = Field(ge=0)
    message_text: str | None = None
    terminal_reason: Literal["EOS", "CONTEXT_LIMIT"] | None = None
    candidates: list[TransferCandidate] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_sizes(self) -> StateOffer:
        for candidate in self.candidates:
            if candidate.token_count != self.token_count:
                raise ValueError("candidate size must match the offered state")
        return self


class TransferPlan(ContractModel):
    output_token_count: int = Field(default=0, ge=0)
    wrapper_token_count: int = Field(default=0, ge=0)
    target_digest: str = ""
    runtime_fingerprint: str = ""
    receiver_known_tokens: int = Field(default=0, ge=0)
    receiver_cache_tokens: int = Field(default=0, ge=0)
    recovery_reason: str | None = None
    base_state_id: str | None = None
    start_token: int = Field(default=0, ge=0)
    base_digest: str = ""
    context_epoch: int = Field(default=0, ge=0)
    protocol_version: str = "2"
    transfer_id: str = Field(default_factory=new_id)
    conversation_id: str
    turn_id: int = Field(ge=0)
    source_agent: str
    target_agent: str
    mode: str = "TOKEN"
    scope: str = "FULL"
    state_id: str
    parent_state_id: str | None = None
    token_count: int = Field(ge=0)
    payload_bytes: int = Field(ge=0)
    kv_bytes_per_token: int = Field(default=16, ge=0)
    kv_container_bytes: int = Field(default=0, ge=0)
    network_snapshot_id: str
    network_state: NetworkState
    selector_reason: str = "only_supported_mode"
    predicted_p2p_ms: float = Field(default=0.0, ge=0.0)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_plan(self) -> TransferPlan:
        if self.mode == "KV":
            if self.scope not in {"FULL", "DELTA"} or self.start_token > self.token_count:
                raise ValueError("invalid KV scope or range")
            expected_kv_bytes = (
                (self.token_count - self.start_token) * (4 + self.kv_bytes_per_token)
                + self.kv_container_bytes
            )
            if self.payload_bytes != expected_kv_bytes:
                raise ValueError("invalid KV payload size for the declared cache format")
            if self.scope == "FULL" and (self.start_token or self.base_state_id is not None):
                raise ValueError("full KV cannot bind a base")
            if self.scope == "DELTA" and (
                not self.start_token or not self.base_state_id or not self.base_digest
            ):
                raise ValueError("delta KV requires a nonempty base")
        if self.source_agent == self.target_agent:
            raise ValueError("source_agent and target_agent must differ")
        if self.mode == "TOKEN":
            if (
                self.start_token > self.token_count
                or self.payload_bytes != (self.token_count - self.start_token) * 4
            ):
                raise ValueError("invalid Token payload range or size")
            if self.scope == "DELTA" and (not self.base_state_id or not self.base_digest):
                raise ValueError("Token delta must bind an existing prefix")
            if self.scope == "RECOVERY" and (self.start_token or self.base_state_id is not None):
                raise ValueError("Token recovery requires a full snapshot")
        if self.network_snapshot_id != self.network_state.snapshot_id:
            raise ValueError("network_snapshot_id must match network_state.snapshot_id")
        if (
            self.source_agent != self.network_state.source_agent
            or self.target_agent != self.network_state.target_agent
        ):
            raise ValueError("network_state direction must match the transfer direction")
        return self

    @property
    def network_snapshot_version(self) -> int:
        return self.network_state.version


class TransferManifest(ContractModel):
    output_token_count: int = Field(default=0, ge=0)
    wrapper_token_count: int = Field(default=0, ge=0)
    target_digest: str = ""
    runtime_fingerprint: str = ""
    receiver_known_tokens: int = Field(default=0, ge=0)
    receiver_cache_tokens: int = Field(default=0, ge=0)
    recovery_reason: str | None = None
    base_state_id: str | None = None
    start_token: int = Field(default=0, ge=0)
    base_digest: str = ""
    context_epoch: int = Field(default=0, ge=0)
    protocol_version: str = "2"
    transfer_id: str
    conversation_id: str
    turn_id: int = Field(ge=0)
    source_agent: str
    target_agent: str
    mode: str = "TOKEN"
    scope: str = "FULL"
    state_id: str
    parent_state_id: str | None = None
    token_count: int = Field(ge=0)
    payload_bytes: int = Field(ge=0)
    kv_bytes_per_token: int = Field(default=16, ge=0)
    kv_container_bytes: int = Field(default=0, ge=0)
    network_snapshot_id: str
    network_version: int = Field(ge=1)
    applied_jitter_ms: float = 0.0

    @classmethod
    def from_plan(cls, plan: TransferPlan) -> TransferManifest:
        return cls(
            output_token_count=plan.output_token_count,
            wrapper_token_count=plan.wrapper_token_count,
            target_digest=plan.target_digest,
            runtime_fingerprint=plan.runtime_fingerprint,
            receiver_known_tokens=plan.receiver_known_tokens,
            receiver_cache_tokens=plan.receiver_cache_tokens,
            recovery_reason=plan.recovery_reason,
            base_state_id=plan.base_state_id,
            start_token=plan.start_token,
            base_digest=plan.base_digest,
            context_epoch=plan.context_epoch,
            mode=plan.mode,
            scope=plan.scope,
            protocol_version=plan.protocol_version,
            transfer_id=plan.transfer_id,
            conversation_id=plan.conversation_id,
            turn_id=plan.turn_id,
            source_agent=plan.source_agent,
            target_agent=plan.target_agent,
            state_id=plan.state_id,
            parent_state_id=plan.parent_state_id,
            token_count=plan.token_count,
            payload_bytes=plan.payload_bytes,
            kv_bytes_per_token=plan.kv_bytes_per_token,
            kv_container_bytes=plan.kv_container_bytes,
            network_snapshot_id=plan.network_snapshot_id,
            network_version=plan.network_state.version,
            applied_jitter_ms=plan.network_state.sampled_jitter_ms,
        )


class TokenTransferManifest(TransferManifest):
    @model_validator(mode="after")
    def validate_payload_size(self) -> TokenTransferManifest:
        if self.mode != "TOKEN" or self.scope != "FULL":
            raise ValueError("unsupported Token manifest mode or scope")
        if self.payload_bytes != self.token_count * 4:
            raise ValueError("payload_bytes must equal token_count * 4")
        return self


class ObservationEvent(ContractModel):
    """Ordered observation metadata suitable for HTTP and WebSocket clients."""

    seq: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=utc_now)
    type: str = Field(min_length=1)
    conversation_id: str | None = None
    turn_id: int | None = Field(default=None, ge=0)
    transfer_id: str | None = None
    agent_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ProviderDescriptor(ContractModel):
    provider_id: str
    kind: Literal["model", "network", "transfer"]
    display_name: str
    provenance: Literal["mock", "real", "test"]
    capabilities: list[str] = Field(default_factory=list)


class ToolPageContribution(ContractModel):
    page_id: str
    route: str
    title: str
    provider_id: str


class CapabilityDocument(ContractModel):
    protocol_version: str = "2"
    environment_id: str = "local-mock"
    providers: list[ProviderDescriptor] = Field(default_factory=list)
    supported_transfer_modes: list[TransferMode] = Field(
        default_factory=lambda: [TransferMode.TOKEN]
    )
    supported_mode_policies: list[ModePolicy] = Field(
        default_factory=lambda: [ModePolicy.ALWAYS_TOKEN]
    )
    analysis_features: dict[str, bool] = Field(
        default_factory=lambda: {
            "token": True,
            "network": True,
            "cache": False,
            "kv": False,
            "adaptive": False,
        }
    )
    tool_pages: list[ToolPageContribution] = Field(default_factory=list)


class ModelTimings(ContractModel):
    context_rebuild_ms: float = Field(default=0.0, ge=0.0)
    tokenize_ms: float = Field(default=0.0, ge=0.0)
    prefill_ms: float = Field(default=0.0, ge=0.0)
    generate_ms: float = Field(default=0.0, ge=0.0)
    seal_ms: float = Field(default=0.0, ge=0.0)


class TransferPhaseMetrics(ContractModel):
    sender_prepare_ms: float = Field(default=0.0, ge=0.0)
    predicted_p2p_ms: float = Field(default=0.0, ge=0.0)
    p2p_ms: float = Field(default=0.0, ge=0.0)
    receiver_prefill_ms: float = Field(default=0.0, ge=0.0)
    local_prefill_ms: float = Field(default=0.0, ge=0.0)
    generation_ms: float = Field(default=0.0, ge=0.0)
    total_ms: float = Field(default=0.0, ge=0.0)


class WireReceipt(ContractModel):
    transfer_id: str
    accepted: bool
    receiver_wire_ms: float = Field(ge=0.0)
    detail: str | None = None


class DataPlaneSendResult(ContractModel):
    frame_bytes: int = Field(ge=0)
    sender_prepare_ms: float = Field(ge=0.0)
    sender_elapsed_ms: float = Field(ge=0.0)
    receiver_wire_ms: float = Field(ge=0.0)


class ConversationMessage(ContractModel):
    conversation_id: str
    turn_id: int = Field(ge=0)
    agent_id: str
    text: str
    token_count: int = Field(ge=0)
    state_id: str
    created_at: datetime = Field(default_factory=utc_now)


class TransferMetric(ContractModel):
    transfer_id: str
    conversation_id: str
    turn_id: int = Field(ge=0)
    source_agent: str
    target_agent: str
    mode: TransferMode = TransferMode.TOKEN
    scope: TransferScope = TransferScope.FULL
    token_count: int = Field(ge=0)
    payload_bytes: int = Field(ge=0)
    network_snapshot_id: str
    status: TransferStatus = TransferStatus.PLANNED
    phases: TransferPhaseMetrics = Field(default_factory=TransferPhaseMetrics)


class ConversationSnapshot(ContractModel):
    conversation_id: str
    status: ConversationStatus
    config: SessionConfig
    current_turn: int = Field(default=0, ge=0)
    current_speaker: str | None = None
    committed_state_id: str | None = None
    messages: list[ConversationMessage] = Field(default_factory=list)
    transfers: list[TransferMetric] = Field(default_factory=list)
    stop_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CommitAck(ContractModel):
    transfer_id: str
    conversation_id: str
    turn_id: int = Field(ge=0)
    target_agent: str
    state_id: str
    token_count: int = Field(ge=0)
    metrics: TransferPhaseMetrics = Field(default_factory=TransferPhaseMetrics)
    committed_at: datetime = Field(default_factory=utc_now)


class AgentErrorReport(ContractModel):
    agent_id: str
    conversation_id: str | None = None
    turn_id: int | None = Field(default=None, ge=0)
    transfer_id: str | None = None
    code: str
    detail: str
    timestamp: datetime = Field(default_factory=utc_now)


class OpenSessionCommand(ContractModel):
    shared_prompt: str = ""
    decision: DecisionSettings = Field(default_factory=DecisionSettings)
    command_id: str
    command_type: Literal["open_session"] = "open_session"
    conversation_id: str
    profile: AgentProfile
    peer_agent_id: str
    initial_input: str
    max_context_tokens: int = Field(ge=1)
    stop_on_eos: bool = True
    is_first_speaker: bool = False


class GenerateCommand(ContractModel):
    command_id: str
    command_type: Literal["generate"] = "generate"
    conversation_id: str
    turn_id: int = Field(ge=0)
    target_agent: str
    profile: AgentProfile
    local_context: str = ""
    initial: bool = False


class PrepareTransferCommand(ContractModel):
    command_id: str
    command_type: Literal["prepare_transfer"] = "prepare_transfer"
    conversation_id: str
    plan: TransferPlan


class ExecuteTransferCommand(ContractModel):
    command_id: str
    command_type: Literal["execute_transfer"] = "execute_transfer"
    conversation_id: str
    plan: TransferPlan
    target_data_endpoint: str


class StopCommand(ContractModel):
    command_id: str
    command_type: Literal["stop"] = "stop"
    conversation_id: str
    reason: str = "user_requested"


AgentCommandEnvelope = Annotated[
    OpenSessionCommand
    | GenerateCommand
    | PrepareTransferCommand
    | ExecuteTransferCommand
    | StopCommand,
    Field(discriminator="command_type"),
]


class AgentCommandResult(ContractModel):
    command_id: str
    accepted: bool = True
    status: str = "completed"
    detail: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


# Model service contracts.  Token arrays are intentionally legal on this
# direct model/peer interface, unlike the coordinator control plane.
class ModelSessionRef(ContractModel):
    session_id: str
    agent_id: str
    conversation_id: str
    created_at: datetime = Field(default_factory=utc_now)


class CreateSessionRequest(ContractModel):
    agent_id: str
    conversation_id: str


class CreateSessionResponse(ContractModel):
    session: ModelSessionRef


class TokenizeRequest(ContractModel):
    text: str
    add_bos: bool = False
    add_eos: bool = False


class TokenizeResponse(ContractModel):
    token_ids: list[int]
    token_count: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0.0)


class RenderLocalInputRequest(ContractModel):
    session_id: str
    profile: AgentProfile
    turn_id: int = Field(ge=0)
    local_context: str = ""


class RenderLocalInputResponse(ContractModel):
    token_ids: list[int]
    token_count: int = Field(ge=0)


class PrefillRequest(ContractModel):
    pinned_prefix_tokens: int | None = Field(default=None, ge=0)
    context_epoch: int = Field(default=0, ge=0)
    session_id: str
    token_ids: list[int]
    state_id: str | None = None
    parent_state_id: str | None = None
    replace: bool = True


class StateDescriptor(ContractModel):
    state_id: str
    parent_state_id: str | None = None
    token_count: int = Field(ge=0)
    sealed: bool = False


class PrefillResponse(ContractModel):
    session_id: str
    state: StateDescriptor
    elapsed_ms: float = Field(ge=0.0)


class GenerateRequest(ContractModel):
    history_window_tokens: int | None = Field(default=None, ge=64)
    session_id: str
    turn_id: int = Field(ge=0)
    profile: AgentProfile | None = None
    local_context: str = ""
    suffix_token_ids: list[int] | None = None
    generation_config: GenerationConfig | None = None

    @model_validator(mode="after")
    def validate_input(self) -> GenerateRequest:
        if self.profile is None and self.suffix_token_ids is None:
            raise ValueError("profile or suffix_token_ids is required")
        return self


class GenerateResponse(ContractModel):
    context_epoch: int = Field(default=0, ge=0)
    shared_prefix_tokens: int = Field(default=0, ge=0)
    trimmed_tokens: int = Field(default=0, ge=0)
    session_id: str
    conversation_id: str
    agent_id: str
    turn_id: int = Field(ge=0)
    state_id: str
    parent_state_id: str | None = None
    text: str
    token_ids: list[int]
    generated_token_ids: list[int]
    token_count: int = Field(ge=0)
    eos: bool = False
    timings: ModelTimings = Field(default_factory=ModelTimings)


class SealRequest(ContractModel):
    session_id: str
    state_id: str
    expected_token_count: int = Field(ge=0)


class SealResponse(ContractModel):
    session_id: str
    state: StateDescriptor
    elapsed_ms: float = Field(ge=0.0)


class ModelBehaviorConfig(ContractModel):
    tokenize_delay_ms: float = Field(default=0.0, ge=0.0, le=60000.0)
    prefill_delay_ms: float = Field(default=0.0, ge=0.0, le=60000.0)
    prefill_per_token_ms: float = Field(default=0.0, ge=0.0, le=10.0, allow_inf_nan=False)
    cache_export_delay_ms: float = Field(default=0.0, ge=0.0, le=60000.0)
    cache_import_delay_ms: float = Field(default=0.0, ge=0.0, le=60000.0)
    generate_delay_ms: float = Field(default=0.0, ge=0.0, le=60000.0)
    response_prefix: str = "Mock"


class HealthResponse(ContractModel):
    status: Literal["ok"] = "ok"
    service: str


__all__ = [
    "AgentCommandEnvelope",
    "AgentCommandResult",
    "AgentErrorReport",
    "AgentLifecycleStatus",
    "AgentProfile",
    "AgentRegistration",
    "CapabilityDocument",
    "CommitAck",
    "CandidateCost",
    "DecisionRecord",
    "DecisionSettings",
    "PaperModelConfig",
    "ContractModel",
    "ConversationMessage",
    "ConversationSnapshot",
    "ConversationStatus",
    "CreateSessionRequest",
    "CreateSessionResponse",
    "ExecuteTransferCommand",
    "GenerateCommand",
    "GenerateRequest",
    "GenerateResponse",
    "GenerationConfig",
    "HealthResponse",
    "Heartbeat",
    "ModePolicy",
    "ModelBehaviorConfig",
    "ModelSessionRef",
    "ModelTimings",
    "NetworkState",
    "ObservationEvent",
    "OpenSessionCommand",
    "PrefillRequest",
    "PrefillResponse",
    "PrepareTransferCommand",
    "ProviderDescriptor",
    "RenderLocalInputRequest",
    "RenderLocalInputResponse",
    "RuntimeDescriptor",
    "SealRequest",
    "SealResponse",
    "SessionConfig",
    "StateDescriptor",
    "StateOffer",
    "StopCommand",
    "TokenTransferManifest",
    "TokenizeRequest",
    "TokenizeResponse",
    "ToolPageContribution",
    "TransferCandidate",
    "TransferMetric",
    "TransferMode",
    "TransferPhaseMetrics",
    "TransferPlan",
    "TransferScope",
    "TransferStatus",
    "new_id",
    "utc_now",
]
