from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from agent_peer.config import AgentSettings
from agent_peer.network import DeterministicLinkConditionApplier
from agent_peer.runtime import AgentRuntime, AgentRuntimeError
from agent_peer.transport import TcpTokenTransport, WireReceipt
from coordinator.schemas import AgentCommand, default_session_config
from coordinator.service import CoordinatorService
from model_service.runtime import MockModelRuntime
from model_service.tokenizer import ControlToken
from shared.codec import encode_token_payload
from shared.contracts import (
    AgentProfile,
    AgentRegistration,
    CreateSessionRequest,
    GenerateCommand,
    GenerateRequest,
    GenerateResponse,
    NetworkState,
    OpenSessionCommand,
    PrefillRequest,
    PrefillResponse,
    PrepareTransferCommand,
    SealRequest,
    SealResponse,
    StateOffer,
    TokenizeRequest,
    TokenizeResponse,
    TokenTransferManifest,
    TransferManifest,
    TransferPlan,
)
from shared.kv import CacheInventory, TokenImportRequest, prefix_digest, runtime_fingerprint


def _profile(agent_id: str = "agent-a") -> AgentProfile:
    return AgentProfile(
        agent_id=agent_id,
        display_name=agent_id,
        role_description="A deterministic test role.",
        system_prompt="Respond deterministically.",
        local_context="Local test context.",
    )


def _network(*, source: str = "agent-a", target: str = "agent-b") -> NetworkState:
    return NetworkState(
        snapshot_id="network-1",
        source_agent=source,
        target_agent=target,
        bandwidth_mbps=100.0,
        rtt_ms=10.0,
        jitter_ms=2.0,
        sampled_jitter_ms=1.0,
        effective_rtt_ms=11.0,
        version=3,
        seed=17,
    )


def _plan(
    *,
    source: str = "agent-a",
    target: str = "agent-b",
    conversation_id: str = "conversation-1",
    tokens: int = 3,
) -> TransferPlan:
    network = _network(source=source, target=target)
    return TransferPlan(
        transfer_id="transfer-1",
        conversation_id=conversation_id,
        turn_id=1,
        source_agent=source,
        target_agent=target,
        state_id="state-1",
        token_count=tokens,
        payload_bytes=tokens * 4,
        network_snapshot_id=network.snapshot_id,
        network_state=network,
        predicted_p2p_ms=11.001,
    )


class _MemoryWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.drain_count = 0

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        self.drain_count += 1


@pytest.mark.asyncio
async def test_link_shaper_applies_one_rtt_and_per_chunk_bandwidth_delay() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    shaper = DeterministicLinkConditionApplier(sleep=fake_sleep)
    writer = _MemoryWriter()
    network = NetworkState(
        source_agent="agent-a",
        target_agent="agent-b",
        bandwidth_mbps=0.008,  # exactly 1,000 bytes/second
        rtt_ms=10,
        jitter_ms=1,
        sampled_jitter_ms=1,
        effective_rtt_ms=11,
    )

    await shaper.write(
        cast(asyncio.StreamWriter, writer),
        b"0123456789",
        network,
        chunk_bytes=4,
    )

    assert bytes(writer.data) == b"0123456789"
    assert writer.drain_count == 3
    assert sleeps == pytest.approx([0.011, 0.004, 0.004, 0.002])
    assert shaper.predicted_p2p_ms(network, 10) == pytest.approx(21.0)


