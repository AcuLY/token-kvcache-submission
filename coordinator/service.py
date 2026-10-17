"""Coordinator application service and role-agnostic session FSM."""

from __future__ import annotations

import asyncio
import csv
import io
import json
from typing import Any, cast
from uuid import uuid4

import httpx

from coordinator.control import AgentControlClient, HttpAgentControlClient
from coordinator.decision import decide
from coordinator.errors import (
    AgentControlError,
    ConflictError,
    InvalidTransitionError,
    ModelServiceError,
    NotFoundError,
    StaleVersionError,
)
from coordinator.journal import EventJournal, contains_token_ids, public_event
from coordinator.model_connection import (
    Connection,
    ConnectionRequest,
    describe_url,
    prepare_connection,
)
from coordinator.planning import plan_candidates
from coordinator.providers import (
    DirectionalMockNetworkProvider,
    KvSnapshotHandler,
    ProviderRegistry,
    TokenCandidateProvider,
)
from coordinator.registry import AgentRecord, AgentRegistry
from coordinator.schemas import (
    AgentCommand,
    AgentErrorReport,
    CapabilityDocument,
    ConfigDocument,
    EnvironmentDocument,
    LedgerEntry,
    MessageRecord,
    MetricRecord,
    MetricReport,
    MockConfig,
    PendingTransfer,
    SessionView,
    TransferCommitAck,
    TransferRecord,
    UiExtension,
    default_session_config,
    utc_now,
)
from shared.contracts import ModePolicy
from shared.models import (
    AgentRegistration,
    Heartbeat,
    ModelBehaviorConfig,
    StateOffer,
    TransferPlan,
)
from shared.prompt import build_shared_prompt

ACTIVE_STATUSES = {"STARTING", "RUNNING", "PAUSED", "STOPPING"}


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw).upper().split(".")[-1]


def _normalized(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _normalized(child) for key, child in sorted(value.items())}
    if isinstance(value, (list, tuple, set)):
        normalized = [_normalized(child) for child in value]
        return sorted(normalized, key=lambda child: json.dumps(child, sort_keys=True))
    return value


