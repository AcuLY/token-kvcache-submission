"""Paper Eqs. (1), (18)-(21), separate from measured-protocol cost estimates."""

from coordinator.providers import estimate_p2p_ms
from shared.contracts import (
    CandidateCost,
    DecisionRecord,
    DecisionSettings,
    ModelBehaviorConfig,
    ModePolicy,
    PaperModelConfig,
    TransferManifest,
    TransferPlan,
)


def paper_prefill_ms(tokens: int, history: int, model: PaperModelConfig) -> float:
    # Eq. (1), s=alpha and phi=alpha+theta. C is FLOP/s, output is milliseconds.
    s, phi = tokens, tokens + history
    layers, h, dh, dm, df, dv = (
        model.layers,
        model.heads,
        model.head_dim,
        model.model_dim,
        model.ff_dim,
        model.vocab_size,
    )
    flops = (
        layers
        * (6 * h * dh * dm * s + 4 * h * dh * phi * s + 2 * h * dh * dm * s + 4 * dm * df * s)
        + 2 * dm * dv
    )
    return flops / model.receiver_flops * 1000


def estimate_cost(
    plan: TransferPlan,
    settings: DecisionSettings,
    behavior: ModelBehaviorConfig,
) -> CandidateCost:
    frame_bytes = (
        4 + len(TransferManifest.from_plan(plan).model_dump_json().encode()) + plan.payload_bytes
    )
    p2p = estimate_p2p_ms(frame_bytes, plan.network_state)
    alpha = plan.output_token_count
    h = plan.token_count - alpha - plan.wrapper_token_count
    xi = max(0, h - plan.receiver_cache_tokens)
    paper = settings.paper
    rate = plan.network_state.bandwidth_mbps * 1e6
    applicable = (
        plan.receiver_known_tokens == h
        and plan.receiver_cache_tokens == h
        and not plan.recovery_reason
    )
    if plan.mode == "TOKEN":
        count = plan.token_count - plan.receiver_cache_tokens
        sender = settings.token_prepare_ms
        receiver = (
            settings.token_restore_overhead_ms
            + behavior.prefill_delay_ms
            + behavior.prefill_per_token_ms * count
        )
        bits = float(paper.token_bits * alpha)
        theory = paper_prefill_ms(alpha, plan.receiver_known_tokens, paper) + bits / rate * 1000
    elif plan.mode == "KV":
        count = 0
        sender = settings.kv_export_overhead_ms + behavior.cache_export_delay_ms
        receiver = settings.kv_import_overhead_ms + behavior.cache_import_delay_ms
        bits = (
            2
            * paper.kv_precision_bits
            * paper.layers
            * paper.heads
            * paper.head_dim
            * (xi + alpha)
            / paper.compression_ratio
        )
        theory = bits / rate * 1000
    else:
        raise ValueError(f"no cost model for {plan.mode}")
    total = sender + p2p + receiver
    objective = (
        theory
        if settings.objective == "paper"
        else p2p
        if settings.objective == "p2p_only"
        else total
    )
    return CandidateCost(
        mode=plan.mode,
        scope=plan.scope,
        payload_bytes=plan.payload_bytes,
        frame_bytes=frame_bytes,
        sender_ms=sender,
        p2p_ms=p2p,
        receiver_ms=receiver,
        mode_e2e_ms=total,
        paper_ms=theory,
        paper_bits=bits,
        paper_applicable=applicable,
        prefill_tokens=count,
        objective_ms=objective,
    )


def decide(
    token_plan: TransferPlan,
    kv_plan: TransferPlan,
    settings: DecisionSettings,
    behavior: ModelBehaviorConfig,
    policy: ModePolicy,
    *,
    history_tokens: int,
) -> tuple[TransferPlan, DecisionRecord]:
    costs = [estimate_cost(plan, settings, behavior) for plan in (token_plan, kv_plan)]
    effective = settings.objective
    fallback = effective == "paper" and not all(cost.paper_applicable for cost in costs)
    if fallback:
        effective = "mode_e2e"
        costs = [cost.model_copy(update={"objective_ms": cost.mode_e2e_ms}) for cost in costs]
    difference = costs[0].objective_ms - costs[1].objective_ms
    if policy == ModePolicy.ADAPTIVE:
        choose_kv = difference > 0
        reason = "kv_lower_marginal_cost" if choose_kv else "token_lower_or_equal_marginal_cost"
    else:
        choose_kv = policy == ModePolicy.ALWAYS_KV
        reason = "fixed_kv_policy" if choose_kv else "fixed_token_policy"
    if fallback:
        reason = "paper_assumptions_unmet_engineering:" + reason
    selected = kv_plan if choose_kv else token_plan
    cost = costs[int(choose_kv)]
    selected = selected.model_copy(
        update={"selector_reason": reason, "predicted_p2p_ms": cost.p2p_ms}
    )
    return selected, DecisionRecord(
        policy=policy,
        objective=settings.objective,
        effective_objective=effective,
        candidates=costs,
        token_minus_kv_ms=difference,
        selected_mode=selected.mode,
        selected_scope=selected.scope,
        reason=reason,
        cache_prefix_tokens=kv_plan.start_token,
        xi_tokens=max(0, history_tokens - kv_plan.start_token),
        current_tokens=token_plan.output_token_count,
        wrapper_tokens=token_plan.wrapper_token_count,
        theta_tokens=token_plan.receiver_known_tokens,
        model_behavior=behavior.model_dump(exclude={"response_prefix"}),
        overheads=settings.model_copy(deep=True),
    )
