"""Agent orchestration, model calls, and P2P receive/send lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from shared.contracts import (
    AgentErrorReport,
    AgentLifecycleStatus,
    AgentProfile,
    AgentRegistration,
    CreateSessionRequest,
    DecisionSettings,
    ExecuteTransferCommand,
    GenerateCommand,
    GenerateRequest,
    Heartbeat,
    OpenSessionCommand,
    PrefillRequest,
    PrepareTransferCommand,
    RuntimeDescriptor,
    SealRequest,
    StateOffer,
    TokenizeRequest,
    TransferManifest,
    TransferPlan,
)
from shared.interfaces import DataPlaneTransport, ModelRuntimeClient
from shared.kv import CacheInventory, CacheStatusRequest, prefix_digest, runtime_fingerprint

from .config import AgentSettings
from .coordinator_client import CoordinatorClient, CoordinatorClientError
from .handlers import AgentTransferHandler, KvCacheHandler, TokenMessageHandler
from .model_client import HttpModelRuntimeClient
from .transport import TcpByteTransport, WireReceipt


class AgentRuntimeError(RuntimeError):
    pass


@dataclass(slots=True)
class PeerSession:
    conversation_id: str
    peer_agent_id: str
    profile: AgentProfile
    model_session_id: str
    max_context_tokens: int
    stop_on_eos: bool
    initial_input: str
    is_first_speaker: bool
    shared_prompt: str = ""
    initialization_ms: float = 0
    decision: DecisionSettings = field(default_factory=DecisionSettings)
    state_id: str | None = None
    parent_state_id: str | None = None
    token_ids: list[int] = field(default_factory=list)
    states: dict[str, list[int]] = field(default_factory=dict)
    stopped: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AgentRuntime:
    def __init__(
        self,
        settings: AgentSettings,
        *,
        coordinator: CoordinatorClient | None = None,
        model: ModelRuntimeClient | None = None,
        transport: DataPlaneTransport | None = None,
        handlers: dict[tuple[str, str], AgentTransferHandler] | None = None,
    ) -> None:
        self.settings = settings
        self.coordinator = coordinator or CoordinatorClient(
            settings.coordinator_url,
            timeout_seconds=settings.request_timeout_seconds,
        )
        self.model = model or HttpModelRuntimeClient(
            settings.model_service_url,
            timeout_seconds=settings.request_timeout_seconds,
        )
        self.handlers = handlers or {
            ("TOKEN", "DELTA"): TokenMessageHandler(),
            ("TOKEN", "RECOVERY"): TokenMessageHandler("RECOVERY"),
            ("KV", "FULL"): KvCacheHandler("FULL"),
            ("KV", "DELTA"): KvCacheHandler("DELTA"),
        }
        self.transport = transport or TcpByteTransport(
            chunk_bytes=settings.transfer_chunk_bytes,
            timeout_seconds=settings.request_timeout_seconds,
        )
        self.runtime_descriptor: RuntimeDescriptor | None = None
        self.sessions: dict[str, PeerSession] = {}
        self.pending_transfers: dict[str, TransferPlan] = {}
        self.committed_transfers: set[str] = set()
        self.received_transfers: dict[str, tuple[TransferManifest, str, dict[str, Any]]] = {}
        self.receive_locks: dict[str, asyncio.Lock] = {}
        self.command_results: dict[str, dict[str, Any]] = {}
        self.command_tasks: set[asyncio.Task[Any]] = set()
        self.heartbeat_task: asyncio.Task[None] | None = None
        self.tcp_server: asyncio.AbstractServer | None = None
        self.status = AgentLifecycleStatus.STARTING
        self.phase = "STARTING"
        self.current_conversation_id: str | None = None
        self.registered = False

    async def start(self) -> None:
        self.runtime_descriptor = await self.model.describe()
        if "shared_context_v2" not in self.runtime_descriptor.capabilities:
            raise AgentRuntimeError("model service does not support shared-context v2")
        self.tcp_server = await asyncio.start_server(
            self._receive_connection,
            self.settings.data_bind_host,
            self.settings.data_bind_port,
        )
        self.status = AgentLifecycleStatus.READY
        self.phase = "IDLE"
        await self._register_until_ready()
        self.heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"heartbeat:{self.settings.agent_id}",
        )

    async def stop(self) -> None:
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            await asyncio.gather(self.heartbeat_task, return_exceptions=True)
        for task in list(self.command_tasks):
            task.cancel()
        if self.command_tasks:
            await asyncio.gather(*self.command_tasks, return_exceptions=True)
        if self.tcp_server:
            self.tcp_server.close()
            await self.tcp_server.wait_closed()
        await self.model.close()
        await self.coordinator.close()

    async def enqueue_command(
        self,
        command_id: str,
        command_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        previous = self.command_results.get(command_id)
        if previous is not None:
            return previous.copy()
        result = {
            "command_id": command_id,
            "accepted": True,
            "status": "queued",
            "detail": None,
        }
        self.command_results[command_id] = result
        if command_type in {"open_session", "prepare_transfer", "stop", "evict_cache", "set_model"}:
            await self._run_command(command_id, command_type, payload)
            return self.command_results[command_id].copy()
        task = asyncio.create_task(
            self._run_command(command_id, command_type, payload),
            name=f"command:{self.settings.agent_id}:{command_id}",
        )
        self.command_tasks.add(task)
        task.add_done_callback(self.command_tasks.discard)
        return result.copy()

    async def _run_command(
        self,
        command_id: str,
        command_type: str,
        payload: dict[str, Any],
    ) -> None:
        result = self.command_results[command_id]
        result["status"] = "running"
        try:
            raw = {"command_id": command_id, "command_type": command_type, **payload}
            if command_type == "set_model":
                await self.set_model(str(payload["url"]))
            elif command_type == "open_session":
                await self.open_session(OpenSessionCommand.model_validate(raw))
            elif command_type == "generate":
                await self.generate(GenerateCommand.model_validate(raw))
            elif command_type == "prepare_transfer":
                await self.prepare_transfer(PrepareTransferCommand.model_validate(raw))
            elif command_type == "execute_transfer":
                await self.execute_transfer(ExecuteTransferCommand.model_validate(raw))
            elif command_type == "evict_cache":
                session = self._require_session(str(payload["conversation_id"]))
                request = CacheStatusRequest(
                    session_id=session.model_session_id,
                    retain_prefix_tokens=payload.get("retain_prefix_tokens", 0),
                )
                if set(payload) - {"conversation_id", "retain_prefix_tokens"}:
                    raise AgentRuntimeError("unknown cache eviction fields")
                async with session.lock:
                    await self.model.evict_cache(request)
            elif command_type == "stop":
                await self.stop_session(str(payload["conversation_id"]))
            else:
                raise AgentRuntimeError(f"unsupported command type: {command_type}")
            result["status"] = "completed"
        except asyncio.CancelledError:
            result["status"] = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - boundary must report any command failure
            result["accepted"] = False
            result["status"] = "failed"
            result["detail"] = str(exc)
            await self._report_error(
                code="COMMAND_FAILED",
                detail=str(exc),
                phase=command_type,
                conversation_id=str(payload.get("conversation_id") or "") or None,
                turn_id=payload.get("turn_id"),
                transfer_id=(payload.get("plan") or {}).get("transfer_id"),
            )

    async def set_model(self, url: str) -> None:
        if self.current_conversation_id is not None or self.command_tasks:
            raise AgentRuntimeError("Agent 正在执行会话，不能切换模型")
        candidate = HttpModelRuntimeClient(
            url, timeout_seconds=self.settings.request_timeout_seconds
        )
        previous, descriptor = self.model, self.runtime_descriptor
        try:
            next_descriptor = await candidate.describe()
            if "shared_context_v2" not in next_descriptor.capabilities:
                raise AgentRuntimeError("模型不支持共享上下文协议")
            self.model, self.runtime_descriptor = candidate, next_descriptor
            await self._register_until_ready()
        except BaseException:
            self.model, self.runtime_descriptor = previous, descriptor
            await candidate.close()
            raise
        self.sessions.clear()
        self.pending_transfers.clear()
        self.received_transfers.clear()
        self.receive_locks.clear()
        await previous.close()

    async def open_session(self, command: OpenSessionCommand) -> None:
        existing = self.sessions.get(command.conversation_id)
        if existing is not None:
            same_command = (
                existing.decision == command.decision
                and existing.profile == command.profile
                and existing.peer_agent_id == command.peer_agent_id
                and existing.initial_input == command.initial_input
                and existing.shared_prompt == command.shared_prompt
                and existing.max_context_tokens == command.max_context_tokens
                and existing.stop_on_eos == command.stop_on_eos
                and existing.is_first_speaker == command.is_first_speaker
            )
            if not same_command:
                raise AgentRuntimeError("open_session differs from the existing session")
            return
        if command.profile.agent_id != self.settings.agent_id:
            raise AgentRuntimeError("profile agent_id does not match this Agent")

        initialization_started = perf_counter()
        response = await self.model.create_session(
            CreateSessionRequest(
                agent_id=self.settings.agent_id,
                conversation_id=command.conversation_id,
            )
        )
        session = PeerSession(
            conversation_id=command.conversation_id,
            peer_agent_id=command.peer_agent_id,
            profile=command.profile,
            model_session_id=response.session.session_id,
            max_context_tokens=command.max_context_tokens,
            stop_on_eos=command.stop_on_eos,
            initial_input=command.initial_input,
            is_first_speaker=command.is_first_speaker,
            decision=command.decision,
            shared_prompt=command.shared_prompt,
        )
        if not command.shared_prompt:
            raise AgentRuntimeError("shared-context v2 requires a common initial prompt")
        tokenized = await self.model.tokenize(
            TokenizeRequest(text=command.shared_prompt, add_bos=True)
        )
        if tokenized.token_count > command.max_context_tokens:
            raise AgentRuntimeError("CONTEXT_LIMIT: initial task exceeds max_context_tokens")
        initial_state_id = f"{command.conversation_id}:initial"
        prefilled = await self.model.prefill(
            PrefillRequest(
                session_id=session.model_session_id,
                token_ids=tokenized.token_ids,
                state_id=initial_state_id,
                replace=True,
                pinned_prefix_tokens=tokenized.token_count,
            )
        )
        session.token_ids = tokenized.token_ids.copy()
        session.state_id = prefilled.state.state_id
        session.states[prefilled.state.state_id] = session.token_ids.copy()
        session.initialization_ms = (perf_counter() - initialization_started) * 1000
        self.sessions[command.conversation_id] = session
        self.current_conversation_id = command.conversation_id
        self.status = AgentLifecycleStatus.BUSY
        self.phase = "SESSION_OPEN"

    async def generate(self, command: GenerateCommand) -> None:
        session = self._require_session(command.conversation_id)
        async with session.lock:
            if session.stopped:
                return
            self.phase = "GENERATING"
            generated = await self.model.generate(
                GenerateRequest(
                    history_window_tokens=session.decision.history_window_tokens,
                    session_id=session.model_session_id,
                    turn_id=command.turn_id,
                    profile=command.profile,
                    local_context=command.local_context,
                    generation_config=command.profile.generation_config,
                )
            )
            sealed = await self.model.seal(
                SealRequest(
                    session_id=session.model_session_id,
                    state_id=generated.state_id,
                    expected_token_count=generated.token_count,
                )
            )
            if not sealed.state.sealed:
                raise AgentRuntimeError("model service did not seal the generated state")
            session.parent_state_id = generated.parent_state_id
            session.state_id = generated.state_id
            session.token_ids = generated.token_ids.copy()
            session.states[generated.state_id] = generated.token_ids.copy()

        if session.stopped:
            return
        await self._post_metric(
            conversation_id=command.conversation_id,
            phase="local_prefill",
            duration_ms=generated.timings.prefill_ms,
            turn_id=command.turn_id,
        )
        if session.stopped:
            return
        await self._post_metric(
            conversation_id=command.conversation_id,
            phase="generation",
            duration_ms=generated.timings.generate_ms,
            turn_id=command.turn_id,
        )
        if session.stopped:
            return
        if generated.timings.context_rebuild_ms:
            await self._post_metric(
                conversation_id=command.conversation_id,
                phase="context_rebuild",
                duration_ms=generated.timings.context_rebuild_ms,
                turn_id=command.turn_id,
                values={
                    "trimmed_tokens": generated.trimmed_tokens,
                    "context_epoch": generated.context_epoch,
                },
            )
        self.phase = "OFFERING"
        offer = StateOffer(
            context_epoch=generated.context_epoch,
            shared_prefix_tokens=generated.shared_prefix_tokens,
            trimmed_tokens=generated.trimmed_tokens,
            conversation_id=command.conversation_id,
            turn_id=command.turn_id,
            source_agent=self.settings.agent_id,
            target_agent=command.target_agent,
            state_id=generated.state_id,
            parent_state_id=generated.parent_state_id,
            token_count=generated.token_count,
            payload_bytes=(generated.token_count - generated.shared_prefix_tokens) * 4,
            output_token_count=len(generated.generated_token_ids),
            wrapper_token_count=generated.token_count
            - generated.shared_prefix_tokens
            - len(generated.generated_token_ids),
            target_digest=prefix_digest(generated.token_ids),
            shared_prefix_digest=prefix_digest(
                generated.token_ids[: generated.shared_prefix_tokens]
            ),
            runtime_fingerprint=runtime_fingerprint(
                self.runtime_descriptor or await self.model.describe()
            ),
            message_text=generated.text,
            terminal_reason=(
                "EOS"
                if generated.eos and session.stop_on_eos
                else "CONTEXT_LIMIT"
                if generated.token_count >= session.max_context_tokens
                else None
            ),
            candidates=[],
        )
        try:
            response = await self.coordinator.offer(offer)
        except CoordinatorClientError:
            if session.stopped:
                return
            raise
        if response.get("status") == "completed":
            self.phase = "IDLE"
            self.status = AgentLifecycleStatus.READY
            self.current_conversation_id = None
        else:
            self.phase = "AWAITING_TRANSFER"

    async def prepare_transfer(self, command: PrepareTransferCommand) -> None:
        plan = command.plan
        if command.conversation_id != plan.conversation_id:
            raise AgentRuntimeError("prepare_transfer conversation does not match its plan")
        if plan.target_agent != self.settings.agent_id:
            raise AgentRuntimeError("prepare_transfer targets a different Agent")
        self._handler(plan.mode, plan.scope)
        if plan.protocol_version != "2":
            raise AgentRuntimeError("incompatible transfer protocol")
        session = self._require_session(command.conversation_id)
        if session.stopped:
            raise AgentRuntimeError("session is stopped")
        previous = self.pending_transfers.get(plan.transfer_id)
        if previous and previous != plan:
            raise AgentRuntimeError("transfer_id is already bound to a different plan")
        self.pending_transfers[plan.transfer_id] = plan
        self.phase = "TRANSFER_PREPARED"

    async def execute_transfer(self, command: ExecuteTransferCommand) -> None:
        plan = command.plan
        if command.conversation_id != plan.conversation_id:
            raise AgentRuntimeError("execute_transfer conversation does not match its plan")
        if plan.source_agent != self.settings.agent_id:
            raise AgentRuntimeError("execute_transfer source is a different Agent")
        session = self._require_session(command.conversation_id)
        token_ids = session.states.get(plan.state_id)
        if token_ids is None:
            raise AgentRuntimeError("planned state is not resident on the sender")
        if len(token_ids) != plan.token_count:
            raise AgentRuntimeError("planned token_count differs from resident state")
        self.phase = "TRANSFERRING"
        prepare_started = perf_counter()
        handler = self._handler(plan.mode, plan.scope)
        if isinstance(handler, KvCacheHandler):
            payload = await handler.export(
                self.model,
                session.model_session_id,
                TransferManifest.from_plan(plan),
            )
        else:
            payload = handler.prepare(token_ids[plan.start_token :])
        payload_prepare_ms = (perf_counter() - prepare_started) * 1000
        result = await self.transport.send(
            command.target_data_endpoint,
            TransferManifest.from_plan(plan),
            payload,
            plan.network_state,
        )
        await self._post_metric(
            conversation_id=command.conversation_id,
            phase="sender_prepare",
            duration_ms=result.sender_prepare_ms + payload_prepare_ms,
            turn_id=plan.turn_id,
            transfer_id=plan.transfer_id,
            values={"frame_bytes": result.frame_bytes},
        )
        await self._post_metric(
            conversation_id=command.conversation_id,
            phase="sender_p2p_elapsed",
            duration_ms=result.sender_elapsed_ms,
            turn_id=plan.turn_id,
            transfer_id=plan.transfer_id,
            values={"receiver_wire_ms": result.receiver_wire_ms},
        )
        await self._post_metric(
            conversation_id=command.conversation_id,
            phase="handoff_e2e",
            duration_ms=payload_prepare_ms + result.sender_prepare_ms + result.sender_elapsed_ms,
            turn_id=plan.turn_id,
            transfer_id=plan.transfer_id,
        )
        self.phase = "AWAITING_COMMIT"

    async def cache_status(self, conversation_id: str) -> CacheInventory:
        session = self._require_session(conversation_id)
        inventory = await self.model.cache_status(
            CacheStatusRequest(session_id=session.model_session_id)
        )
        return inventory.model_copy(update={"initialization_ms": session.initialization_ms})

    async def stop_session(self, conversation_id: str) -> None:
        session = self.sessions.get(conversation_id)
        if session:
            session.stopped = True
        self.pending_transfers = {
            transfer_id: plan
            for transfer_id, plan in self.pending_transfers.items()
            if plan.conversation_id != conversation_id
        }
        if self.current_conversation_id == conversation_id:
            self.current_conversation_id = None
            self.status = AgentLifecycleStatus.READY
            self.phase = "IDLE"

    async def _receive_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        manifest: TransferManifest | None = None
        wire_ms = 0.0
        receipt_sent = False
        receive_lock: asyncio.Lock | None = None
        receive_lock_acquired = False
        try:
            manifest, payload, wire_ms = await self.transport.receive(reader)
            receive_lock = self.receive_locks.setdefault(manifest.transfer_id, asyncio.Lock())
            await receive_lock.acquire()
            receive_lock_acquired = True
            previous = self.received_transfers.get(manifest.transfer_id)
            if previous:
                if previous[0] != manifest or previous[1] != hashlib.sha256(payload).hexdigest():
                    raise AgentRuntimeError("transfer replay differs from the accepted payload")
                await self.transport.write_receipt(
                    writer,
                    WireReceipt(
                        transfer_id=manifest.transfer_id,
                        accepted=True,
                        receiver_wire_ms=wire_ms,
                    ),
                )
                receipt_sent = True
                await self._commit_ack(manifest.transfer_id, previous[2])
                self.committed_transfers.add(manifest.transfer_id)
                self.pending_transfers.pop(manifest.transfer_id, None)
                if self.phase == "COMMIT_ACK_UNKNOWN":
                    self.phase = "READY_TO_GENERATE"
                return
            handler = self._handler(manifest.mode, manifest.scope)
            token_ids = handler.decode(manifest, payload)
            plan = self.pending_transfers.get(manifest.transfer_id)
            if manifest.start_token:
                session = self._require_session(manifest.conversation_id)
                if (
                    session.state_id != manifest.base_state_id
                    or len(session.token_ids) < manifest.start_token
                    or prefix_digest(session.token_ids[: manifest.start_token])
                    != manifest.base_digest
                ):
                    raise AgentRuntimeError("KV base changed after CACHE_STATUS")
                token_ids = session.token_ids[: manifest.start_token] + token_ids
            self._validate_incoming(manifest, token_ids, plan)
            if prefix_digest(token_ids) != manifest.target_digest:
                raise AgentRuntimeError("wire target digest mismatch")
            if manifest.runtime_fingerprint != runtime_fingerprint(
                self.runtime_descriptor or await self.model.describe()
            ):
                raise AgentRuntimeError("wire runtime fingerprint mismatch")
            assert plan is not None
            session = self._require_session(manifest.conversation_id)
            self.phase = "RESTORING"
            prefill_started = perf_counter()
            restored = await handler.restore(
                self.model,
                session.model_session_id,
                manifest,
                payload,
                token_ids,
            )
            receiver_prefill_ms = (perf_counter() - prefill_started) * 1000
            if restored.state.token_count != manifest.token_count:
                raise AgentRuntimeError("restored model state has the wrong token length")
            async with session.lock:
                session.parent_state_id = manifest.parent_state_id
                session.state_id = manifest.state_id
                session.token_ids = token_ids.copy()
                session.states[manifest.state_id] = token_ids.copy()
            ack_body = {
                "transfer_id": manifest.transfer_id,
                "conversation_id": manifest.conversation_id,
                "receiver_agent": self.settings.agent_id,
                "state_id": manifest.state_id,
                "turn_id": manifest.turn_id,
                "token_count": manifest.token_count,
                "success": True,
                "metrics": {
                    "p2p_ms": wire_ms,
                    (
                        "receiver_import_ms" if manifest.mode == "KV" else "receiver_prefill_ms"
                    ): receiver_prefill_ms,
                },
            }
            self.received_transfers[manifest.transfer_id] = (
                manifest.model_copy(deep=True),
                hashlib.sha256(payload).hexdigest(),
                ack_body,
            )
            self.phase = "COMMITTING"
            await self.transport.write_receipt(
                writer,
                WireReceipt(
                    transfer_id=manifest.transfer_id,
                    accepted=True,
                    receiver_wire_ms=wire_ms,
                ),
            )
            receipt_sent = True
            await self._commit_ack(manifest.transfer_id, ack_body)
            self.committed_transfers.add(manifest.transfer_id)
            self.pending_transfers.pop(manifest.transfer_id, None)
            self.phase = "READY_TO_GENERATE"
        except Exception as exc:  # noqa: BLE001 - socket boundary must reject atomically
            if manifest is not None and not receipt_sent:
                try:
                    if not writer.is_closing():
                        await self.transport.write_receipt(
                            writer,
                            WireReceipt(
                                transfer_id=manifest.transfer_id,
                                accepted=False,
                                receiver_wire_ms=wire_ms,
                                detail=str(exc),
                            ),
                        )
                except Exception:  # noqa: BLE001 - original failure is authoritative
                    pass
                try:
                    await self.coordinator.commit_transfer(
                        manifest.transfer_id,
                        {
                            "transfer_id": manifest.transfer_id,
                            "conversation_id": manifest.conversation_id,
                            "receiver_agent": self.settings.agent_id,
                            "state_id": manifest.state_id,
                            "turn_id": manifest.turn_id,
                            "token_count": manifest.token_count,
                            "success": False,
                            "error": str(exc),
                            "metrics": {"p2p_ms": wire_ms},
                        },
                    )
                except Exception:  # noqa: BLE001 - best effort error callback
                    pass
                await self._report_error(
                    code="TRANSFER_RECEIVE_FAILED",
                    detail=str(exc),
                    phase="receive_transfer",
                    conversation_id=manifest.conversation_id,
                    turn_id=manifest.turn_id,
                    transfer_id=manifest.transfer_id,
                )
            elif receipt_sent:
                # The model state is restored and the Sender has a success
                # receipt. A missing HTTP response makes the central ACK
                # outcome unknown; never rewrite it as a transfer failure.
                self.phase = "COMMIT_ACK_UNKNOWN"
        finally:
            if receive_lock is not None and receive_lock_acquired:
                receive_lock.release()
            writer.close()
            await writer.wait_closed()

    def _validate_incoming(
        self,
        manifest: TransferManifest,
        token_ids: list[int],
        plan: TransferPlan | None,
    ) -> None:
        if plan is None:
            raise AgentRuntimeError("transfer was not prepared")
        expected = TransferManifest.from_plan(plan)
        if manifest.model_dump() != expected.model_dump():
            raise AgentRuntimeError("wire manifest does not match the prepared plan")
        if manifest.target_agent != self.settings.agent_id:
            raise AgentRuntimeError("wire manifest targets a different Agent")
        if len(token_ids) != manifest.token_count:
            raise AgentRuntimeError("wire token count does not match the manifest")
        session = self._require_session(manifest.conversation_id)
        if len(token_ids) > session.max_context_tokens:
            raise AgentRuntimeError("CONTEXT_LIMIT: incoming state exceeds max_context_tokens")

    async def _register_until_ready(self) -> None:
        if self.runtime_descriptor is None:
            raise AgentRuntimeError("runtime descriptor is unavailable")
        registration = AgentRegistration(
            agent_id=self.settings.agent_id,
            instance_id=self.settings.instance_id,
            control_url=self.settings.control_advertise_url,
            data_bind=self.settings.data_bind,
            data_advertise=self.settings.data_advertise,
            runtime_descriptor=self.runtime_descriptor,
            transport_capabilities=[handler.capability for handler in self.handlers.values()],
            status=AgentLifecycleStatus.READY,
        )
        last_error: Exception | None = None
        for _ in range(60):
            try:
                await self.coordinator.register(registration)
                self.registered = True
                return
            except CoordinatorClientError as exc:
                last_error = exc
                await asyncio.sleep(0.5)
        raise AgentRuntimeError(f"could not register with Coordinator: {last_error}")

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_interval_seconds)
            try:
                await self.coordinator.heartbeat(
                    Heartbeat(
                        agent_id=self.settings.agent_id,
                        instance_id=self.settings.instance_id,
                        status=self.status,
                        conversation_id=self.current_conversation_id,
                        phase=self.phase,
                    )
                )
            except CoordinatorClientError:
                self.registered = False
                try:
                    await self._register_until_ready()
                except AgentRuntimeError:
                    continue

    async def _post_metric(
        self,
        *,
        conversation_id: str,
        phase: str,
        duration_ms: float,
        turn_id: int,
        transfer_id: str | None = None,
        values: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        await self.coordinator.metric(
            {
                "conversation_id": conversation_id,
                "phase": phase,
                "duration_ms": max(0.0, duration_ms),
                "agent_id": self.settings.agent_id,
                "turn_id": turn_id,
                "transfer_id": transfer_id,
                "values": values or {},
            }
        )

    async def _commit_ack(self, transfer_id: str, body: dict[str, Any]) -> None:
        last_error: CoordinatorClientError | None = None
        for attempt in range(2):
            try:
                await self.coordinator.commit_transfer(transfer_id, body)
                return
            except CoordinatorClientError as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.1)
        assert last_error is not None
        raise last_error

    async def _report_error(
        self,
        *,
        code: str,
        detail: str,
        phase: str,
        conversation_id: str | None,
        turn_id: int | None,
        transfer_id: str | None,
    ) -> None:
        try:
            await self.coordinator.error(
                AgentErrorReport(
                    agent_id=self.settings.agent_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    transfer_id=transfer_id,
                    code=code,
                    detail=detail,
                ),
                phase=phase,
            )
        except CoordinatorClientError:
            pass

    def _require_session(self, conversation_id: str) -> PeerSession:
        session = self.sessions.get(conversation_id)
        if session is None:
            raise AgentRuntimeError(f"unknown conversation: {conversation_id}")
        return session

    def _handler(self, mode: str, scope: str) -> AgentTransferHandler:
        handler = self.handlers.get((mode, scope))
        if handler is None:
            raise AgentRuntimeError(f"unsupported transfer handler: {mode}/{scope}")
        return handler


__all__ = ["AgentRuntime", "AgentRuntimeError", "PeerSession"]
