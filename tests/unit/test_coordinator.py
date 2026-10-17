from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from coordinator.app import create_app
from coordinator.errors import ConflictError, InvalidTransitionError, StaleVersionError
from coordinator.journal import EventJournal
from coordinator.schemas import (
    AgentCommand,
    AgentErrorReport,
    MetricReport,
    TransferCommitAck,
)
from coordinator.service import CoordinatorService
from shared.kv import CacheInventory, prefix_digest, runtime_fingerprint
from shared.models import (
    AgentRegistration,
    Heartbeat,
    ModelBehaviorConfig,
    RuntimeDescriptor,
    StateOffer,
)


def cache_inventory(state_id: str, count: int, cached: int | None = None) -> CacheInventory:
    cached = count if cached is None else cached
    return CacheInventory(
        known_state_id=state_id,
        known_token_count=count,
        known_token_digest=prefix_digest([256] * count),
        state_id=state_id if cached else None,
        token_count=cached,
        prefix_digest=prefix_digest([256] * cached),
        runtime_fingerprint=runtime_fingerprint(RuntimeDescriptor()),
    )


def model_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=ModelBehaviorConfig().model_dump())
        ),
        base_url="http://model",
    )


class RecordingControlClient:
    def __init__(self) -> None:
        self.commands: list[tuple[str, AgentCommand]] = []
        self.inventories: dict[str, CacheInventory] = {}

    async def cache_status(self, control_url: str, conversation_id: str) -> CacheInventory:
        return self.inventories[control_url].model_copy(deep=True)

    async def send(self, control_url: str, command: AgentCommand) -> None:
        self.commands.append((control_url, command.model_copy(deep=True)))
        if command.type == "open_session":
            self.inventories[control_url] = cache_inventory(
                f"{command.payload['conversation_id']}:initial",
                20,
            )
        elif command.type == "generate":
            turn = command.payload["turn_id"]
            self.inventories[control_url] = cache_inventory(f"state-{turn}", 20 + turn)

    async def close(self) -> None:
        return None


class CacheControlClient(RecordingControlClient):
    inventory: CacheInventory | None = None

    async def cache_status(self, control_url: str, conversation_id: str) -> CacheInventory:
        return (
            self.inventory.model_copy(deep=True)
            if self.inventory
            else await super().cache_status(control_url, conversation_id)
        )


@pytest.mark.asyncio
async def test_kv_stale_ancestor_debt_and_missing_cache_full_fallback() -> None:
    control = CacheControlClient()
    service = CoordinatorService(control_client=control, model_service_client=model_client())
    for agent_id in ["agent-a", "agent-b"]:
        agent = registration(agent_id)
        agent.transport_capabilities += ["KV_FULL_V2", "KV_DELTA_V2"]
        await service.register_agent(agent)
    config = (await service.get_config()).config.model_copy(update={"mode_policy": "always_kv"})
    session = await service.create_session(config)
    try:
        source, target = "agent-a", "agent-b"
        for turn in range(1, 5):
            # Turn 3 deliberately retains a stale ancestor, turn 4 simulates eviction.
            control.inventory = (
                cache_inventory("state-1", 21)
                if turn in {2, 3}
                else cache_inventory(f"{session.conversation_id}:initial", 20, 0)
            )
            plan = await service.accept_state_offer(
                offer_for(
                    session.conversation_id,
                    turn,
                    source,
                    target,
                    parent_state_id=f"state-{turn - 1}" if turn > 1 else None,
                )
            )
            assert plan is not None
            assert plan.scope == ("DELTA" if turn in {2, 3} else "FULL")
            pending = await service.get_session(session.conversation_id)
            assert len(pending.ledger) == turn - 1
            if turn == 3:
                assert pending.transfers[-1].xi_tokens == 1
                assert plan.start_token == 21
            await service.commit_transfer(
                TransferCommitAck(
                    transfer_id=plan.transfer_id,
                    conversation_id=session.conversation_id,
                    receiver_agent=target,
                    state_id=plan.state_id,
                    turn_id=turn,
                    token_count=plan.token_count,
                )
            )
            source, target = target, source
    finally:
        await service.close()