class CoordinatorService:
    def __init__(
        self,
        *,
        heartbeat_timeout_seconds: float = 6.0,
        event_retention: int = 10_000,
        control_client: AgentControlClient | None = None,
        model_service_url: str = "http://127.0.0.1:8100",
        model_service_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.registry = AgentRegistry(heartbeat_timeout_seconds)
        self.journal = EventJournal(event_retention)
        self.control_client = control_client or HttpAgentControlClient()
        self._owns_model_service_client = model_service_client is None
        self._model_url = model_service_url.rstrip("/")
        self._connection: Connection | None = None
        self._model_switching = False
        self._model_switch_failed = False
        self._connection_error: str | None = None
        self._connection_progress: dict[str, Any] | None = None
        self._model_service_client = model_service_client or httpx.AsyncClient(
            base_url=model_service_url.rstrip("/"), timeout=10.0, trust_env=False
        )
        self._config = ConfigDocument(version=1, config=default_session_config())
        environment = EnvironmentDocument()
        network = DirectionalMockNetworkProvider()
        self.providers = ProviderRegistry(
            environment=environment,
            network=network,
            transfer_handlers={
                "token_delta": TokenCandidateProvider(),
                "token_recovery": TokenCandidateProvider("RECOVERY"),
                "kv_full": KvSnapshotHandler("FULL"),
                "kv_delta": KvSnapshotHandler("DELTA"),
            },
        )
        self._sessions: dict[str, SessionView] = {}
        self._pending: dict[str, PendingTransfer] = {}
        self._offer_results: dict[str, TransferPlan | None] = {}
        self._ack_results: dict[str, tuple[TransferCommitAck, SessionView]] = {}
        self._lock = asyncio.Lock()
        self._mock_update_lock = asyncio.Lock()
        # Reported by the registered Agents' RuntimeDescriptor so the capability
        # document reflects a real backend instead of always claiming Mock.
        self._runtime_provider: str | None = None

    async def close(self) -> None:
        if self._connection:
            await self._connection.close()
        await self.control_client.close()
        if self._owns_model_service_client:
            await self._model_service_client.aclose()

    async def model_connection_status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "url": self._model_url,
            "config": (
                self._connection.config
                if self._connection
                else ConnectionRequest(url=self._model_url)
            ).model_dump(mode="json"),
            "switching": self._model_switching,
            "progress": self._connection_progress,
            "active_session": self._active_session_unlocked() is not None,
            "error": self._connection_error,
            "connected": False,
        }
        try:
            descriptor = await describe_url(self._model_url)
            result["descriptor"] = descriptor.model_dump(mode="json")
            result["connected"] = not self._model_switch_failed
        except (httpx.HTTPError, ValueError) as exc:
            result["error"] = f"模型服务不可达：{exc}"
        return result

    async def connect_model(self, config: ConnectionRequest) -> dict[str, Any]:
        async with self._lock:
            if self._active_session_unlocked() is not None or self._model_switching:
                raise ConflictError("请先结束当前会话，再切换模型连接")
            self._model_switching = True
            self._connection_error = None
        candidate: Connection | None = None
        touched: list[AgentRecord] = []
        try:
            agents = await self.registry.require_ready(self._agent_ids(self._config.config))
            candidate = await prepare_connection(
                config, lambda state: setattr(self, "_connection_progress", state)
            )
            for agent in agents:
                # Include the attempted peer: an HTTP timeout can hide a successful change.
                touched.append(agent)
                await self.control_client.send(
                    str(agent.registration.control_url),
                    AgentCommand(
                        command_id=f"model-{uuid4().hex}",
                        type="set_model",
                        payload={"url": candidate.url},
                    ),
                )
            previous = self._connection
            self._connection = candidate
            self._model_url = candidate.url
            self._model_service_client.base_url = httpx.URL(candidate.url)
            self._runtime_provider = candidate.descriptor.provider
            self._model_switch_failed = False
            self._connection_error = None
            if previous:
                await previous.close()
            await self.journal.publish(
                "model.connected",
                data={
                    "mode": config.mode,
                    "model_id": candidate.descriptor.model_id,
                },
            )
        except Exception as exc:
            rollback_errors = []
            for agent in touched:
                try:
                    await self.control_client.send(
                        str(agent.registration.control_url),
                        AgentCommand(
                            command_id=f"rollback-{uuid4().hex}",
                            type="set_model",
                            payload={"url": self._model_url},
                        ),
                    )
                except Exception as rollback:
                    rollback_errors.append(str(rollback))
            self._model_switch_failed = self._model_switch_failed or bool(rollback_errors)
            self._connection_error = str(exc)
            if rollback_errors:
                self._connection_error += "；Agent 回退未完成，请重新连接后再创建会话"
            if candidate and candidate is not self._connection:
                await candidate.close()
            raise ModelServiceError(self._connection_error) from exc
        finally:
            self._model_switching = False
            self._connection_progress = None
        return await self.model_connection_status()

    def capabilities(self) -> CapabilityDocument:
        additions = self.providers.capabilities()
        providers = cast(dict[str, str], additions["providers"])
        model_provider = self._runtime_provider or providers.get("model", "mock_http")
        providers = {**providers, "model": model_provider}
        return CapabilityDocument(
            providers=providers,
            transfer_modes=cast(list[str], additions["transfer_modes"]),
            unsupported_features=[] if model_provider != "mock_http" else ["real_model"],
            ui_extensions=[
                UiExtension.model_validate(item)
                for item in cast(list[dict[str, str]], additions["ui_extensions"])
            ],
        )

    async def register_agent(self, registration: AgentRegistration) -> dict[str, Any]:
        view = await self.registry.register(registration)
        self._runtime_provider = registration.runtime_descriptor.provider
        await self.journal.publish(
            "agent.registered",
            agent_id=registration.agent_id,
            data={"instance_id": registration.instance_id, "status": view.status},
        )
        return view.model_dump(mode="json")

    async def heartbeat_agent(self, heartbeat: Heartbeat) -> dict[str, Any]:
        view = await self.registry.heartbeat(heartbeat)
        await self.journal.publish(
            "agent.heartbeat",
            agent_id=heartbeat.agent_id,
            conversation_id=heartbeat.conversation_id,
            data={"status": view.status, "phase": heartbeat.phase, "ready": view.ready},
        )
        return view.model_dump(mode="json")

    async def get_config(self) -> ConfigDocument:
        async with self._lock:
            return self._config.model_copy(deep=True)

    async def update_config(self, config: Any, expected_version: int | None) -> ConfigDocument:
        async with self._lock:
            current = self._config.version
            if expected_version is not None and expected_version != current:
                raise StaleVersionError(
                    f"configuration version is {current}, not {expected_version}"
                )
            self._validate_session_config(config)
            self._config = ConfigDocument(version=current + 1, config=config)
            result = self._config.model_copy(deep=True)
        await self.journal.publish("config.updated", data={"version": result.version})
        return result

    async def environment(self) -> EnvironmentDocument:
        async with self._lock:
            document = self.providers.environment.model_copy(deep=True)
        if self._runtime_provider and document.model_provider != self._runtime_provider:
            # The registered Agents' RuntimeDescriptor is the authority for the
            # model provider; the stored default must not keep reporting Mock
            # while a real backend is connected. Same rule as capabilities().
            document = document.model_copy(update={"model_provider": self._runtime_provider})
        return document

    async def update_environment(self, document: EnvironmentDocument) -> EnvironmentDocument:
        async with self._lock:
            current = self.providers.environment
            if document.version != current.version:
                raise StaleVersionError(
                    f"environment version is {current.version}, not {document.version}"
                )
            if document.network_provider != self.providers.network.provider_id:
                raise ConflictError(
                    f"network provider '{document.network_provider}' is not registered"
                )
            if document.transfer_provider != "shared_context_v2":
                raise ConflictError("only shared_context_v2 is installed")
            updated = document.model_copy(update={"version": current.version + 1})
            self.providers.environment = updated
        await self.journal.publish(
            "environment.updated",
            data={"version": updated.version, "environment_id": updated.environment_id},
        )
        return updated.model_copy(deep=True)

    async def mock_config(self) -> MockConfig:
        network = self.providers.network
        if not isinstance(network, DirectionalMockNetworkProvider):
            raise NotFoundError("the active environment has no Mock configuration")
        return network.config

    async def model_behavior(self) -> ModelBehaviorConfig:
        try:
            response = await self._model_service_client.get("/behavior")
            response.raise_for_status()
            return ModelBehaviorConfig.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelServiceError(f"cannot read Mock Model behavior: {exc}") from exc

    async def update_model_behavior(self, behavior: ModelBehaviorConfig) -> ModelBehaviorConfig:
        network = self.providers.network
        if not isinstance(network, DirectionalMockNetworkProvider):
            raise NotFoundError("the active environment has no Mock configuration")
        async with self._mock_update_lock:
            current = network.config
            applied = await self._put_model_behavior(behavior)
            updated = current.model_copy(update={"version": current.version + 1, "model": applied})
            network.replace_config(updated)
        await self.journal.publish(
            "mock.updated",
            data={"version": updated.version, "component": "model_behavior"},
        )
        return applied

    async def _put_model_behavior(self, behavior: ModelBehaviorConfig) -> ModelBehaviorConfig:
        try:
            response = await self._model_service_client.put(
                "/behavior", json=behavior.model_dump(mode="json")
            )
            response.raise_for_status()
            return ModelBehaviorConfig.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelServiceError(f"cannot update Mock Model behavior: {exc}") from exc

    async def update_mock_config(self, document: MockConfig) -> MockConfig:
        network = self.providers.network
        if not isinstance(network, DirectionalMockNetworkProvider):
            raise NotFoundError("the active environment has no Mock configuration")
        async with self._mock_update_lock:
            current = network.config
            if document.version != current.version:
                raise StaleVersionError(
                    f"Mock version is {current.version}, not {document.version}"
                )
            applied_behavior = await self._put_model_behavior(document.model)
            updated = document.model_copy(
                update={"version": current.version + 1, "model": applied_behavior}
            )
            network.replace_config(updated)
        await self.journal.publish("mock.updated", data={"version": updated.version})
        return updated

    async def create_session(self, config: Any | None = None) -> SessionView:
        chosen = config or (await self.get_config()).config
        self._validate_session_config(chosen)
        agent_ids = self._agent_ids(chosen)

        async with self._lock:
            if self._model_switching or self._model_switch_failed:
                raise ConflictError("模型连接尚未就绪，请先完成连接")
            active = self._active_session_unlocked()
            if active is not None:
                raise ConflictError(f"conversation '{active.conversation_id}' is already active")

        agents = await self.registry.require_ready(agent_ids)
        self._validate_runtime_match(agents)
        self._validate_transport_capabilities(agents)
        if _enum_value(chosen.mode_policy) in {"ALWAYS_KV", "ADAPTIVE"}:
            for agent in agents:
                if not {"KV_FULL_V2", "KV_DELTA_V2"}.issubset(
                    agent.registration.transport_capabilities
                ) or not {"kv_full", "kv_delta"}.issubset(
                    agent.registration.runtime_descriptor.capabilities
                ):
                    raise ConflictError("both Agents and model runtimes must support KV FULL/DELTA")

        conversation_id = f"conversation-{uuid4().hex}"
        config_version = (await self.get_config()).version
        session = SessionView(
            conversation_id=conversation_id,
            config_version=config_version,
            config=chosen.model_copy(deep=True),
            status="STARTING",
            turn_count=0,
            current_speaker=chosen.first_speaker,
        )
        async with self._lock:
            active = self._active_session_unlocked()
            if active is not None:
                raise ConflictError(f"conversation '{active.conversation_id}' is already active")
            if self._model_switching or self._model_switch_failed:
                raise ConflictError("模型连接尚未就绪，请先完成连接")
            self._sessions[conversation_id] = session

        await self.journal.publish(
            "session.created",
            conversation_id=conversation_id,
            data={
                "config_version": config_version,
                "first_speaker": chosen.first_speaker,
                "max_agent_turns": chosen.max_agent_turns,
            },
        )

        try:
            for agent, profile in zip(
                agents, [chosen.agent_a_profile, chosen.agent_b_profile], strict=True
            ):
                await self._send(
                    agent,
                    AgentCommand(
                        command_id=f"{conversation_id}:open:{profile.agent_id}",
                        type="open_session",
                        payload={
                            "conversation_id": conversation_id,
                            "profile": profile.model_dump(mode="json"),
                            "peer_agent_id": next(
                                candidate
                                for candidate in agent_ids
                                if candidate != profile.agent_id
                            ),
                            "initial_input": chosen.initial_input,
                            "shared_prompt": build_shared_prompt(chosen),
                            "is_first_speaker": profile.agent_id == chosen.first_speaker,
                            "max_context_tokens": chosen.max_context_tokens,
                            "stop_on_eos": chosen.stop_on_eos,
                            "decision": chosen.decision.model_dump(mode="json"),
                        },
                    ),
                )
            inventories = {
                agent.registration.agent_id: await self.control_client.cache_status(
                    agent.registration.control_url,
                    conversation_id,
                )
                for agent in agents
            }
            first_inventory = next(iter(inventories.values()))
            for inventory in inventories.values():
                if (
                    inventory.known_state_id != f"{conversation_id}:initial"
                    or inventory.known_token_count != first_inventory.known_token_count
                    or inventory.known_token_digest != first_inventory.known_token_digest
                    or inventory.token_count != inventory.known_token_count
                    or inventory.prefix_digest != inventory.known_token_digest
                    or inventory.context_epoch != 0
                    or inventory.known_token_count == 0
                    or inventory.runtime_fingerprint != first_inventory.runtime_fingerprint
                ):
                    raise AgentControlError("shared initial prompt/cache mismatch")
            session.initial_state = first_inventory
            session.initialization = inventories
            await self.journal.publish(
                "session.initialized",
                conversation_id=conversation_id,
                data={key: value.model_dump() for key, value in inventories.items()},
            )
            async with self._lock:
                session.status = "RUNNING"
                session.updated_at = utc_now()
            await self.journal.publish("session.started", conversation_id=conversation_id, data={})
            first = next(
                agent for agent in agents if agent.registration.agent_id == chosen.first_speaker
            )
            await self._command_generate(first, session, turn_id=1, initial=True)
        except AgentControlError as exc:
            await self._fail_session(session, str(exc))
            raise
        return session.model_copy(deep=True)

    async def list_sessions(self) -> list[SessionView]:
        async with self._lock:
            return [
                session.model_copy(deep=True)
                for session in sorted(self._sessions.values(), key=lambda item: item.created_at)
            ]

    async def get_session(self, conversation_id: str) -> SessionView:
        async with self._lock:
            session = self._sessions.get(conversation_id)
            if session is None:
                raise NotFoundError(f"conversation '{conversation_id}' does not exist")
            return session.model_copy(deep=True)

    async def pause_session(self, conversation_id: str) -> SessionView:
        async with self._lock:
            session = self._require_session_unlocked(conversation_id)
            if session.status != "RUNNING":
                raise InvalidTransitionError(f"cannot pause a {session.status} conversation")
            session.status = "PAUSED"
            session.updated_at = utc_now()
        await self.journal.publish("session.paused", conversation_id=conversation_id, data={})
        return session.model_copy(deep=True)

    async def resume_session(self, conversation_id: str) -> SessionView:
        command: tuple[AgentRecord, int] | None = None
        async with self._lock:
            session = self._require_session_unlocked(conversation_id)
            if session.status != "PAUSED":
                raise InvalidTransitionError(f"cannot resume a {session.status} conversation")
            session.status = "RUNNING"
            session.updated_at = utc_now()
            if session.pending_transfer_id is None:
                next_turn = session.turn_count + 1
            else:
                next_turn = 0
        await self.journal.publish("session.resumed", conversation_id=conversation_id, data={})
        if next_turn:
            agent = await self.registry.get(session.current_speaker)
            command = (agent, next_turn)
        if command:
            await self._command_generate(command[0], session, command[1], initial=command[1] == 1)
        return session.model_copy(deep=True)

    async def stop_session(self, conversation_id: str) -> SessionView:
        async with self._lock:
            session = self._require_session_unlocked(conversation_id)
            if session.status not in ACTIVE_STATUSES:
                raise InvalidTransitionError(f"cannot stop a {session.status} conversation")
            session.status = "STOPPING" if session.pending_transfer_id else "STOPPED"
            session.finish_reason = "USER_STOPPED"
            session.updated_at = utc_now()
            agent_ids = self._agent_ids(session.config)
            should_command_stop = session.status == "STOPPED"
        # If a P2P transfer is already running, stop at its ACK boundary. Sending
        # an immediate stop to the peers could prevent that ACK and strand the
        # authoritative session forever in STOPPING.
        for agent_id in agent_ids if should_command_stop else []:
            try:
                agent = await self.registry.get(agent_id)
                await self._send(
                    agent,
                    AgentCommand(
                        command_id=f"{conversation_id}:stop:{agent_id}",
                        type="stop",
                        payload={
                            "conversation_id": conversation_id,
                            "reason": "user_requested",
                        },
                    ),
                )
            except (AgentControlError, NotFoundError):
                # Stop is best-effort; central authority has already prevented
                # further plans for this conversation.
                pass
        await self.journal.publish(
            "session.stopped",
            conversation_id=conversation_id,
            data={"pending_transfer": session.pending_transfer_id is not None},
        )
        return session.model_copy(deep=True)

    async def accept_state_offer(self, offer: StateOffer) -> TransferPlan | None:
        if contains_token_ids(offer.model_dump(mode="python")):
            raise ValueError("StateOffer must not contain token_ids")

        async with self._lock:
            if offer.offer_id in self._offer_results:
                prior = self._offer_results[offer.offer_id]
                return prior.model_copy(deep=True) if prior else None
            session = self._require_session_unlocked(offer.conversation_id)
            self._validate_offer_unlocked(session, offer)

            session.messages.append(
                MessageRecord(
                    output_token_count=offer.output_token_count,
                    turn_id=offer.turn_id,
                    agent_id=offer.source_agent,
                    text=offer.message_text or "",
                    state_id=offer.state_id,
                    token_count=offer.token_count,
                    created_at=offer.created_at,
                )
            )
            session.updated_at = utc_now()

        await self.journal.publish(
            "turn.generated",
            conversation_id=offer.conversation_id,
            turn_id=offer.turn_id,
            agent_id=offer.source_agent,
            data={
                "state_id": offer.state_id,
                "token_count": offer.token_count,
                "output_token_count": offer.output_token_count,
                "message_text": offer.message_text or "",
                "context_epoch": offer.context_epoch,
                "trimmed_tokens": offer.trimmed_tokens,
            },
        )

        # A final reply is useful to the user but has no downstream consumer,
        # so it must not produce a redundant P2P transfer.
        terminal_reason: str | None = None
        async with self._lock:
            session = self._require_session_unlocked(offer.conversation_id)
            self._validate_offer_unlocked(session, offer)
            if offer.terminal_reason is not None:
                terminal_reason = offer.terminal_reason
            elif offer.token_count >= session.config.max_context_tokens:
                terminal_reason = "CONTEXT_LIMIT"
            elif offer.turn_id >= session.config.max_agent_turns:
                terminal_reason = "MAX_TURNS"
            if terminal_reason:
                session.turn_count = offer.turn_id
                session.final_state_id = offer.state_id
                session.finish_reason = terminal_reason
                session.status = "COMPLETED"
                session.updated_at = utc_now()
                self._offer_results[offer.offer_id] = None
        if terminal_reason:
            await self.journal.publish(
                "session.completed",
                conversation_id=offer.conversation_id,
                turn_id=offer.turn_id,
                data={"reason": terminal_reason, "final_state_id": offer.state_id},
            )
            stop_reason = terminal_reason.lower()
            await self._best_effort_stop_agents(session, stop_reason)
            return None

        transfer_id = f"transfer-{uuid4().hex}"
        network = self.providers.network.snapshot(
            offer.source_agent, offer.target_agent, transfer_id
        )
        receiver = await self.registry.get(offer.target_agent)
        inventory = await self.control_client.cache_status(
            receiver.registration.control_url,
            offer.conversation_id,
        )
        await self.journal.publish(
            "cache.status",
            conversation_id=offer.conversation_id,
            turn_id=offer.turn_id,
            agent_id=offer.target_agent,
            data=inventory.model_dump(mode="json"),
        )
        known_states = {
            entry.state_id: (entry.token_count, entry.context_epoch, entry.target_digest)
            for entry in session.ledger
        }
        initial = session.initial_state
        if initial and initial.known_state_id:
            known_states[initial.known_state_id] = (
                initial.known_token_count,
                initial.context_epoch,
                initial.known_token_digest,
            )
        sender = await self.registry.get(offer.source_agent)
        descriptor = sender.registration.runtime_descriptor
        token_plan, kv_plan = plan_candidates(
            offer,
            inventory,
            network,
            transfer_id,
            known_states,
            kv_bytes_per_token=descriptor.kv_bytes_per_token,
            kv_container_bytes=descriptor.kv_container_bytes,
        )
        behavior = await self.model_behavior()
        history_tokens = offer.shared_prefix_tokens or 0
        plan, decision_record = decide(
            token_plan,
            kv_plan,
            session.config.decision,
            behavior,
            ModePolicy(session.config.mode_policy),
            history_tokens=history_tokens,
        )
        predicted = plan.predicted_p2p_ms
        reason = plan.selector_reason
        xi_tokens = decision_record.xi_tokens

        async with self._lock:
            session = self._require_session_unlocked(offer.conversation_id)
            # State may have changed while publishing the generated event or
            # sampling providers. Never install a plan after a concurrent stop.
            self._validate_offer_unlocked(session, offer)
            pending = PendingTransfer(plan=plan)
            self._pending[transfer_id] = pending
            session.pending_transfer_id = transfer_id
            session.transfers.append(
                TransferRecord(
                    transfer_id=transfer_id,
                    turn_id=offer.turn_id,
                    mode=plan.mode,
                    variant=plan.scope,
                    context_epoch=offer.context_epoch,
                    trimmed_tokens=offer.trimmed_tokens,
                    output_token_count=offer.output_token_count,
                    wrapper_token_count=offer.wrapper_token_count,
                    receiver_known_tokens=plan.receiver_known_tokens,
                    receiver_cache_tokens=plan.receiver_cache_tokens,
                    recovery_reason=plan.recovery_reason,
                    decision=decision_record,
                    base_state_id=plan.base_state_id,
                    start_token=plan.start_token,
                    xi_tokens=xi_tokens,
                    source_agent_id=offer.source_agent,
                    target_agent_id=offer.target_agent,
                    selector_reason=reason,
                    state_id=offer.state_id,
                    parent_state_id=offer.parent_state_id,
                    token_count=offer.token_count,
                    payload_bytes=plan.payload_bytes,
                    predicted_ms=predicted,
                    status="PLANNED",
                    network=network.model_dump(mode="json"),
                )
            )
            session.updated_at = utc_now()
            self._offer_results[offer.offer_id] = plan

        await self.journal.publish(
            "transfer.planned",
            conversation_id=offer.conversation_id,
            turn_id=offer.turn_id,
            transfer_id=transfer_id,
            data={
                "source_agent": offer.source_agent,
                "target_agent": offer.target_agent,
                "mode": plan.mode,
                "scope": plan.scope,
                "selector_reason": reason,
                "payload_bytes": plan.payload_bytes,
                "start_token": plan.start_token,
                "xi_tokens": xi_tokens,
                "predicted_p2p_ms": predicted,
                "network_snapshot_version": network.version,
            },
        )

        if decision_record is not None:
            await self.journal.publish(
                "transfer.decision",
                conversation_id=offer.conversation_id,
                turn_id=offer.turn_id,
                transfer_id=transfer_id,
                data=decision_record.model_dump(mode="json"),
            )

        receiver = await self.registry.get(offer.target_agent)
        sender = await self.registry.get(offer.source_agent)
        try:
            await self._send(
                receiver,
                AgentCommand(
                    command_id=f"{transfer_id}:prepare",
                    type="prepare_transfer",
                    payload={
                        "conversation_id": offer.conversation_id,
                        "plan": plan.model_dump(mode="json"),
                    },
                ),
            )
            async with self._lock:
                pending.stage = "PREPARED"
                self._transfer_record_unlocked(session, transfer_id).status = "PREPARED"
            await self.journal.publish(
                "transfer.prepare_requested",
                conversation_id=offer.conversation_id,
                turn_id=offer.turn_id,
                transfer_id=transfer_id,
                agent_id=offer.target_agent,
                data={},
            )
            await self._send(
                sender,
                AgentCommand(
                    command_id=f"{transfer_id}:execute",
                    type="execute_transfer",
                    payload={
                        "conversation_id": offer.conversation_id,
                        "plan": plan.model_dump(mode="json"),
                        "target_data_endpoint": receiver.registration.data_advertise,
                    },
                ),
            )
            async with self._lock:
                # An immediate receiver callback may already have removed the
                # pending entry while the execute request was in flight.
                current = self._pending.get(transfer_id)
                if current is not None:
                    current.stage = "EXECUTING"
                    self._transfer_record_unlocked(session, transfer_id).status = "RUNNING"
            await self.journal.publish(
                "transfer.execute_requested",
                conversation_id=offer.conversation_id,
                turn_id=offer.turn_id,
                transfer_id=transfer_id,
                agent_id=offer.source_agent,
                data={},
            )
        except AgentControlError as exc:
            await self._fail_session(session, str(exc), transfer_id=transfer_id)
            raise
        return plan.model_copy(deep=True)

    async def commit_transfer(self, ack: TransferCommitAck) -> SessionView:
        if contains_token_ids(ack.model_dump(mode="python")):
            raise ValueError("transfer ACK must not contain token_ids")
        generate_agent_id: str | None = None
        next_turn = 0
        cleanup_after_ack = False
        async with self._lock:
            cached = self._ack_results.get(ack.transfer_id)
            if cached is not None:
                cached_ack, cached_result = cached
                if ack.model_dump(mode="json") != cached_ack.model_dump(mode="json"):
                    raise ConflictError(f"transfer '{ack.transfer_id}' already has a different ACK")
                return cached_result.model_copy(deep=True)
            pending = self._pending.get(ack.transfer_id)
            if pending is None:
                raise NotFoundError(f"transfer '{ack.transfer_id}' is not pending")
            plan = pending.plan
            if ack.conversation_id != plan.conversation_id:
                raise ConflictError("ACK conversation_id does not match the transfer plan")
            if ack.receiver_agent != plan.target_agent:
                raise ConflictError("ACK was not sent by the planned receiver")
            if ack.state_id != plan.state_id:
                raise ConflictError("ACK state_id does not match the transfer plan")
            if ack.turn_id is not None and ack.turn_id != plan.turn_id:
                raise ConflictError("ACK turn_id does not match the transfer plan")
            if ack.token_count is not None and ack.token_count != plan.token_count:
                raise ConflictError("ACK token_count does not match the transfer plan")
            session = self._require_session_unlocked(plan.conversation_id)
            stop_requested = session.status == "STOPPING"
            if not ack.success:
                session.status = "ERROR"
                session.finish_reason = "TRANSFER_ERROR"
                session.updated_at = utc_now()
                del self._pending[ack.transfer_id]
                session.pending_transfer_id = None
                transfer_record = self._transfer_record_unlocked(session, ack.transfer_id)
                transfer_record.status = "FAILED"
                transfer_record.completed_at = utc_now()
                error_message = ack.error or "receiver rejected the transfer"
                cleanup_after_ack = stop_requested
            else:
                session.ledger.append(
                    LedgerEntry(
                        transfer_id=plan.transfer_id,
                        turn_id=plan.turn_id,
                        target_digest=plan.target_digest,
                        context_epoch=plan.context_epoch,
                        mode=plan.mode,
                        scope=plan.scope,
                        source_agent=plan.source_agent,
                        target_agent=plan.target_agent,
                        state_id=plan.state_id,
                        parent_state_id=plan.parent_state_id,
                        token_count=plan.token_count,
                        payload_bytes=plan.payload_bytes,
                    )
                )
                transfer_record = self._transfer_record_unlocked(session, ack.transfer_id)
                transfer_record.status = "COMMITTED"
                transfer_record.completed_at = utc_now()
                transfer_record.timings.update(ack.metrics)
                transfer_record.actual_ms = ack.metrics.get("p2p_ms")
                for phase, duration in sorted(ack.metrics.items()):
                    session.metrics.append(
                        MetricRecord(
                            phase=phase,
                            duration_ms=duration,
                            agent_id=ack.receiver_agent,
                            turn_id=plan.turn_id,
                            transfer_id=plan.transfer_id,
                        )
                    )
                session.turn_count = plan.turn_id
                session.current_speaker = plan.target_agent
                session.pending_transfer_id = None
                session.updated_at = utc_now()
                del self._pending[ack.transfer_id]
                error_message = None
                if session.status == "STOPPING":
                    session.status = "STOPPED"
                    cleanup_after_ack = True
                elif session.status == "RUNNING":
                    generate_agent_id = plan.target_agent
                    next_turn = plan.turn_id + 1
            ack_result = session.model_copy(deep=True)
            self._ack_results[ack.transfer_id] = (
                ack.model_copy(deep=True),
                ack_result.model_copy(deep=True),
            )

        if error_message:
            await self.journal.publish(
                "transfer.failed",
                conversation_id=ack.conversation_id,
                transfer_id=ack.transfer_id,
                agent_id=ack.receiver_agent,
                data={"message": error_message},
            )
            if cleanup_after_ack:
                await self._best_effort_stop_agents(session, "user_requested")
            return ack_result

        await self.journal.publish(
            "transfer.committed",
            conversation_id=plan.conversation_id,
            turn_id=plan.turn_id,
            transfer_id=plan.transfer_id,
            agent_id=plan.target_agent,
            data={
                "state_id": plan.state_id,
                "source_agent": plan.source_agent,
                "target_agent": plan.target_agent,
                "mode": plan.mode,
                "scope": plan.scope,
            },
        )
        if cleanup_after_ack:
            await self._best_effort_stop_agents(session, "user_requested")
        if generate_agent_id:
            agent = await self.registry.get(generate_agent_id)
            try:
                await self._command_generate(agent, session, next_turn, initial=False)
            except AgentControlError as exc:
                await self._fail_session(session, str(exc))
                raise
        return ack_result

    async def record_metric(self, report: MetricReport) -> MetricRecord:
        if contains_token_ids(report.model_dump(mode="python")):
            raise ValueError("metrics must not contain token_ids")
        record = MetricRecord(**report.model_dump(exclude={"conversation_id"}))
        async with self._lock:
            session = self._require_session_unlocked(report.conversation_id)
            session.metrics.append(record)
            if report.transfer_id and report.phase in {"handoff_e2e", "sender_prepare"}:
                transfer = self._transfer_record_unlocked(session, report.transfer_id)
                transfer.timings[f"{report.phase}_ms"] = report.duration_ms
            session.updated_at = utc_now()
        await self.journal.publish(
            "metric.recorded",
            conversation_id=report.conversation_id,
            turn_id=report.turn_id,
            transfer_id=report.transfer_id,
            agent_id=report.agent_id,
            data={"phase": report.phase, "duration_ms": report.duration_ms, **report.values},
        )
        return record

    async def record_error(self, report: AgentErrorReport) -> dict[str, str]:
        if contains_token_ids(report.model_dump(mode="python")):
            raise ValueError("error reports must not contain token_ids")
        completed_session: SessionView | None = None
        ignored_reason: str | None = None
        if report.conversation_id:
            async with self._lock:
                session = self._require_session_unlocked(report.conversation_id)
                processed_ack = (
                    self._ack_results.get(report.transfer_id) if report.transfer_id else None
                )
                context_limit = "CONTEXT_LIMIT" in (f"{report.code or ''} {report.message}".upper())
                if processed_ack is not None and processed_ack[0].success:
                    ignored_reason = "transfer_already_committed"
                elif (
                    context_limit
                    and session.status in {"STARTING", "RUNNING", "PAUSED"}
                    and session.pending_transfer_id is None
                ):
                    session.status = "COMPLETED"
                    session.finish_reason = "CONTEXT_LIMIT"
                    session.final_state_id = session.ledger[-1].state_id if session.ledger else None
                    session.updated_at = utc_now()
                    completed_session = session.model_copy(deep=True)
                elif session.status in {"STARTING", "RUNNING", "PAUSED"}:
                    session.status = "ERROR"
                    session.finish_reason = "AGENT_ERROR"
                    session.updated_at = utc_now()
                else:
                    ignored_reason = f"session_already_{session.status.lower()}"
        if completed_session is not None:
            await self.journal.publish(
                "session.completed",
                conversation_id=report.conversation_id,
                turn_id=report.turn_id,
                agent_id=report.agent_id,
                data={"reason": "CONTEXT_LIMIT"},
            )
            await self._best_effort_stop_agents(completed_session, "context_limit")
            return {"status": "completed"}
        await self.journal.publish(
            "agent.error_ignored" if ignored_reason else "agent.error",
            conversation_id=report.conversation_id,
            turn_id=report.turn_id,
            transfer_id=report.transfer_id,
            agent_id=report.agent_id,
            data={
                "phase": report.phase,
                "message": report.message,
                "ignored_reason": ignored_reason,
            },
        )
        return {"status": "ignored" if ignored_reason else "recorded"}

    async def bootstrap(self) -> dict[str, Any]:
        config, agents, sessions, environment = await asyncio.gather(
            self.get_config(), self.registry.list(), self.list_sessions(), self.environment()
        )
        events, _ = await self.journal.snapshot(max(0, self.journal.latest_seq - 199))
        active = next((session for session in sessions if session.status in ACTIVE_STATUSES), None)
        public_config = config.config.model_dump(mode="json")
        public_config["version"] = config.version
        result: dict[str, Any] = {
            "capabilities": self.capabilities().model_dump(mode="json"),
            "config": public_config,
            "config_version": config.version,
            "agents": [agent.model_dump(mode="json") for agent in agents],
            "sessions": [session.model_dump(mode="json") for session in sessions],
            "active_session": active.model_dump(mode="json") if active else None,
            "environment": environment.model_dump(mode="json"),
            "latest_seq": self.journal.latest_seq,
            "events": [public_event(event) for event in events],
        }
        if self.providers.network.contributes_mock_ui:
            result["mock"] = (await self.mock_config()).model_dump(mode="json")
        return result

    async def experiments_csv(self) -> str:
        sessions = await self.list_sessions()
        output = io.StringIO(newline="")
        fields = [
            "conversation_id",
            "turn_id",
            "transfer_id",
            "source_agent",
            "target_agent",
            "mode",
            "scope",
            "token_count",
            "payload_bytes",
            "phase",
            "duration_ms",
            "protocol_version",
            "alpha_tokens",
            "wrapper_tokens",
            "theta_tokens",
            "receiver_known_tokens",
            "receiver_cache_tokens",
            "effective_objective",
            "paper_token_bits",
            "paper_kv_bits",
            "paper_token_ms",
            "paper_kv_ms",
            "recovery_reason",
            "decision_objective",
            "token_cost_ms",
            "kv_cost_ms",
            "selector_reason",
            "context_epoch",
            "trimmed_tokens",
            "xi_tokens",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for session in sessions:
            metrics_by_transfer: dict[str, list[MetricRecord]] = {}
            for metric in session.metrics:
                if metric.transfer_id:
                    metrics_by_transfer.setdefault(metric.transfer_id, []).append(metric)
            for entry in session.ledger:
                transfer = self._transfer_record_unlocked(session, entry.transfer_id)
                decision = transfer.decision
                found_metrics = metrics_by_transfer.get(entry.transfer_id)
                transfer_metrics: list[MetricRecord | None] = (
                    list(found_metrics) if found_metrics else [None]
                )
                for transfer_metric in transfer_metrics:
                    writer.writerow(
                        {
                            "protocol_version": session.protocol_version,
                            "alpha_tokens": transfer.output_token_count,
                            "wrapper_tokens": transfer.wrapper_token_count,
                            "theta_tokens": decision.theta_tokens if decision else "",
                            "receiver_known_tokens": transfer.receiver_known_tokens,
                            "receiver_cache_tokens": transfer.receiver_cache_tokens,
                            "effective_objective": decision.effective_objective if decision else "",
                            "paper_token_bits": decision.candidates[0].paper_bits
                            if decision
                            else "",
                            "paper_kv_bits": decision.candidates[1].paper_bits if decision else "",
                            "paper_token_ms": decision.candidates[0].paper_ms if decision else "",
                            "paper_kv_ms": decision.candidates[1].paper_ms if decision else "",
                            "recovery_reason": transfer.recovery_reason or "",
                            "decision_objective": decision.objective if decision else "",
                            "token_cost_ms": decision.candidates[0].objective_ms
                            if decision
                            else "",
                            "kv_cost_ms": decision.candidates[1].objective_ms if decision else "",
                            "selector_reason": transfer.selector_reason,
                            "context_epoch": transfer.context_epoch,
                            "trimmed_tokens": transfer.trimmed_tokens,
                            "xi_tokens": transfer.xi_tokens,
                            "conversation_id": session.conversation_id,
                            "turn_id": entry.turn_id,
                            "transfer_id": entry.transfer_id,
                            "source_agent": entry.source_agent,
                            "target_agent": entry.target_agent,
                            "mode": entry.mode,
                            "scope": entry.scope,
                            "token_count": entry.token_count,
                            "payload_bytes": entry.payload_bytes,
                            "phase": transfer_metric.phase if transfer_metric else "",
                            "duration_ms": (transfer_metric.duration_ms if transfer_metric else ""),
                        }
                    )
        return output.getvalue()

    async def _command_generate(
        self, agent: AgentRecord, session: SessionView, turn_id: int, initial: bool
    ) -> None:
        target_agent = self._other_agent(session.config, agent.registration.agent_id)
        profile = (
            session.config.agent_a_profile
            if session.config.agent_a_profile.agent_id == agent.registration.agent_id
            else session.config.agent_b_profile
        )
        await self._send(
            agent,
            AgentCommand(
                command_id=f"{session.conversation_id}:generate:{turn_id}",
                type="generate",
                payload={
                    "conversation_id": session.conversation_id,
                    "turn_id": turn_id,
                    "initial": initial,
                    "target_agent": target_agent,
                    "profile": profile.model_dump(mode="json"),
                    "local_context": "",
                },
            ),
        )
        await self.journal.publish(
            "turn.generate_requested",
            conversation_id=session.conversation_id,
            turn_id=turn_id,
            agent_id=agent.registration.agent_id,
            data={"initial": initial},
        )

    async def _send(self, agent: AgentRecord, command: AgentCommand) -> None:
        await self.control_client.send(str(agent.registration.control_url), command)

    async def _best_effort_stop_agents(self, session: SessionView, reason: str) -> None:
        for agent_id in self._agent_ids(session.config):
            try:
                agent = await self.registry.get(agent_id)
                await self._send(
                    agent,
                    AgentCommand(
                        command_id=f"{session.conversation_id}:stop:{reason}:{agent_id}",
                        type="stop",
                        payload={
                            "conversation_id": session.conversation_id,
                            "reason": reason,
                        },
                    ),
                )
            except (AgentControlError, NotFoundError):
                pass

    async def _fail_session(
        self, session: SessionView, message: str, transfer_id: str | None = None
    ) -> None:
        async with self._lock:
            session.status = "ERROR"
            session.finish_reason = "CONTROL_ERROR"
            session.updated_at = utc_now()
            if transfer_id:
                self._pending.pop(transfer_id, None)
                session.pending_transfer_id = None
                transfer_record = self._transfer_record_unlocked(session, transfer_id)
                transfer_record.status = "FAILED"
                transfer_record.completed_at = utc_now()
        await self.journal.publish(
            "session.error",
            conversation_id=session.conversation_id,
            transfer_id=transfer_id,
            data={"message": message},
        )

    def _active_session_unlocked(self) -> SessionView | None:
        return next(
            (session for session in self._sessions.values() if session.status in ACTIVE_STATUSES),
            None,
        )

    def _require_session_unlocked(self, conversation_id: str) -> SessionView:
        session = self._sessions.get(conversation_id)
        if session is None:
            raise NotFoundError(f"conversation '{conversation_id}' does not exist")
        return session

    def _validate_offer_unlocked(self, session: SessionView, offer: StateOffer) -> None:
        if session.status not in {"RUNNING", "PAUSED"}:
            raise InvalidTransitionError(
                f"cannot accept an offer for a {session.status} conversation"
            )
        expected_turn = session.turn_count + 1
        if offer.turn_id != expected_turn:
            raise ConflictError(f"expected turn {expected_turn}, received turn {offer.turn_id}")
        if offer.source_agent != session.current_speaker:
            raise ConflictError(
                f"current speaker is '{session.current_speaker}', not '{offer.source_agent}'"
            )
        expected_target = self._other_agent(session.config, offer.source_agent)
        if offer.target_agent != expected_target:
            raise ConflictError(f"expected target '{expected_target}'")
        if session.pending_transfer_id is not None:
            raise ConflictError("the conversation already has a pending transfer")
        initial = session.initial_state
        if initial is None:
            raise ConflictError("shared initial state is not established")
        if offer.runtime_fingerprint != initial.runtime_fingerprint:
            raise ConflictError("offer runtime mismatch")
        if (
            offer.shared_prefix_tokens is None
            or offer.shared_prefix_tokens + offer.output_token_count + offer.wrapper_token_count
            != offer.token_count
        ):
            raise ConflictError("output and wrapper counts do not match target range")
        expected_parent = session.ledger[-1].state_id if session.ledger else initial.known_state_id
        if offer.parent_state_id != expected_parent:
            raise ConflictError(f"expected parent_state_id '{expected_parent}'")
        expected_digest = (
            session.ledger[-1].target_digest if session.ledger else initial.known_token_digest
        )
        if not offer.trimmed_tokens and offer.shared_prefix_digest != expected_digest:
            raise ConflictError("offered prefix differs from the canonical parent")
        previous_epoch = session.ledger[-1].context_epoch if session.ledger else 0
        if offer.context_epoch != previous_epoch + int(offer.trimmed_tokens > 0):
            raise ConflictError("offer epoch must advance exactly once for a context rebase")
        if (
            offer.shared_prefix_tokens is not None
            and offer.shared_prefix_tokens > offer.token_count
        ):
            raise ConflictError("shared prefix exceeds target state")
        if offer.turn_id > 1:
            if not session.ledger:
                raise ConflictError("a later turn requires a committed parent state")
            if (
                offer.shared_prefix_tokens is not None
                and offer.shared_prefix_tokens + offer.trimmed_tokens
                != session.ledger[-1].token_count
            ):
                raise ConflictError("retained and trimmed history must match the committed parent")
            expected_parent = session.ledger[-1].state_id
            if offer.parent_state_id != expected_parent:
                raise ConflictError(
                    f"expected parent_state_id '{expected_parent}', "
                    f"received '{offer.parent_state_id}'"
                )

    @staticmethod
    def _transfer_record_unlocked(session: SessionView, transfer_id: str) -> TransferRecord:
        record = next(
            (item for item in session.transfers if item.transfer_id == transfer_id),
            None,
        )
        if record is None:
            raise NotFoundError(f"transfer '{transfer_id}' has no session record")
        return record

    @staticmethod
    def _agent_ids(config: Any) -> list[str]:
        return [config.agent_a_profile.agent_id, config.agent_b_profile.agent_id]

    @staticmethod
    def _other_agent(config: Any, current: str) -> str:
        ids = CoordinatorService._agent_ids(config)
        if current not in ids:
            raise ConflictError(f"agent '{current}' is not part of this conversation")
        return ids[1] if current == ids[0] else ids[0]

    @staticmethod
    def _validate_session_config(config: Any) -> None:
        ids = CoordinatorService._agent_ids(config)
        if len(set(ids)) != 2:
            raise ConflictError("a conversation requires two distinct Agent IDs")
        if config.first_speaker not in ids:
            raise ConflictError("first_speaker must match one of the two Agent IDs")
        if _enum_value(config.mode_policy) not in {"ALWAYS_TOKEN", "ALWAYS_KV", "ADAPTIVE"}:
            raise ConflictError("unsupported mode policy")

    @staticmethod
    def _validate_runtime_match(agents: list[AgentRecord]) -> None:
        descriptors = [_normalized(agent.registration.runtime_descriptor) for agent in agents]
        if descriptors[0] != descriptors[1]:
            raise ConflictError("the two Agents report different RuntimeDescriptor values")

    @staticmethod
    def _validate_transport_capabilities(agents: list[AgentRecord]) -> None:
        for agent in agents:
            capabilities = {
                _enum_value(capability) for capability in agent.registration.transport_capabilities
            }
            if "TOKEN_DELTA_V2" not in capabilities:
                raise ConflictError(
                    f"agent '{agent.registration.agent_id}' "
                    "does not support TOKEN/DELTA protocol v2"
                )
