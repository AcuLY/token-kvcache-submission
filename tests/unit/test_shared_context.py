"""v2 semantics: shared prefix, output-level messages, cache holes, and safe replay."""

import pytest

from coordinator.decision import decide, paper_prefill_ms
from coordinator.planning import plan_candidates
from model_service.runtime import MockModelRuntime, StateConflictError
from shared.contracts import (
    AgentProfile,
    CreateSessionRequest,
    DecisionSettings,
    GenerateRequest,
    ModelBehaviorConfig,
    ModePolicy,
    NetworkState,
    PaperModelConfig,
    PrefillRequest,
    SealRequest,
    StateOffer,
    TransferManifest,
)
from shared.kv import (
    CacheExportRequest,
    CacheImportRequest,
    CacheStatusRequest,
    TokenImportRequest,
    prefix_digest,
    runtime_fingerprint,
)


async def setup(cached: int = 12):
    runtime = MockModelRuntime()
    sessions = []
    prefix = [256, 257, 258] * 4
    for agent in ["a", "b"]:
        sid = (
            await runtime.create_session(CreateSessionRequest(agent_id=agent, conversation_id="c"))
        ).session.session_id
        await runtime.prefill(
            PrefillRequest(
                session_id=sid,
                token_ids=prefix,
                state_id="initial",
                pinned_prefix_tokens=len(prefix),
            )
        )
        sessions.append(sid)
    a, b = sessions
    await runtime.evict_cache(CacheStatusRequest(session_id=b, retain_prefix_tokens=cached))
    generated = await runtime.generate(
        GenerateRequest(
            session_id=a,
            turn_id=1,
            profile=AgentProfile(agent_id="a", display_name="A"),
        )
    )
    await runtime.seal(
        SealRequest(
            session_id=a, state_id=generated.state_id, expected_token_count=generated.token_count
        )
    )
    offer = StateOffer(
        conversation_id="c",
        turn_id=1,
        source_agent="a",
        target_agent="b",
        state_id=generated.state_id,
        parent_state_id="initial",
        token_count=generated.token_count,
        payload_bytes=(generated.token_count - len(prefix)) * 4,
        shared_prefix_tokens=len(prefix),
        shared_prefix_digest=prefix_digest(prefix),
        target_digest=prefix_digest(generated.token_ids),
        output_token_count=len(generated.generated_token_ids),
        wrapper_token_count=generated.token_count
        - len(prefix)
        - len(generated.generated_token_ids),
        runtime_fingerprint=runtime_fingerprint(await runtime.describe()),
    )
    inventory = await runtime.cache_status(CacheStatusRequest(session_id=b))
    token, kv = plan_candidates(
        offer,
        inventory,
        NetworkState(source_agent="a", target_agent="b", bandwidth_mbps=100),
        "transfer",
        {"initial": (len(prefix), 0, prefix_digest(prefix))},
    )
    return runtime, a, b, generated, token, kv


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [0, 5, 12])
async def test_token_delta_reconstructs_only_missing_cache_without_resending_history(cached):
    runtime, _, b, generated, token, _ = await setup(cached)
    assert token.start_token == 12
    suffix = generated.token_ids[12:]
    assert token.payload_bytes == len(suffix) * 4
    before = await runtime.cache_status(CacheStatusRequest(session_id=b))
    assert before.known_token_count == 12 and before.token_count == cached
    request = TokenImportRequest(
        session_id=b, manifest=TransferManifest.from_plan(token), token_ids=suffix
    )
    result = await runtime.import_tokens(request)
    assert result.token_ids == generated.token_ids
    assert result.inventory.token_count == result.inventory.known_token_count
    assert result.inventory.prefix_digest == token.target_digest
    assert await runtime.import_tokens(request) == result
    changed = request.model_copy(deep=True)
    changed.token_ids[0] = 259
    with pytest.raises(StateConflictError, match="reused"):
        await runtime.import_tokens(changed)
    assert await runtime.cache_status(CacheStatusRequest(session_id=b)) == result.inventory


@pytest.mark.asyncio
async def test_kv_delta_backfills_historical_hole_without_duplicating_known_tokens():
    runtime, a, b, generated, _, kv = await setup(5)
    assert kv.start_token == 5
    exported = await runtime.export_cache(
        CacheExportRequest(
            session_id=a,
            state_id=kv.state_id,
            start_token=5,
            base_state_id=kv.base_state_id,
            base_digest=kv.base_digest,
        )
    )
    result = await runtime.import_cache(
        CacheImportRequest(session_id=b, transfer_id="kv", payload=exported)
    )
    assert result.token_ids == generated.token_ids
    restored = await runtime.export_cache(CacheExportRequest(session_id=b, state_id=kv.state_id))
    source = await runtime.export_cache(CacheExportRequest(session_id=a, state_id=kv.state_id))
    assert restored.kv_base64 == source.kv_base64


