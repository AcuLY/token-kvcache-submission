"""Descriptor-driven KV payload sizing and wire splitting.

The Mock runtime keeps 16 bytes per position; a real HF backend reports larger
tensor bytes plus a fixed container. Both must size the plan, the manifest and
the P2P payload from the RuntimeDescriptor, not from a hard-coded constant.
"""

import base64

import pytest

from agent_peer.handlers import KvCacheHandler
from coordinator.planning import plan_candidates
from shared.contracts import (
    NetworkState,
    StateDescriptor,
    StateOffer,
    TransferManifest,
)
from shared.kv import CacheImportResponse, CacheInventory, kv_wire_bytes, prefix_digest

REAL_KV_BYTES = 256
REAL_CONTAINER_BYTES = 32


def build_offer_and_inventory() -> tuple[StateOffer, CacheInventory, dict]:
    prefix = list(range(10, 110))
    offer = StateOffer(
        conversation_id="conversation",
        turn_id=3,
        source_agent="agent-a",
        target_agent="agent-b",
        state_id="state-target",
        parent_state_id="state-parent",
        token_count=130,
        shared_prefix_tokens=100,
        output_token_count=25,
        wrapper_token_count=5,
        payload_bytes=30 * 4,
        target_digest=prefix_digest(list(range(10, 140))),
        shared_prefix_digest=prefix_digest(prefix),
        runtime_fingerprint="fingerprint",
        context_epoch=0,
    )
    inventory = CacheInventory(
        known_state_id="state-known",
        known_token_count=100,
        known_token_digest=prefix_digest(prefix),
        runtime_fingerprint="fingerprint",
        state_id="state-known-cache",
        token_count=60,
        context_epoch=0,
        prefix_digest=prefix_digest(prefix[:60]),
    )
    known_states = {"state-known": (100, 0, prefix_digest(prefix))}
    return offer, inventory, known_states


def network() -> NetworkState:
    return NetworkState(source_agent="agent-a", target_agent="agent-b", bandwidth_mbps=100)


def test_kv_wire_bytes_matches_mock_and_real_layouts() -> None:
    # Mock layout: 16 cache bytes per position and no container.
    assert kv_wire_bytes(70, 16) == 70 * 20
    # Real layout: declared tensor bytes plus the fixed container header.
    assert kv_wire_bytes(70, REAL_KV_BYTES, REAL_CONTAINER_BYTES) == 70 * (4 + REAL_KV_BYTES) + 32
    with pytest.raises(ValueError):
        kv_wire_bytes(-1, REAL_KV_BYTES)


def test_plan_candidates_uses_the_descriptor_kv_layout() -> None:
    offer, inventory, known_states = build_offer_and_inventory()
    token_plan, kv_plan = plan_candidates(
        offer,
        inventory,
        network(),
        "transfer-1",
        known_states,
        kv_bytes_per_token=REAL_KV_BYTES,
        kv_container_bytes=REAL_CONTAINER_BYTES,
    )
    # Token DELTA only carries the output range; KV DELTA carries [60, 130).
    assert token_plan.payload_bytes == (130 - 100) * 4
    assert kv_plan.payload_bytes == kv_wire_bytes(70, REAL_KV_BYTES, REAL_CONTAINER_BYTES)
    assert kv_plan.kv_bytes_per_token == REAL_KV_BYTES
    assert kv_plan.kv_container_bytes == REAL_CONTAINER_BYTES
    assert kv_plan.start_token == 60
    assert kv_plan.scope == "DELTA"


def test_plan_candidates_defaults_keep_the_mock_layout() -> None:
    offer, inventory, known_states = build_offer_and_inventory()
    _, kv_plan = plan_candidates(offer, inventory, network(), "transfer-2", known_states)
    assert kv_plan.payload_bytes == 70 * 20
    assert kv_plan.kv_bytes_per_token == 16
    assert kv_plan.kv_container_bytes == 0


