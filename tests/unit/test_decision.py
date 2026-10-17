"""Paper marginal-cost criterion and engineering wire-size adaptation."""

import pytest

from coordinator.decision import decide, estimate_cost
from shared.contracts import (
    DecisionSettings,
    ModelBehaviorConfig,
    ModePolicy,
    NetworkState,
    TransferManifest,
    TransferPlan,
)


def plans(bandwidth: float = 100, n: int = 1000, k: int = 0):
    network = NetworkState(source_agent="a", target_agent="b", bandwidth_mbps=bandwidth)
    token = TransferPlan(
        conversation_id="test",
        turn_id=1,
        source_agent="a",
        target_agent="b",
        state_id="target",
        token_count=n,
        payload_bytes=n * 4,
        network_snapshot_id=network.snapshot_id,
        network_state=network,
    )
    kv = TransferPlan.model_validate(
        {
            **token.model_dump(),
            "mode": "KV",
            "scope": "DELTA" if k else "FULL",
            "start_token": k,
            "base_state_id": "base" if k else None,
            "base_digest": "prefix" if k else "",
            "payload_bytes": (n - k) * 20,
        }
    )
    return token, kv


def test_bandwidth_changes_sign_and_selected_mode():
    behavior = ModelBehaviorConfig(prefill_delay_ms=100)
    for bandwidth, expected in [(0.1, "TOKEN"), (100, "KV")]:
        selected, record = decide(
            *plans(bandwidth),
            DecisionSettings(),
            behavior,
            ModePolicy.ADAPTIVE,
            history_tokens=500,
        )
        assert selected.mode == expected
        assert (record.token_minus_kv_ms > 0) == (expected == "KV")
        assert record.xi_tokens == 500


def test_real_delta_size_and_framing_are_charged():
    token, kv = plans(n=1000, k=900)
    cost = estimate_cost(kv, DecisionSettings(), ModelBehaviorConfig())
    assert cost.payload_bytes == 2000  # KV blocks and token sidecar, not full cache.
    assert (
        cost.frame_bytes
        == 4 + len(TransferManifest.from_plan(kv).model_dump_json().encode()) + 2000
    )
    selected, record = decide(
        token,
        kv,
        DecisionSettings(objective="p2p_only"),
        ModelBehaviorConfig(cache_export_delay_ms=1000),
        ModePolicy.ADAPTIVE,
        history_tokens=950,
    )
    assert selected.mode == "KV"
    assert record.xi_tokens == 50
    assert record.candidates[1].mode_e2e_ms > record.candidates[0].mode_e2e_ms


def test_export_import_costs_and_common_cost_exclusion():
    token, kv = plans()
    settings = DecisionSettings()
    behavior = ModelBehaviorConfig(prefill_delay_ms=100)
    _, original = decide(token, kv, settings, behavior, ModePolicy.ADAPTIVE, history_tokens=500)
    behavior.generate_delay_ms = 50000
    behavior.tokenize_delay_ms = 50000
    _, common = decide(token, kv, settings, behavior, ModePolicy.ADAPTIVE, history_tokens=500)
    assert original.candidates == common.candidates
    behavior.cache_export_delay_ms = 80
    behavior.cache_import_delay_ms = 80
    selected, _ = decide(token, kv, settings, behavior, ModePolicy.ADAPTIVE, history_tokens=500)
    assert selected.mode == "TOKEN"


def test_tie_prefers_token_and_fixed_baselines_remain_fixed():
    token, kv = plans()
    settings = DecisionSettings()
    behavior = ModelBehaviorConfig()
    # Match all candidate costs exactly by using identical wire plans in this boundary test.
    selected, record = decide(
        token, token, settings, behavior, ModePolicy.ADAPTIVE, history_tokens=0
    )
    assert selected.mode == "TOKEN" and record.token_minus_kv_ms == 0
    for policy, expected in [(ModePolicy.ALWAYS_TOKEN, "TOKEN"), (ModePolicy.ALWAYS_KV, "KV")]:
        selected, _ = decide(token, kv, settings, behavior, policy, history_tokens=0)
        assert selected.mode == expected


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan")])
def test_nonphysical_overheads_are_rejected(value):
    with pytest.raises(ValueError):
        DecisionSettings(kv_import_overhead_ms=value)
