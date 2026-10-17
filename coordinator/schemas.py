"""Coordinator-owned API and in-memory state models.

Wire contracts shared with the peers live in ``shared.models``.  The models in
this module are deliberately limited to control-plane configuration, views and
acknowledgements; no model here has a token payload field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts import DecisionRecord
from shared.kv import CacheInventory
from shared.models import AgentProfile, ModelBehaviorConfig, SessionConfig, TransferPlan


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigDocument(StrictModel):
    version: int = Field(ge=1)
    config: SessionConfig


class ConfigUpdate(StrictModel):
    expected_version: int = Field(ge=1)
    config: SessionConfig


class SessionCreateRequest(StrictModel):
    config: SessionConfig | None = None


class NetworkScenario(StrictModel):
    bandwidth_mbps: float = Field(default=100.0, gt=0)
    rtt_ms: float = Field(default=10.0, ge=0)
    jitter_ms: float = Field(default=1.0, ge=0)
    seed: int = 7


class MockConfig(StrictModel):
    version: int = Field(default=1, ge=1)
    links: dict[str, NetworkScenario] = Field(
        default_factory=lambda: {
            "agent-a->agent-b": NetworkScenario(),
            "agent-b->agent-a": NetworkScenario(
                bandwidth_mbps=80.0, rtt_ms=14.0, jitter_ms=2.0, seed=11
            ),
        }
    )
    model: ModelBehaviorConfig = Field(default_factory=ModelBehaviorConfig)

    @model_validator(mode="after")
    def validate_directions(self) -> MockConfig:
        for direction in self.links:
            if "->" not in direction:
                raise ValueError("link keys must use '<source>-><target>'")
            source, target = direction.split("->", 1)
            if not source or not target or source == target:
                raise ValueError("link directions must identify two different agents")
        return self


class EnvironmentDocument(StrictModel):
    version: int = Field(default=1, ge=1)
    environment_id: str = "local-mock"
    model_provider: str = "mock_http"
    network_provider: str = "directional_mock"
    transfer_provider: str = "shared_context_v2"


class UiExtension(StrictModel):
    id: str
    label: str
    route: str
    kind: str = "tool"


class CapabilityDocument(StrictModel):
    protocol_version: str = "2"
    transfer_modes: list[str] = Field(
        default_factory=lambda: ["TOKEN/DELTA", "TOKEN/RECOVERY", "KV/FULL", "KV/DELTA"]
    )
    mode_policies: list[str] = Field(
        default_factory=lambda: ["always_token", "always_kv", "adaptive"]
    )
    runtime_features: list[str] = Field(
        default_factory=lambda: ["tokenize", "prefill", "generate", "seal", "kv_full", "kv_delta"]
    )
    analysis_features: list[str] = Field(
        default_factory=lambda: [
            "overview",
            "network",
            "transfers",
            "events",
            "csv",
            "cache_metrics",
        ]
    )
    unsupported_features: list[str] = Field(default_factory=lambda: ["real_model"])
    ui_extensions: list[UiExtension] = Field(default_factory=list)
    providers: dict[str, str]


class RegisteredAgentView(StrictModel):
    agent_id: str
    instance_id: str
    control_url: str
    data_bind: str
    data_advertise: str
    runtime_descriptor: dict[str, Any]
    transport_capabilities: list[str]
    status: str
    online: bool
    ready: bool
    registered_at: datetime
    last_heartbeat_at: datetime
    conversation_id: str | None = None
    phase: str | None = None


class MessageRecord(StrictModel):
    output_token_count: int = Field(default=0, ge=0)
    turn_id: int = Field(ge=1)
    agent_id: str
    text: str
    state_id: str
    token_count: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    turn_index: int | None = Field(default=None, ge=1)
    id: str | None = None

    @model_validator(mode="after")
    def populate_view_fields(self) -> MessageRecord:
        if self.turn_index is None:
            self.turn_index = self.turn_id
        if self.id is None:
            self.id = f"{self.agent_id}-{self.turn_id}"
        return self


class LedgerEntry(StrictModel):
    target_digest: str = ""
    context_epoch: int = Field(default=0, ge=0)
    transfer_id: str
    turn_id: int = Field(ge=1)
    source_agent: str
    target_agent: str
    state_id: str
    parent_state_id: str | None = None
    mode: Literal["TOKEN", "KV"] = "TOKEN"
    scope: Literal["FULL", "DELTA", "RECOVERY"] = "FULL"
    token_count: int = Field(ge=0)
    payload_bytes: int = Field(ge=0)
    committed_at: datetime = Field(default_factory=utc_now)


class MetricRecord(StrictModel):
    phase: str
    duration_ms: float = Field(ge=0)
    agent_id: str | None = None
    turn_id: int | None = Field(default=None, ge=1)
    transfer_id: str | None = None
    recorded_at: datetime = Field(default_factory=utc_now)
    values: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class TransferRecord(StrictModel):
    output_token_count: int = Field(default=0, ge=0)
    wrapper_token_count: int = Field(default=0, ge=0)
    receiver_known_tokens: int = Field(default=0, ge=0)
    receiver_cache_tokens: int = Field(default=0, ge=0)
    recovery_reason: str | None = None
    context_epoch: int = Field(default=0, ge=0)
    trimmed_tokens: int = Field(default=0, ge=0)
    decision: DecisionRecord | None = None
    base_state_id: str | None = None
    start_token: int = Field(default=0, ge=0)
    xi_tokens: int = Field(default=0, ge=0)
    transfer_id: str
    turn_id: int = Field(ge=1)
    source_agent_id: str
    target_agent_id: str
    mode: str = "TOKEN"
    variant: str = "FULL"
    selector_reason: str
    state_id: str
    parent_state_id: str | None = None
    token_count: int = Field(ge=0)
    payload_bytes: int = Field(ge=0)
    predicted_ms: float = Field(ge=0)
    actual_ms: float | None = Field(default=None, ge=0)
    status: Literal["PLANNED", "PREPARED", "RUNNING", "COMMITTED", "FAILED"]
    network: dict[str, Any]
    timings: dict[str, float] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    direction: str | None = None
    turn_index: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def populate_view_fields(self) -> TransferRecord:
        if self.direction is None:
            self.direction = f"{self.source_agent_id}->{self.target_agent_id}"
        if self.turn_index is None:
            self.turn_index = self.turn_id
        return self


class SessionView(StrictModel):
    protocol_version: str = "2"
    initial_state: CacheInventory | None = None
    initialization: dict[str, CacheInventory] = Field(default_factory=dict)
    conversation_id: str
    config_version: int = Field(ge=1)
    config: SessionConfig
    status: Literal["STARTING", "RUNNING", "PAUSED", "STOPPING", "STOPPED", "COMPLETED", "ERROR"]
    turn_count: int = Field(ge=0)
    current_speaker: str
    pending_transfer_id: str | None = None
    final_state_id: str | None = None
    finish_reason: str | None = None
    messages: list[MessageRecord] = Field(default_factory=list)
    ledger: list[LedgerEntry] = Field(default_factory=list)
    transfers: list[TransferRecord] = Field(default_factory=list)
    metrics: list[MetricRecord] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TransferCommitAck(StrictModel):
    transfer_id: str
    conversation_id: str
    receiver_agent: str
    state_id: str
    turn_id: int | None = Field(default=None, ge=0)
    token_count: int | None = Field(default=None, ge=0)
    success: bool = True
    error: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)


class MetricReport(StrictModel):
    conversation_id: str
    phase: str
    duration_ms: float = Field(ge=0)
    agent_id: str | None = None
    turn_id: int | None = Field(default=None, ge=1)
    transfer_id: str | None = None
    values: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class AgentErrorReport(StrictModel):
    conversation_id: str | None = None
    agent_id: str
    phase: str
    message: str
    code: str | None = None
    turn_id: int | None = Field(default=None, ge=1)
    transfer_id: str | None = None


class AgentCommand(StrictModel):
    """Stable coordinator-to-agent envelope.

    ``payload`` is command-specific metadata only. Token bytes remain on the P2P
    socket and can never be inserted into this model due to the recursive guard
    in the command client.
    """

    command_id: str
    type: Literal[
        "open_session", "generate", "prepare_transfer", "execute_transfer", "stop", "set_model"
    ]
    payload: dict[str, Any]


class PendingTransfer(StrictModel):
    plan: TransferPlan
    stage: Literal["PLANNED", "PREPARED", "EXECUTING"] = "PLANNED"


def default_session_config() -> SessionConfig:
    """Return a fresh, editable six-turn demo configuration."""

    common_generation = {"max_new_tokens": 64, "temperature": 0.0}
    profile_a = AgentProfile(
        agent_id="agent-a",
        display_name="Agent A",
        role_description="提出具体、可执行的方案。",
        system_prompt="你是 Agent A，请简洁、建设性地推进任务。",
        prompt_version="v1",
        local_context_provider="static",
        local_context="来自 Agent A 的本地上下文。",
        generation_config=common_generation,
    )
    profile_b = AgentProfile(
        agent_id="agent-b",
        display_name="Agent B",
        role_description="审阅上一轮内容并提出改进。",
        system_prompt="你是 Agent B，请检查上一轮回复并给出改进。",
        prompt_version="v1",
        local_context_provider="static",
        local_context="来自 Agent B 的本地上下文。",
        generation_config=common_generation,
    )
    return SessionConfig(
        agent_a_profile=profile_a,
        agent_b_profile=profile_b,
        initial_input="请协作完成这项本地演示任务。",
        first_speaker="agent-a",
        max_agent_turns=6,
        max_context_tokens=4096,
        stop_on_eos=True,
        mode_policy="adaptive",
    )