def registration(
    agent_id: str,
    *,
    revision: str = "1",
    instance_id: str | None = None,
) -> AgentRegistration:
    suffix = agent_id.rsplit("-", 1)[-1]
    return AgentRegistration(
        agent_id=agent_id,
        instance_id=instance_id or f"instance-{suffix}",
        control_url=f"http://{agent_id}:8100",
        data_bind="0.0.0.0:9100",
        data_advertise=f"{agent_id}:9100",
        runtime_descriptor=RuntimeDescriptor(revision=revision),
        transport_capabilities=["TOKEN_DELTA_V2"],
        status="READY",
        registered_at=datetime.now(UTC),
    )


async def ready_service() -> tuple[CoordinatorService, RecordingControlClient]:
    client = RecordingControlClient()
    service = CoordinatorService(control_client=client, model_service_client=model_client())
    current = await service.get_config()
    await service.update_config(
        current.config.model_copy(update={"mode_policy": "always_token"}),
        expected_version=current.version,
    )
    await service.register_agent(registration("agent-a"))
    await service.register_agent(registration("agent-b"))
    return service, client


def offer_for(
    conversation_id: str,
    turn_id: int,
    source: str,
    target: str,
    *,
    parent_state_id: str | None = None,
) -> StateOffer:
    return StateOffer(
        offer_id=f"offer-{turn_id}",
        conversation_id=conversation_id,
        turn_id=turn_id,
        source_agent=source,
        target_agent=target,
        state_id=f"state-{turn_id}",
        parent_state_id=parent_state_id or f"{conversation_id}:initial",
        shared_prefix_tokens=19 + turn_id,
        output_token_count=1,
        target_digest=prefix_digest([256] * (20 + turn_id)),
        shared_prefix_digest=prefix_digest([256] * (19 + turn_id)),
        runtime_fingerprint=runtime_fingerprint(RuntimeDescriptor()),
        token_count=20 + turn_id,
        payload_bytes=(20 + turn_id) * 4,
        message_text=f"第 {turn_id} 次回复",
    )


@pytest.mark.asyncio
async def test_registration_heartbeat_and_duplicate_live_instance() -> None:
    service, _ = await ready_service()
    agents = await service.registry.list()
    assert [agent.agent_id for agent in agents] == ["agent-a", "agent-b"]
    assert all(agent.ready and agent.online for agent in agents)

    heartbeat = Heartbeat(
        agent_id="agent-a",
        instance_id="instance-a",
        status="BUSY",
        conversation_id="conversation-test",
        phase="generate",
    )
    updated = await service.heartbeat_agent(heartbeat)
    assert updated["status"] == "BUSY"
    assert updated["conversation_id"] == "conversation-test"

    with pytest.raises(ConflictError, match="live instance"):
        await service.register_agent(registration("agent-a", instance_id="replacement"))


@pytest.mark.asyncio
async def test_session_requires_matching_runtime_and_only_one_active_session() -> None:
    client = RecordingControlClient()
    mismatch = CoordinatorService(control_client=client, model_service_client=model_client())
    await mismatch.register_agent(registration("agent-a", revision="1"))
    await mismatch.register_agent(registration("agent-b", revision="2"))
    with pytest.raises(ConflictError, match="RuntimeDescriptor"):
        await mismatch.create_session()

    service, commands = await ready_service()
    session = await service.create_session()
    assert session.status == "RUNNING"
    assert session.current_speaker == "agent-a"
    assert [command.type for _, command in commands.commands] == [
        "open_session",
        "open_session",
        "generate",
    ]
    generate = commands.commands[-1][1]
    assert generate.payload["target_agent"] == "agent-b"
    open_commands = [command for _, command in commands.commands if command.type == "open_session"]
    assert all(command.payload["stop_on_eos"] is True for command in open_commands)

    with pytest.raises(ConflictError, match="already active"):
        await service.create_session()


