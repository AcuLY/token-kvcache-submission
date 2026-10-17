"""Plan Token and KV ranges from the same canonical lineage and two coverage watermarks."""

from shared.contracts import NetworkState, StateOffer, TransferPlan
from shared.kv import CacheInventory, kv_wire_bytes


def plan_candidates(
    offer: StateOffer,
    inventory: CacheInventory,
    network: NetworkState,
    transfer_id: str,
    known_states: dict[str, tuple[int, int, str]],
    *,
    kv_bytes_per_token: int = 16,
    kv_container_bytes: int = 0,
) -> tuple[TransferPlan, TransferPlan]:
    if inventory.runtime_fingerprint != offer.runtime_fingerprint:
        raise ValueError("receiver runtime differs from offered state")
    known = known_states.get(inventory.known_state_id or "")
    if known and known != (
        inventory.known_token_count,
        inventory.context_epoch,
        inventory.known_token_digest,
    ):
        raise ValueError("receiver Token inventory differs from committed lineage")
    if not 0 <= inventory.token_count <= inventory.known_token_count:
        raise ValueError("KV coverage cannot exceed known Token coverage")
    aligned = known is not None and inventory.context_epoch == offer.context_epoch
    h = offer.shared_prefix_tokens or 0
    if aligned and inventory.known_token_count > h:
        raise ValueError("receiver is ahead of offered parent")
    token_start = inventory.known_token_count if aligned else 0
    kv_start = inventory.token_count if aligned else 0
    common = dict(
        protocol_version="2",
        transfer_id=transfer_id,
        conversation_id=offer.conversation_id,
        turn_id=offer.turn_id,
        source_agent=offer.source_agent,
        target_agent=offer.target_agent,
        state_id=offer.state_id,
        parent_state_id=offer.parent_state_id,
        token_count=offer.token_count,
        context_epoch=offer.context_epoch,
        output_token_count=offer.output_token_count,
        wrapper_token_count=offer.wrapper_token_count,
        target_digest=offer.target_digest,
        runtime_fingerprint=offer.runtime_fingerprint,
        receiver_known_tokens=token_start,
        receiver_cache_tokens=kv_start,
        network_snapshot_id=network.snapshot_id,
        network_state=network,
    )
    token = TransferPlan(
        **common,
        mode="TOKEN",
        scope="DELTA" if aligned else "RECOVERY",
        start_token=token_start,
        base_state_id=inventory.known_state_id if aligned else None,
        base_digest=inventory.known_token_digest if aligned else "",
        payload_bytes=(offer.token_count - token_start) * 4,
        recovery_reason=None if aligned else "epoch_rebase_or_token_history_missing",
    )
    kv = TransferPlan(
        **common,
        mode="KV",
        scope="DELTA" if kv_start else "FULL",
        start_token=kv_start,
        base_state_id=inventory.known_state_id if kv_start else None,
        base_digest=inventory.prefix_digest if kv_start else "",
        payload_bytes=kv_wire_bytes(
            offer.token_count - kv_start,
            kv_bytes_per_token,
            kv_container_bytes,
        ),
        kv_bytes_per_token=kv_bytes_per_token,
        kv_container_bytes=kv_container_bytes,
        recovery_reason=None if kv_start else "cache_missing_or_epoch_rebase",
    )
    return token, kv