def test_transfer_plan_rejects_a_mismatched_kv_size() -> None:
    from shared.contracts import TransferPlan

    with pytest.raises(ValueError):
        TransferPlan(
            conversation_id="conversation",
            turn_id=1,
            source_agent="agent-a",
            target_agent="agent-b",
            mode="KV",
            scope="FULL",
            state_id="state",
            token_count=10,
            payload_bytes=10 * 20,
            kv_bytes_per_token=REAL_KV_BYTES,
            kv_container_bytes=REAL_CONTAINER_BYTES,
            network_snapshot_id="net",
            network_state=network(),
        )


def real_manifest(start: int, count: int) -> TransferManifest:
    return TransferManifest(
        transfer_id="transfer-real",
        conversation_id="conversation",
        turn_id=1,
        source_agent="agent-a",
        target_agent="agent-b",
        mode="KV",
        scope="FULL" if start == 0 else "DELTA",
        state_id="state-target",
        parent_state_id="state-parent",
        start_token=start,
        base_state_id=None if start == 0 else "state-base",
        base_digest="" if start == 0 else prefix_digest(list(range(10, 10 + start))),
        token_count=count,
        payload_bytes=kv_wire_bytes(count - start, REAL_KV_BYTES, REAL_CONTAINER_BYTES),
        kv_bytes_per_token=REAL_KV_BYTES,
        kv_container_bytes=REAL_CONTAINER_BYTES,
        network_snapshot_id="net",
        network_version=1,
    )


def test_kv_handler_decodes_the_real_wire_layout() -> None:
    from shared.codec import encode_token_payload

    token_ids = list(range(10, 20))
    kv_bytes = bytes(REAL_CONTAINER_BYTES) + bytes(10 * REAL_KV_BYTES)
    payload = encode_token_payload(token_ids) + kv_bytes
    manifest = real_manifest(0, len(token_ids))
    assert len(payload) == kv_wire_bytes(len(token_ids), REAL_KV_BYTES, REAL_CONTAINER_BYTES)
    assert KvCacheHandler("FULL").decode(manifest, payload) == token_ids


def test_kv_handler_rejects_a_mock_sized_payload_for_a_real_manifest() -> None:
    from shared.codec import encode_token_payload

    token_ids = list(range(10, 20))
    payload = encode_token_payload(token_ids) + bytes(10 * 16)
    manifest = real_manifest(0, len(token_ids))
    with pytest.raises(ValueError):
        KvCacheHandler("FULL").decode(manifest, payload)


class RecordingModelClient:
    def __init__(self) -> None:
        self.imported = None

    async def describe(self):
        from shared.contracts import RuntimeDescriptor

        return RuntimeDescriptor(
            provider="huggingface",
            model_id="tiny",
            kv_cache_format="hf-stacked-kv-v1",
            kv_bytes_per_token=REAL_KV_BYTES,
            kv_container_bytes=REAL_CONTAINER_BYTES,
        )

    async def import_cache(self, request):
        self.imported = request
        descriptor = await self.describe()
        from shared.kv import CacheInventory

        return CacheImportResponse(
            inventory=CacheInventory(
                known_state_id=request.payload.state_id,
                state_id=request.payload.state_id,
                known_token_count=request.payload.token_count,
                token_count=request.payload.token_count,
                cache_format=descriptor.kv_cache_format,
            ),
            token_ids=request.payload.token_ids,
            elapsed_ms=0.5,
        )


async def test_kv_handler_restore_forwards_the_declared_cache_format() -> None:
    from shared.codec import encode_token_payload

    token_ids = list(range(10, 20))
    container = bytes(REAL_CONTAINER_BYTES) + bytes(10 * REAL_KV_BYTES)
    payload = encode_token_payload(token_ids) + container
    manifest = real_manifest(0, len(token_ids))
    model = RecordingModelClient()
    response = await KvCacheHandler("FULL").restore(model, "session", manifest, payload, token_ids)
    assert isinstance(response.state, StateDescriptor)
    assert response.state.state_id == "state-target"
    assert model.imported.payload.cache_format == "hf-stacked-kv-v1"
    assert base64.b64decode(model.imported.payload.kv_base64) == container