@pytest.mark.asyncio
async def test_transfer_is_committed_only_after_receiver_ack() -> None:
    service, commands = await ready_service()
    session = await service.create_session()
    state_offer = offer_for(session.conversation_id, 1, "agent-a", "agent-b")

    plan = await service.accept_state_offer(state_offer)
    assert plan is not None
    before_ack = await service.get_session(session.conversation_id)
    assert before_ack.ledger == []
    assert before_ack.turn_count == 0
    assert before_ack.pending_transfer_id == plan.transfer_id
    assert [command.type for _, command in commands.commands[-2:]] == [
        "prepare_transfer",
        "execute_transfer",
    ]
    execute = commands.commands[-1][1]
    assert execute.payload["target_data_endpoint"] == "agent-b:9100"
    assert execute.payload["plan"]["mode"] == "TOKEN"

    ack = TransferCommitAck(
        transfer_id=plan.transfer_id,
        conversation_id=session.conversation_id,
        receiver_agent="agent-b",
        state_id="state-1",
        turn_id=1,
        token_count=21,
        metrics={"p2p_ms": 9.5, "receiver_prefill_ms": 3.0},
    )
    committed = await service.commit_transfer(ack)
    assert committed.turn_count == 1
    assert committed.current_speaker == "agent-b"
    assert committed.pending_transfer_id is None
    assert [entry.state_id for entry in committed.ledger] == ["state-1"]
    assert commands.commands[-1][1].type == "generate"
    assert commands.commands[-1][1].payload["target_agent"] == "agent-a"

    duplicate = await service.commit_transfer(ack)
    assert duplicate == committed
    assert len((await service.get_session(session.conversation_id)).ledger) == 1
    assert sum(command.type == "generate" for _, command in commands.commands) == 2

    with pytest.raises(ConflictError, match="different ACK"):
        await service.commit_transfer(ack.model_copy(update={"metrics": {"p2p_ms": 99.0}}))

    ignored = await service.record_error(
        AgentErrorReport(
            conversation_id=session.conversation_id,
            agent_id="agent-b",
            phase="receive_transfer",
            message="ACK response was lost",
            transfer_id=plan.transfer_id,
        )
    )
    assert ignored == {"status": "ignored"}
    assert (await service.get_session(session.conversation_id)).status == "RUNNING"


@pytest.mark.asyncio
async def test_stop_with_pending_transfer_cleans_up_both_agents_after_ack() -> None:
    service, commands = await ready_service()
    session = await service.create_session()
    plan = await service.accept_state_offer(
        offer_for(session.conversation_id, 1, "agent-a", "agent-b")
    )
    assert plan is not None

    stopping = await service.stop_session(session.conversation_id)
    assert stopping.status == "STOPPING"
    assert not any(command.type == "stop" for _, command in commands.commands)

    stopped = await service.commit_transfer(
        TransferCommitAck(
            transfer_id=plan.transfer_id,
            conversation_id=session.conversation_id,
            receiver_agent="agent-b",
            state_id="state-1",
            turn_id=1,
            token_count=21,
        )
    )
    assert stopped.status == "STOPPED"
    stop_commands = [command for _, command in commands.commands if command.type == "stop"]
    assert len(stop_commands) == 2
    assert {command.payload["conversation_id"] for command in stop_commands} == {
        session.conversation_id
    }


@pytest.mark.asyncio
async def test_concurrent_stop_prevents_state_offer_plan_installation() -> None:
    service, commands = await ready_service()
    session = await service.create_session()
    generated_event_started = asyncio.Event()
    release_generated_event = asyncio.Event()
    publish = service.journal.publish

    async def gated_publish(event_type: str, **kwargs: Any) -> Any:
        if event_type == "turn.generated":
            generated_event_started.set()
            await release_generated_event.wait()
        return await publish(event_type, **kwargs)

    service.journal.publish = gated_publish  # type: ignore[method-assign]
    offer_task = asyncio.create_task(
        service.accept_state_offer(offer_for(session.conversation_id, 1, "agent-a", "agent-b"))
    )
    await generated_event_started.wait()
    stopped = await service.stop_session(session.conversation_id)
    assert stopped.status == "STOPPED"
    release_generated_event.set()

    with pytest.raises(InvalidTransitionError, match="STOPPED"):
        await offer_task
    stored = await service.get_session(session.conversation_id)
    assert stored.pending_transfer_id is None
    assert not any(
        command.type in {"prepare_transfer", "execute_transfer"} for _, command in commands.commands
    )


@pytest.mark.asyncio
async def test_later_offer_must_extend_latest_committed_state() -> None:
    service, _ = await ready_service()
    session = await service.create_session()
    first = await service.accept_state_offer(
        offer_for(session.conversation_id, 1, "agent-a", "agent-b")
    )
    assert first is not None
    await service.commit_transfer(
        TransferCommitAck(
            transfer_id=first.transfer_id,
            conversation_id=session.conversation_id,
            receiver_agent="agent-b",
            state_id="state-1",
            turn_id=1,
            token_count=21,
        )
    )

    with pytest.raises(ConflictError, match="expected parent_state_id 'state-1'"):
        await service.accept_state_offer(
            offer_for(
                session.conversation_id,
                2,
                "agent-b",
                "agent-a",
                parent_state_id="stale-state",
            )
        )


