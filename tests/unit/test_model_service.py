from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from model_service.app import create_app
from model_service.runtime import MockModelRuntime, StateConflictError
from model_service.tokenizer import CONTROL_TOKEN_NAMES, ControlToken, MockByteTokenizer
from shared.codec import decode_token_transfer, encode_token_transfer
from shared.models import (
    AgentProfile,
    CreateSessionRequest,
    GenerateRequest,
    ModelBehaviorConfig,
    PrefillRequest,
    SealRequest,
    TokenizeRequest,
    TokenTransferManifest,
)


def profile(agent_id: str) -> AgentProfile:
    return AgentProfile(
        agent_id=agent_id,
        display_name=agent_id,
        role_description="Test role",
        system_prompt="Be deterministic.",
    )


def test_mock_byte_tokenizer_round_trips_unicode_without_control_collisions() -> None:
    tokenizer = MockByteTokenizer()
    text = "Token 往返 🌐\nnull:\x00"
    tokens = tokenizer.encode(text, add_bos=True, add_eos=True)

    assert tokens[0] == ControlToken.BOS
    assert tokens[-1] == ControlToken.EOS
    assert all(
        token in CONTROL_TOKEN_NAMES or tokenizer.byte_offset <= token < tokenizer.byte_limit
        for token in tokens
    )
    assert tokenizer.decode(tokens) == text
    assert "<|bos|>" in tokenizer.decode(tokens, skip_control_tokens=False)


def test_uint32_codec_round_trips_manifest_and_tokens() -> None:
    tokens = [1, 256, 511, 2**32 - 1]
    manifest = TokenTransferManifest(
        transfer_id="transfer-1",
        conversation_id="conversation-1",
        turn_id=1,
        source_agent="agent-a",
        target_agent="agent-b",
        state_id="state-1",
        token_count=len(tokens),
        payload_bytes=len(tokens) * 4,
        network_snapshot_id="network-1",
        network_version=1,
        applied_jitter_ms=-0.5,
    )

    decoded_manifest, decoded_tokens = decode_token_transfer(
        encode_token_transfer(manifest, tokens)
    )

    assert decoded_manifest == manifest
    assert decoded_tokens == tokens


@pytest.mark.asyncio
async def test_logical_sessions_are_isolated_and_generation_is_deterministic() -> None:
    runtime = MockModelRuntime()
    session_a = (
        await runtime.create_session(
            CreateSessionRequest(agent_id="agent-a", conversation_id="conversation-1")
        )
    ).session
    session_b = (
        await runtime.create_session(
            CreateSessionRequest(agent_id="agent-b", conversation_id="conversation-1")
        )
    ).session
    initial = (await runtime.tokenize(TokenizeRequest(text="shared", add_bos=True))).token_ids
    await runtime.prefill(
        PrefillRequest(
            session_id=session_a.session_id,
            token_ids=initial,
            state_id="shared-state",
        )
    )
    await runtime.prefill(
        PrefillRequest(
            session_id=session_b.session_id,
            token_ids=initial,
            state_id="shared-state",
        )
    )

    first = await runtime.generate(
        GenerateRequest(session_id=session_a.session_id, turn_id=0, profile=profile("agent-a"))
    )
    repeated = await runtime.generate(
        GenerateRequest(session_id=session_a.session_id, turn_id=0, profile=profile("agent-a"))
    )
    other = await runtime.generate(
        GenerateRequest(session_id=session_b.session_id, turn_id=0, profile=profile("agent-b"))
    )

    assert repeated == first
    assert first.token_ids[: len(initial)] == initial
    assert other.token_ids[: len(initial)] == initial
    assert first.agent_id == "agent-a"
    assert other.agent_id == "agent-b"
    assert first.text != other.text
    assert first.state_id != other.state_id


@pytest.mark.asyncio
async def test_prefill_and_seal_validate_exact_token_length() -> None:
    runtime = MockModelRuntime()
    session = (
        await runtime.create_session(
            CreateSessionRequest(agent_id="agent-a", conversation_id="conversation-1")
        )
    ).session
    prefilled = await runtime.prefill(
        PrefillRequest(session_id=session.session_id, token_ids=[ControlToken.BOS, 256])
    )

    with pytest.raises(StateConflictError):
        await runtime.seal(
            SealRequest(
                session_id=session.session_id,
                state_id=prefilled.state.state_id,
                expected_token_count=3,
            )
        )

    sealed = await runtime.seal(
        SealRequest(
            session_id=session.session_id,
            state_id=prefilled.state.state_id,
            expected_token_count=2,
        )
    )
    assert sealed.state.sealed is True
    assert sealed.state.token_count == 2


def test_http_contract_and_in_memory_behavior_configuration() -> None:
    runtime = MockModelRuntime()
    client = TestClient(create_app(runtime))

    assert client.get("/health").json() == {
        "status": "ok",
        "service": "mock-model-service",
    }
    descriptor = client.get("/descriptor")
    assert descriptor.status_code == 200
    assert descriptor.json()["tokenizer_id"] == "mock-byte-v1"

    behavior = ModelBehaviorConfig(
        tokenize_delay_ms=1,
        prefill_delay_ms=2,
        generate_delay_ms=3,
        response_prefix="Stable",
    )
    assert client.put("/behavior", json=behavior.model_dump(mode="json")).json() == (
        behavior.model_dump(mode="json")
    )
    assert client.post("/tokenize", json={"text": "abc"}).json()["elapsed_ms"] >= 0

    documented_paths = set(client.get("/openapi.json").json()["paths"])
    assert {
        "/health",
        "/descriptor",
        "/create-session",
        "/tokenize",
        "/render-local-input",
        "/prefill",
        "/generate",
        "/seal",
        "/behavior",
    } <= documented_paths
    assert {"/v1/cache-status", "/v1/export-cache", "/v1/import-cache"} <= documented_paths


def test_state_offer_schema_cannot_accept_token_arrays() -> None:
    from pydantic import ValidationError

    from shared.models import StateOffer

    with pytest.raises(ValidationError):
        StateOffer.model_validate(
            {
                "conversation_id": "conversation-1",
                "turn_id": 1,
                "source_agent": "agent-a",
                "target_agent": "agent-b",
                "state_id": "state-1",
                "token_count": 1,
                "payload_bytes": 4,
                "token_ids": [256],
            }
        )