@pytest.mark.asyncio
async def test_tcp_transport_sends_length_prefixed_manifest_and_uint32_tokens() -> None:
    received: list[tuple[TokenTransferManifest, list[int]]] = []

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            manifest, token_ids, wire_ms = await TcpTokenTransport.receive(reader)
            received.append((manifest, token_ids))
            await TcpTokenTransport.write_receipt(
                writer,
                WireReceipt(
                    transfer_id=manifest.transfer_id,
                    accepted=True,
                    receiver_wire_ms=wire_ms,
                ),
            )
        finally:
            writer.close()
            await writer.wait_closed()

    async def no_sleep(_: float) -> None:
        return None

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    socket = server.sockets[0]
    port = int(socket.getsockname()[1])
    transport = TcpTokenTransport(
        shaper=DeterministicLinkConditionApplier(sleep=no_sleep),
        chunk_bytes=5,
    )
    plan = _plan()
    manifest = TokenTransferManifest.from_plan(plan)
    token_ids = [ControlToken.BOS, 256, 511]

    try:
        result = await transport.send(
            f"tcp://127.0.0.1:{port}",
            manifest,
            token_ids,
            plan.network_state,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert received == [(manifest, [int(token) for token in token_ids])]
    assert result.frame_bytes > manifest.payload_bytes
    assert result.sender_prepare_ms >= 0
    assert TcpTokenTransport.parse_endpoint("host.example:9123") == (
        "host.example",
        9123,
    )
    with pytest.raises(ValueError, match="invalid P2P endpoint"):
        TcpTokenTransport.parse_endpoint("missing-port")


class _SpyModel(MockModelRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.create_requests: list[CreateSessionRequest] = []
        self.tokenize_requests: list[TokenizeRequest] = []
        self.prefill_requests: list[PrefillRequest] = []
        self.generate_requests: list[GenerateRequest] = []
        self.seal_requests: list[SealRequest] = []

    async def create_session(self, request: CreateSessionRequest):
        self.create_requests.append(request)
        return await super().create_session(request)

    async def tokenize(self, request: TokenizeRequest) -> TokenizeResponse:
        self.tokenize_requests.append(request)
        return await super().tokenize(request)

    async def prefill(self, request: PrefillRequest) -> PrefillResponse:
        self.prefill_requests.append(request)
        return await super().prefill(request)

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        self.generate_requests.append(request)
        return await super().generate(request)

    async def seal(self, request: SealRequest) -> SealResponse:
        self.seal_requests.append(request)
        return await super().seal(request)

    async def close(self) -> None:
        return None


class _FakeCoordinator:
    def __init__(self) -> None:
        self.offers: list[StateOffer] = []
        self.metrics: list[dict[str, Any]] = []
        self.errors: list[tuple[Any, str]] = []

    async def offer(self, offer: StateOffer) -> dict[str, Any]:
        self.offers.append(offer)
        return {"status": "completed", "plan": None}

    async def metric(self, body: dict[str, Any]) -> dict[str, Any]:
        self.metrics.append(body)
        return {"status": "accepted"}

    async def error(self, report: Any, *, phase: str) -> dict[str, Any]:
        self.errors.append((report, phase))
        return {"status": "accepted"}

    async def close(self) -> None:
        return None


def _runtime(
    model: _SpyModel | None = None,
    coordinator: _FakeCoordinator | None = None,
) -> tuple[AgentRuntime, _SpyModel, _FakeCoordinator]:
    actual_model = model or _SpyModel()
    actual_coordinator = coordinator or _FakeCoordinator()
    settings = AgentSettings(
        agent_id="agent-a",
        agent_display_name="Agent A",
        instance_id="instance-a",
        data_bind_port=19101,
        data_advertise_port=19101,
    )
    runtime = AgentRuntime(
        settings,
        model=cast(Any, actual_model),
        coordinator=cast(Any, actual_coordinator),
    )
    return runtime, actual_model, actual_coordinator


def _open_command(
    *, command_id: str = "open-1", is_first_speaker: bool = True
) -> OpenSessionCommand:
    return OpenSessionCommand(
        command_id=command_id,
        conversation_id="conversation-1",
        profile=_profile(),
        peer_agent_id="agent-b",
        initial_input="Initial shared task.",
        shared_prompt="Initial shared task.",
        max_context_tokens=4096,
        is_first_speaker=is_first_speaker,
    )


def _contains_token_ids(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"token_ids", "tokens"} or _contains_token_ids(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_token_ids(child) for child in value)
    return False


@pytest.mark.asyncio
async def test_first_speaker_prefills_initial_input_then_offers_generated_metadata() -> None:
    runtime, model, coordinator = _runtime()
    command = _open_command()

    await runtime.open_session(command)

    assert len(model.create_requests) == 1
    assert model.tokenize_requests == [TokenizeRequest(text=command.initial_input, add_bos=True)]
    assert len(model.prefill_requests) == 1
    assert model.prefill_requests[0].token_ids[0] == ControlToken.BOS
    assert model.prefill_requests[0].replace is True
    initial_tokens = model.prefill_requests[0].token_ids.copy()

    await runtime.generate(
        GenerateCommand(
            command_id="generate-1",
            conversation_id=command.conversation_id,
            turn_id=1,
            target_agent="agent-b",
            profile=command.profile,
            local_context="",
        )
    )

    assert len(model.generate_requests) == 1
    assert len(model.seal_requests) == 1
    assert len(coordinator.offers) == 1
    offer = coordinator.offers[0]
    assert offer.source_agent == "agent-a"
    assert offer.target_agent == "agent-b"
    assert offer.turn_id == 1
    assert offer.message_text is not None
    assert offer.message_text.startswith("Mock agent-a turn 1: context=")
    assert offer.message_text.endswith(" tokens.")
    assert offer.payload_bytes == (offer.output_token_count + offer.wrapper_token_count) * 4
    assert not _contains_token_ids(offer.model_dump(mode="python"))
    assert runtime.sessions[command.conversation_id].token_ids[: len(initial_tokens)] == (
        initial_tokens
    )
    assert [metric["phase"] for metric in coordinator.metrics] == [
        "local_prefill",
        "generation",
    ]


@pytest.mark.asyncio
async def test_prepare_transfer_binds_exact_plan_and_rejects_mismatches() -> None:
    runtime, _, _ = _runtime()
    command = _open_command()
    await runtime.open_session(command)

    plan = _plan(source="agent-b", target="agent-a").model_copy(update={"scope": "RECOVERY"})
    await runtime.prepare_transfer(
        PrepareTransferCommand(
            command_id="prepare-1",
            conversation_id=command.conversation_id,
            plan=plan,
        )
    )
    manifest = TransferManifest.from_plan(plan)
    runtime._validate_incoming(manifest, [256, 257, 258], plan)

    changed_manifest = manifest.model_copy(update={"state_id": "other-state"})
    with pytest.raises(AgentRuntimeError, match="prepared plan"):
        runtime._validate_incoming(changed_manifest, [256, 257, 258], plan)
    with pytest.raises(AgentRuntimeError, match="not prepared"):
        runtime._validate_incoming(manifest, [256, 257, 258], None)
    with pytest.raises(AgentRuntimeError, match="token count"):
        runtime._validate_incoming(manifest, [256, 257], plan)

    wrong_target = _plan(source="agent-a", target="agent-b")
    with pytest.raises(AgentRuntimeError, match="different Agent"):
        await runtime.prepare_transfer(
            PrepareTransferCommand(
                command_id="prepare-wrong-agent",
                conversation_id=command.conversation_id,
                plan=wrong_target,
            )
        )

    mismatched_conversation = _plan(
        source="agent-b",
        target="agent-a",
        conversation_id="different-conversation",
    )
    with pytest.raises(AgentRuntimeError, match="conversation"):
        await runtime.prepare_transfer(
            PrepareTransferCommand(
                command_id="prepare-wrong-conversation",
                conversation_id=command.conversation_id,
                plan=mismatched_conversation,
            )
        )


@pytest.mark.asyncio
async def test_duplicate_command_id_returns_cached_result_without_reexecution() -> None:
    runtime, model, coordinator = _runtime()
    command = _open_command(command_id="same-command")
    payload = command.model_dump(mode="python", exclude={"command_id", "command_type"})

    first = await runtime.enqueue_command(
        command.command_id,
        command.command_type,
        payload,
    )
    second = await runtime.enqueue_command(
        command.command_id,
        command.command_type,
        payload,
    )

    assert first == second
    assert first["status"] == "completed"
    assert len(model.create_requests) == 1
    assert coordinator.errors == []

    conflicting = command.model_copy(
        update={"command_id": "different-command", "peer_agent_id": "agent-c"}
    )
    with pytest.raises(AgentRuntimeError, match="differs"):
        await runtime.open_session(conflicting)


class _OrderedModel(_SpyModel):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def prefill(self, request: PrefillRequest) -> PrefillResponse:
        response = await super().prefill(request)
        self._events.append("prefill_completed")
        return response

    async def import_tokens(self, request: TokenImportRequest):
        response = await super().import_tokens(request)
        self._events.append("prefill_completed")
        return response


class _OrderedCoordinator(_FakeCoordinator):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events
        self.commits: list[dict[str, Any]] = []

    async def commit_transfer(self, transfer_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._events.append("coordinator_commit")
        self.commits.append(body)
        return {"status": "committed", "transfer_id": transfer_id, **body}


class _OrderedTransport:
    def __init__(
        self,
        events: list[str],
        manifest: TransferManifest,
        token_ids: list[int],
    ) -> None:
        self._events = events
        self._manifest = manifest
        self._token_ids = token_ids
        self.receipts: list[WireReceipt] = []

    async def receive(self, _: Any) -> tuple[TransferManifest, bytes, float]:
        return self._manifest, encode_token_payload(self._token_ids), 4.0

    async def write_receipt(self, _: Any, receipt: WireReceipt) -> None:
        self._events.append("success_receipt" if receipt.accepted else "failure_receipt")
        self.receipts.append(receipt)


class _ClosingWriter:
    def __init__(self) -> None:
        self.closed = False

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@pytest.mark.asyncio
async def test_receiver_acknowledges_only_after_prefill_then_commits_metadata() -> None:
    events: list[str] = []
    model = _OrderedModel(events)
    coordinator = _OrderedCoordinator(events)
    runtime, _, _ = _runtime(model=model, coordinator=coordinator)
    await runtime.open_session(_open_command(is_first_speaker=False))
    events.clear()
    base = runtime.sessions["conversation-1"]
    plan = TransferPlan.model_validate(
        {
            **_plan(source="agent-b", target="agent-a").model_dump(),
            "scope": "DELTA",
            "token_count": len(base.token_ids) + 3,
            "start_token": len(base.token_ids),
            "base_state_id": base.state_id,
            "base_digest": prefix_digest(base.token_ids),
            "target_digest": prefix_digest(base.token_ids + [256, 257, 258]),
            "runtime_fingerprint": runtime_fingerprint(await model.describe()),
        }
    )
    await runtime.prepare_transfer(
        PrepareTransferCommand(
            command_id="prepare-receive",
            conversation_id="conversation-1",
            plan=plan,
        )
    )
    manifest = TransferManifest.from_plan(plan)
    transport = _OrderedTransport(events, manifest, [256, 257, 258])
    runtime.transport = cast(Any, transport)
    writer = _ClosingWriter()

    await runtime._receive_connection(cast(Any, object()), cast(Any, writer))

    assert events == [
        "prefill_completed",
        "success_receipt",
        "coordinator_commit",
    ]
    assert len(transport.receipts) == 1
    assert transport.receipts[0].accepted is True
    assert set(coordinator.commits[0]["metrics"]) == {
        "p2p_ms",
        "receiver_prefill_ms",
    }
    assert writer.closed is True
    before = runtime.sessions["conversation-1"].token_ids.copy()
    await runtime._receive_connection(cast(Any, object()), cast(Any, _ClosingWriter()))
    assert runtime.sessions["conversation-1"].token_ids == before
    assert events.count("prefill_completed") == 1


class _RecordingControlClient:
    async def cache_status(self, control_url: str, conversation_id: str):
        return CacheInventory(
            known_state_id=f"{conversation_id}:initial",
            known_token_count=1,
            token_count=1,
            known_token_digest=prefix_digest([256]),
            prefix_digest=prefix_digest([256]),
            runtime_fingerprint=runtime_fingerprint(await MockModelRuntime().describe()),
        )

    def __init__(self) -> None:
        self.commands: list[tuple[str, AgentCommand]] = []

    async def send(self, control_url: str, command: AgentCommand) -> None:
        self.commands.append((control_url, command))

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_terminal_offer_stops_both_agents_for_the_next_session() -> None:
    control = _RecordingControlClient()
    coordinator = CoordinatorService(control_client=cast(Any, control))
    config = default_session_config().model_copy(
        update={"max_agent_turns": 1, "mode_policy": "always_token"}
    )
    descriptor = await MockModelRuntime().describe()
    for profile, port in (
        (config.agent_a_profile, 18201),
        (config.agent_b_profile, 18202),
    ):
        await coordinator.register_agent(
            AgentRegistration(
                agent_id=profile.agent_id,
                instance_id=f"instance-{profile.agent_id}",
                control_url=f"http://{profile.agent_id}:{port}",
                data_bind=f"0.0.0.0:{port + 1000}",
                data_advertise=f"{profile.agent_id}:{port + 1000}",
                runtime_descriptor=descriptor,
            )
        )

    session = await coordinator.create_session(config)
    source = config.first_speaker
    target = (
        config.agent_b_profile.agent_id
        if source == config.agent_a_profile.agent_id
        else config.agent_a_profile.agent_id
    )
    result = await coordinator.accept_state_offer(
        StateOffer(
            conversation_id=session.conversation_id,
            turn_id=1,
            source_agent=source,
            target_agent=target,
            state_id="terminal-state",
            parent_state_id=f"{session.conversation_id}:initial",
            shared_prefix_tokens=1,
            output_token_count=3,
            shared_prefix_digest=prefix_digest([256]),
            target_digest=prefix_digest([256] * 4),
            runtime_fingerprint=runtime_fingerprint(descriptor),
            token_count=4,
            payload_bytes=16,
            message_text="Final local Mock response.",
        )
    )

    assert result is None
    stop_commands = [(url, command) for url, command in control.commands if command.type == "stop"]
    assert {command.payload["conversation_id"] for _, command in stop_commands} == {
        session.conversation_id
    }
    assert {url for url, _ in stop_commands} == {
        "http://agent-a:18201",
        "http://agent-b:18202",
    }
    await coordinator.close()