@pytest.mark.asyncio
async def test_offer_terminal_reason_finishes_without_redundant_transfer() -> None:
    service, commands = await ready_service()
    session = await service.create_session()
    eos_offer = offer_for(session.conversation_id, 1, "agent-a", "agent-b").model_copy(
        update={"terminal_reason": "EOS"}
    )
    assert await service.accept_state_offer(eos_offer) is None

    completed = await service.get_session(session.conversation_id)
    assert completed.status == "COMPLETED"
    assert completed.finish_reason == "EOS"
    assert not any(command.type == "execute_transfer" for _, command in commands.commands)
    assert len([command for _, command in commands.commands if command.type == "stop"]) == 2


@pytest.mark.asyncio
async def test_metric_report_does_not_copy_conversation_id_into_strict_record() -> None:
    service, _ = await ready_service()
    session = await service.create_session()
    metric = await service.record_metric(
        MetricReport(
            conversation_id=session.conversation_id,
            phase="generation",
            duration_ms=12.5,
            agent_id="agent-a",
            turn_id=1,
        )
    )
    assert metric.phase == "generation"
    assert metric.duration_ms == 12.5
    stored = await service.get_session(session.conversation_id)
    assert stored.metrics == [metric]


@pytest.mark.asyncio
async def test_six_messages_make_five_transfers_and_no_final_redundant_transfer() -> None:
    service, commands = await ready_service()
    session = await service.create_session()
    source, target = "agent-a", "agent-b"

    for turn in range(1, 7):
        plan = await service.accept_state_offer(
            offer_for(
                session.conversation_id,
                turn,
                source,
                target,
                parent_state_id=f"state-{turn - 1}" if turn > 1 else None,
            )
        )
        if turn < 6:
            assert plan is not None
            await service.commit_transfer(
                TransferCommitAck(
                    transfer_id=plan.transfer_id,
                    conversation_id=session.conversation_id,
                    receiver_agent=target,
                    state_id=f"state-{turn}",
                    turn_id=turn,
                    token_count=20 + turn,
                )
            )
        else:
            assert plan is None
        source, target = target, source

    finished = await service.get_session(session.conversation_id)
    assert finished.status == "COMPLETED"
    assert finished.finish_reason == "MAX_TURNS"
    assert finished.turn_count == 6
    assert len(finished.messages) == 6
    assert len(finished.ledger) == 5
    assert sum(command.type == "execute_transfer" for _, command in commands.commands) == 5


@pytest.mark.asyncio
async def test_event_journal_is_ordered_bounded_and_rejects_token_payloads() -> None:
    journal = EventJournal(max_events=2)
    first = await journal.publish("first", data={})
    second = await journal.publish("second", data={})
    third = await journal.publish("third", data={})
    assert (first.seq, second.seq, third.seq) == (1, 2, 3)

    replay, resync = await journal.snapshot(after_seq=1)
    assert not resync
    assert [event.seq for event in replay] == [2, 3]
    _, resync = await journal.snapshot(after_seq=0)
    assert resync

    with pytest.raises(ValueError, match="token_ids"):
        await journal.publish("invalid", data={"nested": {"token_ids": [1, 2]}})


@pytest.mark.asyncio
async def test_config_optimistic_version_and_capabilities() -> None:
    service, _ = await ready_service()
    current = await service.get_config()
    changed = current.config.model_copy(update={"initial_input": "新的演示任务"})
    updated = await service.update_config(changed, expected_version=current.version)
    assert updated.version == current.version + 1
    with pytest.raises(StaleVersionError):
        await service.update_config(changed, expected_version=current.version)

    capabilities = service.capabilities()
    assert capabilities.transfer_modes == ["TOKEN/DELTA", "TOKEN/RECOVERY", "KV/FULL", "KV/DELTA"]
    assert "adaptive" in capabilities.mode_policies
    assert [extension.id for extension in capabilities.ui_extensions] == ["mock-lab"]


