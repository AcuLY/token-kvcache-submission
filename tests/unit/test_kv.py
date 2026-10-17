"""Cache safety and continuation checks independent of the coordinator."""

import base64

import httpx
import pytest

from agent_peer.model_client import HttpModelRuntimeClient
from model_service.app import create_app
from model_service.runtime import MockModelRuntime, StateConflictError
from shared.contracts import (
    CreateSessionRequest,
    GenerateRequest,
    GenerationConfig,
    PrefillRequest,
    SealRequest,
)
from shared.kv import CacheExportRequest, CacheImportRequest, CacheStatusRequest, prefix_digest


async def session(runtime: MockModelRuntime, agent: str) -> str:
    return (
        await runtime.create_session(
            CreateSessionRequest(agent_id=agent, conversation_id="conversation")
        )
    ).session.session_id


async def seed(runtime: MockModelRuntime, sid: str, tokens: list[int], state: str) -> None:
    await runtime.prefill(PrefillRequest(session_id=sid, token_ids=tokens, state_id=state))
    await runtime.seal(
        SealRequest(
            session_id=sid,
            state_id=state,
            expected_token_count=len(tokens),
        )
    )


@pytest.mark.asyncio
async def test_full_delta_and_token_prefill_have_identical_continuation() -> None:
    runtime = MockModelRuntime()
    a, b, c = [await session(runtime, "agent-a") for _ in range(3)]
    tokens = [256, 257, 258, 259]
    await seed(runtime, a, tokens, "target")
    await seed(runtime, b, tokens[:2], "base")
    # Token Prefill registers valid cache coverage and is a legal DELTA base.
    inventory = await runtime.cache_status(CacheStatusRequest(session_id=b))
    assert inventory.token_count == 2
    delta = await runtime.export_cache(
        CacheExportRequest(
            session_id=a,
            state_id="target",
            base_state_id="base",
            start_token=2,
            base_digest=inventory.prefix_digest,
        )
    )
    assert len(base64.b64decode(delta.kv_base64)) == 32
    request = CacheImportRequest(session_id=b, transfer_id="delta", payload=delta)
    restored = await runtime.import_cache(request)
    assert restored.token_ids == tokens
    assert await runtime.import_cache(request) == restored
    full = await runtime.export_cache(CacheExportRequest(session_id=a, state_id="target"))
    await runtime.import_cache(CacheImportRequest(session_id=c, transfer_id="full", payload=full))
    results = []
    for sid in [a, b, c]:
        generated = await runtime.generate(
            GenerateRequest(
                session_id=sid,
                turn_id=1,
                suffix_token_ids=[260],
                generation_config=GenerationConfig(),
            )
        )
        await runtime.seal(
            SealRequest(
                session_id=sid,
                state_id=generated.state_id,
                expected_token_count=generated.token_count,
            )
        )
        exported = await runtime.export_cache(
            CacheExportRequest(
                session_id=sid,
                state_id=generated.state_id,
            )
        )
        results.append((generated.token_ids, exported.kv_base64))
    assert results[0] == results[1] == results[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["blocks", "sidecar", "runtime", "epoch", "base", "range"])
async def test_invalid_delta_leaves_receiver_unchanged(corruption: str) -> None:
    runtime = MockModelRuntime()
    a, b = await session(runtime, "a"), await session(runtime, "b")
    await seed(runtime, a, [256, 257, 258], "target")
    await seed(runtime, b, [256], "base")
    before = await runtime.cache_status(CacheStatusRequest(session_id=b))
    payload = await runtime.export_cache(
        CacheExportRequest(
            session_id=a,
            state_id="target",
            start_token=1,
            base_state_id="base",
            base_digest=prefix_digest([256]),
        )
    )
    if corruption == "blocks":
        payload.kv_base64 = base64.b64encode(bytes(32)).decode()
    elif corruption == "sidecar":
        payload.token_ids = [259, 260]
    elif corruption == "runtime":
        payload.runtime = payload.runtime.model_copy(update={"revision": "wrong"})
    elif corruption == "epoch":
        payload.context_epoch = 1
    elif corruption == "base":
        payload.base_state_id = "stale"
    else:
        payload.token_count = 99
    with pytest.raises(StateConflictError):
        await runtime.import_cache(
            CacheImportRequest(
                session_id=b,
                transfer_id="bad",
                payload=payload,
            )
        )
    assert await runtime.cache_status(CacheStatusRequest(session_id=b)) == before


@pytest.mark.asyncio
async def test_remote_client_cache_contract_and_conflicting_retry() -> None:
    runtime = MockModelRuntime()
    a, b = await session(runtime, "a"), await session(runtime, "b")
    await seed(runtime, a, [256], "target")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime)),
        base_url="http://model",
    ) as http:
        client = HttpModelRuntimeClient("http://model", client=http)
        payload = await client.export_cache(CacheExportRequest(session_id=a, state_id="target"))
        request = CacheImportRequest(session_id=b, transfer_id="once", payload=payload)
        await client.import_cache(request)
        assert (await client.cache_status(CacheStatusRequest(session_id=b))).state_id == "target"
        request.payload.state_id = "other"
        response = await http.post("/v1/import-cache", json=request.model_dump(mode="json"))
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_eviction_window_rebuild_and_epoch_safe_full_recovery() -> None:
    runtime = MockModelRuntime()
    a, b = await session(runtime, "a"), await session(runtime, "b")
    await seed(runtime, a, [256] * 500, "old")
    evicted = await runtime.evict_cache(CacheStatusRequest(session_id=a))
    assert evicted.token_count == 0 and evicted.state_id is None
    with pytest.raises(StateConflictError):
        await runtime.export_cache(CacheExportRequest(session_id=a, state_id="old"))
    generated = await runtime.generate(
        GenerateRequest(
            session_id=a,
            turn_id=1,
            suffix_token_ids=[257],
            history_window_tokens=128,
            generation_config=GenerationConfig(max_new_tokens=32),
        )
    )
    assert generated.context_epoch == 1
    assert generated.trimmed_tokens == 453
    assert generated.token_count <= 128
    assert generated.timings.context_rebuild_ms > 0
    await runtime.seal(
        SealRequest(
            session_id=a,
            state_id=generated.state_id,
            expected_token_count=generated.token_count,
        )
    )
    payload = await runtime.export_cache(
        CacheExportRequest(
            session_id=a,
            state_id=generated.state_id,
            context_epoch=1,
        )
    )
    await runtime.import_cache(
        CacheImportRequest(session_id=b, transfer_id="rebase", payload=payload)
    )
    inventory = await runtime.cache_status(CacheStatusRequest(session_id=b))
    assert inventory.context_epoch == 1 and inventory.token_count == generated.token_count
    with pytest.raises(StateConflictError, match="epoch"):
        await runtime.prefill(PrefillRequest(session_id=b, token_ids=[256], context_epoch=0))
    assert await runtime.cache_status(CacheStatusRequest(session_id=b)) == inventory