@pytest.mark.asyncio
async def test_stale_prefix_and_old_protocol_fail_before_mutation():
    runtime, _, b, generated, token, _ = await setup()
    before = await runtime.cache_status(CacheStatusRequest(session_id=b))
    for change in [{"base_digest": "wrong"}, {"protocol_version": "1"}, {"target_digest": "wrong"}]:
        request = TokenImportRequest(
            session_id=b,
            token_ids=generated.token_ids[12:],
            manifest=TransferManifest.from_plan(token).model_copy(update=change),
        )
        with pytest.raises(StateConflictError):
            await runtime.import_tokens(request)
        assert await runtime.cache_status(CacheStatusRequest(session_id=b)) == before


def test_paper_prefill_equation_and_history_dependence():
    p = PaperModelConfig(
        layers=2, heads=2, head_dim=3, model_dim=6, ff_dim=7, vocab_size=11, receiver_flops=1000
    )
    # Eq. (1), s=4, phi=4+5=9: projection + attention + FFN + output head.
    expected_flops = (
        2 * (6 * 2 * 3 * 6 * 4 + 4 * 2 * 3 * 9 * 4 + 2 * 2 * 3 * 6 * 4 + 4 * 6 * 7 * 4) + 2 * 6 * 11
    )
    assert paper_prefill_ms(4, 5, p) == expected_flops
    assert paper_prefill_ms(4, 10, p) > paper_prefill_ms(4, 5, p)


def test_paper_eq20_expansion_matches_direct_eq18_minus_eq19():
    p = PaperModelConfig(
        layers=2,
        heads=2,
        head_dim=3,
        model_dim=6,
        ff_dim=7,
        vocab_size=11,
        receiver_flops=1000,
        compression_ratio=2,
    )
    alpha, theta, xi, rate = 7, 20, 3, 1000
    direct = (
        paper_prefill_ms(alpha, theta, p)
        + p.token_bits * alpha / rate * 1000
        - 32 * p.layers * p.heads * p.head_dim * (xi + alpha) / p.compression_ratio / rate * 1000
    )
    k1 = 2 * p.layers * p.heads * p.head_dim
    k2 = p.layers * (8 * p.heads * p.head_dim * p.model_dim + 4 * p.model_dim * p.ff_dim)
    k3 = 2 * p.model_dim * p.vocab_size
    k4 = 2 * k1 / p.receiver_flops
    k5 = (k2 + 2 * k1 * theta) / p.receiver_flops + (
        p.token_bits - 16 * k1 / p.compression_ratio
    ) / rate
    k6 = k3 / p.receiver_flops - 16 * k1 * xi / p.compression_ratio / rate
    assert direct == pytest.approx((k4 * alpha**2 + k5 * alpha + k6) * 1000)


@pytest.mark.asyncio
async def test_paper_bits_and_explicit_recovery_objective():
    _, _, _, generated, token, kv = await setup()
    settings = DecisionSettings()
    _, decision = decide(
        token, kv, settings, ModelBehaviorConfig(), ModePolicy.ADAPTIVE, history_tokens=12
    )
    alpha = len(generated.generated_token_ids)
    p = settings.paper
    assert decision.effective_objective == "paper"
    assert decision.candidates[0].paper_bits == p.token_bits * alpha
    assert decision.candidates[1].paper_bits == 32 * p.layers * p.heads * p.head_dim * alpha
    assert decision.candidates[0].prefill_tokens == alpha + token.wrapper_token_count
    _, _, _, _, gap_token, gap_kv = await setup(5)
    _, gap = decide(
        gap_token, gap_kv, settings, ModelBehaviorConfig(), ModePolicy.ADAPTIVE, history_tokens=12
    )
    assert gap.xi_tokens == 7 and gap.effective_objective == "mode_e2e"
    assert not gap.candidates[0].paper_applicable


@pytest.mark.asyncio
async def test_paper_objective_switches_on_bandwidth_without_cache_eviction():
    _, _, _, _, token, kv = await setup()
    for bandwidth, expected in [(0.01, "TOKEN"), (1000, "KV")]:
        network = NetworkState(source_agent="a", target_agent="b", bandwidth_mbps=bandwidth)
        update = {"network_state": network, "network_snapshot_id": network.snapshot_id}
        chosen, decision = decide(
            token.model_copy(update=update),
            kv.model_copy(update=update),
            DecisionSettings(),
            ModelBehaviorConfig(),
            ModePolicy.ADAPTIVE,
            history_tokens=12,
        )
        assert chosen.mode == expected
        assert decision.effective_objective == "paper" and decision.xi_tokens == 0