@pytest.mark.asyncio
async def test_mock_model_behavior_is_forwarded_to_independent_service() -> None:
    requests: list[httpx.Request] = []

    # Return valid JSON for PUT while retaining a strict assertion about the
    # actual request sent to the independent Model Service.
    def json_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            payload = {
                "tokenize_delay_ms": 2,
                "prefill_delay_ms": 3,
                "generate_delay_ms": 4,
                "response_prefix": "Remote",
            }
        else:
            payload = json.loads(request.content)
        return httpx.Response(200, json=payload)

    model_client = httpx.AsyncClient(
        base_url="http://model-service:8100",
        transport=httpx.MockTransport(json_handler),
    )
    service = CoordinatorService(
        control_client=RecordingControlClient(), model_service_client=model_client
    )
    observed = await service.model_behavior()
    assert observed.response_prefix == "Remote"

    current = await service.mock_config()
    applied = await service.update_model_behavior(
        ModelBehaviorConfig(
            tokenize_delay_ms=4,
            prefill_delay_ms=5,
            generate_delay_ms=6,
            response_prefix="Direct",
        )
    )
    assert applied.response_prefix == "Direct"
    synced = await service.mock_config()
    assert synced.version == current.version + 1
    assert synced.model == applied

    desired = synced.model_copy(
        update={
            "model": ModelBehaviorConfig(
                tokenize_delay_ms=5,
                prefill_delay_ms=6,
                generate_delay_ms=7,
                response_prefix="Updated",
            )
        }
    )
    updated = await service.update_mock_config(desired)
    assert updated.version == synced.version + 1
    assert updated.model.response_prefix == "Updated"
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/behavior"),
        ("PUT", "/behavior"),
        ("PUT", "/behavior"),
    ]
    await model_client.aclose()


@pytest.mark.asyncio
async def test_context_limit_agent_error_completes_and_cleans_up_session() -> None:
    service, commands = await ready_service()
    session = await service.create_session()
    result = await service.record_error(
        AgentErrorReport(
            conversation_id=session.conversation_id,
            agent_id="agent-a",
            phase="generate",
            message="COMMAND_FAILED: CONTEXT_LIMIT: generated state exceeds limit",
            turn_id=1,
        )
    )
    assert result == {"status": "completed"}
    completed = await service.get_session(session.conversation_id)
    assert completed.status == "COMPLETED"
    assert completed.finish_reason == "CONTEXT_LIMIT"
    assert len([command for _, command in commands.commands if command.type == "stop"]) == 2


def test_api_rejects_token_ids_in_state_offer() -> None:
    client_backend = RecordingControlClient()
    service = CoordinatorService(control_client=client_backend)
    app = create_app(service)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/internal/state-offers",
            json={
                "conversation_id": "conversation-test",
                "turn_id": 1,
                "source_agent": "agent-a",
                "target_agent": "agent-b",
                "state_id": "state-1",
                "token_count": 2,
                "payload_bytes": 8,
                "token_ids": [1, 2],
            },
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_state_offer_shared_contract_has_no_token_payload_field() -> None:
    with pytest.raises(ValidationError):
        StateOffer.model_validate(
            {
                "conversation_id": "conversation-test",
                "turn_id": 1,
                "source_agent": "agent-a",
                "target_agent": "agent-b",
                "state_id": "state-1",
                "token_count": 2,
                "payload_bytes": 8,
                "token_ids": [1, 2],
            }
        )


@pytest.mark.asyncio
async def test_capabilities_reflect_the_registered_real_backend() -> None:
    service = CoordinatorService(model_service_client=model_client())
    try:
        assert service.capabilities().unsupported_features == ["real_model"]
        for agent_id in ["agent-a", "agent-b"]:
            agent = registration(agent_id)
            agent.runtime_descriptor = RuntimeDescriptor(
                provider="huggingface",
                model_id="Qwen/Qwen2.5-1.5B-Instruct",
                dtype="float32",
                kv_cache_format="hf-stacked-kv-v1",
                kv_bytes_per_token=57344,
                kv_container_bytes=32,
            )
            await service.register_agent(agent)
        capabilities = service.capabilities()
        assert capabilities.providers["model"] == "huggingface"
        assert capabilities.unsupported_features == []
        assert (await service.environment()).model_provider == "huggingface"
    finally:
        await service.close()
