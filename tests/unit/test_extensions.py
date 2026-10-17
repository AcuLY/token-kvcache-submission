"""Independent providers satisfy the same public ports without editing the FSM."""

import asyncio
import json

from agent_peer.handlers import TokenSnapshotHandler
from agent_peer.transport import TcpByteTransport, WireReceipt
from coordinator.providers import ProviderRegistry
from coordinator.schemas import EnvironmentDocument
from shared.contracts import NetworkState, StateOffer, TransferCandidate, TransferManifest


class JsonStateHandler(TokenSnapshotHandler):
    capability = "TEST_JSON_V1"
    mode = "TEST_JSON"

    def prepare(self, token_ids: list[int]) -> bytes:
        return json.dumps(token_ids).encode()

    def decode(self, manifest: TransferManifest, payload: bytes) -> list[int]:
        return json.loads(payload)


async def test_new_payload_handler_uses_unchanged_tcp_transport() -> None:
    handler = JsonStateHandler()
    tokens = [256, 257, 258]
    payload = handler.prepare(tokens)
    manifest = TransferManifest(
        transfer_id="test",
        conversation_id="conversation",
        turn_id=1,
        source_agent="a",
        target_agent="b",
        mode=handler.mode,
        scope=handler.scope,
        state_id="state",
        token_count=len(tokens),
        payload_bytes=len(payload),
        network_snapshot_id="network",
        network_version=1,
    )
    received: list[int] = []

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        wire_manifest, wire_payload, duration = await TcpByteTransport.receive(reader)
        received.extend(handler.decode(wire_manifest, wire_payload))
        await TcpByteTransport.write_receipt(
            writer,
            WireReceipt(
                transfer_id=wire_manifest.transfer_id,
                accepted=True,
                receiver_wire_ms=duration,
            ),
        )
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    network = NetworkState(source_agent="a", target_agent="b", bandwidth_mbps=100)
    try:
        port = server.sockets[0].getsockname()[1]
        await TcpByteTransport().send(f"127.0.0.1:{port}", manifest, payload, network)
        assert received == tokens
    finally:
        server.close()
        await server.wait_closed()


def test_independent_network_and_candidate_providers() -> None:
    class Network:
        provider_id = "test_hardware"
        contributes_mock_ui = False

        def snapshot(self, source_agent: str, target_agent: str, transfer_id: str) -> NetworkState:
            return NetworkState(
                source_agent=source_agent,
                target_agent=target_agent,
                snapshot_id=transfer_id,
                bandwidth_mbps=3,
                rtt_ms=20,
            )

    class Handler:
        provider_id = "test_token"
        mode = "TOKEN"
        scope = "FULL"

        def candidate(self, offer: StateOffer, network: NetworkState) -> TransferCandidate:
            return TransferCandidate(
                token_count=offer.token_count, payload_bytes=offer.payload_bytes
            )

    registry = ProviderRegistry(
        environment=EnvironmentDocument(network_provider="test_hardware"),
        network=Network(),
        transfer_handlers={"test_token": Handler()},
    )
    offer = StateOffer(
        conversation_id="conversation",
        turn_id=1,
        source_agent="a",
        target_agent="b",
        state_id="state",
        token_count=3,
        payload_bytes=12,
    )
    candidate = registry.transfer_candidates(
        offer, registry.network.snapshot("a", "b", "transfer"),
    )[0]
    assert candidate.payload_bytes == 12
    assert registry.capabilities()["ui_extensions"] == []
