import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from coordinator.errors import ConflictError, ModelServiceError
from coordinator.model_connection import Connection, ConnectionRequest
from coordinator.schemas import SessionView
from coordinator.service import CoordinatorService
from shared.contracts import AgentRegistration, RuntimeDescriptor


async def setup_service(monkeypatch):
    control = AsyncMock()
    service = CoordinatorService(control_client=control, model_service_url="http://old:8100")
    for i, label in enumerate(("a", "b")):
        await service.register_agent(
            AgentRegistration(
                agent_id=f"agent-{label}",
                instance_id=label,
                control_url=f"http://127.0.0.1:{8201 + i}",
                data_bind=f"127.0.0.1:{9101 + i}",
                data_advertise=f"127.0.0.1:{9101 + i}",
                runtime_descriptor=RuntimeDescriptor(),
                status="READY",
            )
        )
    descriptor = RuntimeDescriptor(model_id="new-model")
    connection = Connection("http://new:8100", ConnectionRequest(url="http://new:8100"), descriptor)
    prepare = AsyncMock(return_value=connection)
    monkeypatch.setattr("coordinator.service.prepare_connection", prepare)
    monkeypatch.setattr("coordinator.service.describe_url", AsyncMock(return_value=descriptor))
    return service, control, prepare


async def test_switch_preserves_config_and_completed_history(monkeypatch):
    service, control, _ = await setup_service(monkeypatch)
    before = await service.get_config()
    history = SessionView(
        conversation_id="old",
        config_version=before.version,
        config=before.config,
        status="COMPLETED",
        turn_count=0,
        current_speaker="agent-a",
    )
    service._sessions["old"] = history
    try:
        result = await service.connect_model(ConnectionRequest(url="http://new:8100"))
        assert result["connected"]
        assert result["descriptor"]["model_id"] == "new-model"
        assert len(control.send.await_args_list) == 2
        assert await service.get_config() == before
        assert service._sessions["old"] == history
    finally:
        await service.close()


async def test_active_session_blocks_switch_before_opening_connection(monkeypatch):
    service, _, prepare = await setup_service(monkeypatch)
    config = (await service.get_config()).config
    service._sessions["active"] = SessionView(
        conversation_id="active",
        config_version=1,
        config=config,
        status="PAUSED",
        turn_count=0,
        current_speaker="agent-a",
    )
    try:
        with pytest.raises(ConflictError):
            await service.connect_model(ConnectionRequest())
        prepare.assert_not_awaited()
    finally:
        await service.close()


@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_partial_switch_rolls_back_both_attempted_peers(monkeypatch, rollback_fails):
    service, control, _ = await setup_service(monkeypatch)
    control.send.side_effect = [
        None,
        RuntimeError("peer unavailable"),
        RuntimeError("rollback failed") if rollback_fails else None,
        None,
    ]
    try:
        with pytest.raises(ModelServiceError):
            await service.connect_model(ConnectionRequest())
        calls = control.send.await_args_list
        assert [call.args[1].payload["url"] for call in calls] == [
            "http://new:8100",
            "http://new:8100",
            "http://old:8100",
            "http://old:8100",
        ]
        assert service._model_url == "http://old:8100"
        assert service._model_switch_failed is rollback_fails
        if rollback_fails:
            with pytest.raises(ConflictError):
                await service.create_session()
    finally:
        await service.close()


def test_ssh_option_injection_and_credential_urls_are_rejected():
    for config in (
        {"ssh_host": "-oProxyCommand=evil"},
        {"ssh_host": "host command"},
        {"url": "http://user:password@host"},
        {"source_address": "not-an-ip"},
    ):
        with pytest.raises(ValidationError):
            ConnectionRequest(**config)


async def test_refused_connection_retries_then_succeeds(monkeypatch):
    from coordinator import model_connection as module

    candidate = Connection("http://new", ConnectionRequest(), RuntimeDescriptor())
    once = AsyncMock(side_effect=[ValueError("SSH Connection refused"), candidate])
    sleep = AsyncMock()
    monkeypatch.setattr(module, "_prepare_once", once)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    states = []
    assert (
        await module.prepare_connection(ConnectionRequest(mode="ssh"), states.append) is candidate
    )
    assert once.await_count == 2
    sleep.assert_awaited_once_with(5)
    assert states[1]["next_retry_at"] is not None
    assert states[2]["attempt"] == 2


@pytest.mark.parametrize("message", ["Permission denied", "Host key verification failed"])
async def test_credentials_and_host_keys_are_not_retried(monkeypatch, message):
    from coordinator import model_connection as module

    once = AsyncMock(side_effect=ValueError(message))
    monkeypatch.setattr(module, "_prepare_once", once)
    with pytest.raises(ValueError, match=message):
        await module.prepare_connection(ConnectionRequest(mode="ssh"))
    assert once.await_count == 1


async def test_exhausted_retries_preserve_old_model(monkeypatch):
    from coordinator import model_connection as module

    service, control, _ = await setup_service(monkeypatch)
    monkeypatch.setattr("coordinator.service.prepare_connection", module.prepare_connection)
    once = AsyncMock(side_effect=ValueError("Connection refused"))
    monkeypatch.setattr(module, "_prepare_once", once)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    try:
        with pytest.raises(ModelServiceError, match="Connection refused"):
            await service.connect_model(ConnectionRequest(mode="ssh"))
        assert once.await_count == 4
        assert service._model_url == "http://old:8100"
        assert not service._model_switching
        assert service._connection_progress is None
        control.send.assert_not_awaited()
    finally:
        await service.close()


async def test_cancelled_attempt_terminates_owned_tunnel(monkeypatch):
    from unittest.mock import Mock

    from coordinator import model_connection as module

    process = Mock(returncode=None, wait=AsyncMock())
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(module, "describe_url", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await module.prepare_connection(ConnectionRequest(mode="ssh"))
    process.terminate.assert_called_once()
    process.wait.assert_awaited_once()
