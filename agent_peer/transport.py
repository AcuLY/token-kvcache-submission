"""Peer-to-peer TCP transport for TOKEN/FULL snapshots."""

from __future__ import annotations

import asyncio
import json
import struct
from time import perf_counter

from shared.codec import encode_token_transfer, read_token_transfer
from shared.contracts import (
    DataPlaneSendResult,
    NetworkState,
    TokenTransferManifest,
    TransferManifest,
    WireReceipt,
)
from shared.interfaces import LinkConditionApplier

from .network import DeterministicLinkConditionApplier

SendResult = DataPlaneSendResult


class TcpTokenTransport:
    def __init__(
        self,
        *,
        shaper: LinkConditionApplier | None = None,
        chunk_bytes: int = 16 * 1024,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._shaper = shaper or DeterministicLinkConditionApplier()
        self._chunk_bytes = chunk_bytes
        self._timeout_seconds = timeout_seconds

    async def send(
        self,
        endpoint: str,
        manifest: TokenTransferManifest,
        token_ids: list[int],
        network: NetworkState,
    ) -> DataPlaneSendResult:
        prepare_started = perf_counter()
        frame = encode_token_transfer(manifest, token_ids)
        sender_prepare_ms = (perf_counter() - prepare_started) * 1000
        return await self._send_frame(endpoint, manifest, frame, network, sender_prepare_ms)

    async def _send_frame(
        self, endpoint: str, manifest: TransferManifest, frame: bytes,
        network: NetworkState, sender_prepare_ms: float,
    ) -> DataPlaneSendResult:
        host, port = self.parse_endpoint(endpoint)

        started = perf_counter()
        reader: asyncio.StreamReader
        writer: asyncio.StreamWriter
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=self._timeout_seconds,
        )
        try:
            await asyncio.wait_for(
                self._shaper.write(
                    writer,
                    frame,
                    network,
                    chunk_bytes=self._chunk_bytes,
                ),
                timeout=self._timeout_seconds,
            )
            raw_receipt = await asyncio.wait_for(
                reader.readline(),
                timeout=self._timeout_seconds,
            )
            if not raw_receipt:
                raise ConnectionError("receiver closed before returning the wire receipt")
            receipt = WireReceipt.model_validate_json(raw_receipt)
            if receipt.transfer_id != manifest.transfer_id:
                raise ValueError("wire receipt transfer_id does not match the manifest")
            if not receipt.accepted:
                raise ConnectionError(receipt.detail or "receiver rejected token transfer")
            return DataPlaneSendResult(
                frame_bytes=len(frame),
                sender_prepare_ms=sender_prepare_ms,
                sender_elapsed_ms=(perf_counter() - started) * 1000,
                receiver_wire_ms=receipt.receiver_wire_ms,
            )
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    async def receive(
        reader: asyncio.StreamReader,
    ) -> tuple[TokenTransferManifest, list[int], float]:
        started = perf_counter()
        manifest, token_ids = await read_token_transfer(reader)
        return manifest, token_ids, (perf_counter() - started) * 1000

    @staticmethod
    async def write_receipt(
        writer: asyncio.StreamWriter,
        receipt: WireReceipt,
    ) -> None:
        payload = json.dumps(receipt.model_dump(mode="json"), separators=(",", ":"))
        writer.write(payload.encode("utf-8") + b"\n")
        await writer.drain()

    @staticmethod
    def parse_endpoint(endpoint: str) -> tuple[str, int]:
        value = endpoint.removeprefix("tcp://")
        try:
            host, raw_port = value.rsplit(":", 1)
            port = int(raw_port)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid P2P endpoint: {endpoint}") from exc
        if not host or not 1 <= port <= 65535:
            raise ValueError(f"invalid P2P endpoint: {endpoint}")
        return host, port


class TcpByteTransport(TcpTokenTransport):
    """Framing/network transport; payload interpretation belongs to a mode handler."""

    async def send(  # type: ignore[override]
        self, endpoint: str, manifest: TransferManifest, payload: bytes, network: NetworkState,
    ) -> DataPlaneSendResult:
        started = perf_counter()
        if len(payload) != manifest.payload_bytes:
            raise ValueError("payload length does not match plan")
        metadata = manifest.model_dump_json().encode('utf-8')
        frame = struct.pack('!I', len(metadata)) + metadata + payload
        return await self._send_frame(
            endpoint, manifest, frame, network, (perf_counter() - started) * 1000,
        )

    @staticmethod
    async def receive(  # type: ignore[override]
        reader: asyncio.StreamReader,
    ) -> tuple[TransferManifest, bytes, float]:
        started = perf_counter()
        size = struct.unpack('!I', await reader.readexactly(4))[0]
        if not 0 < size <= 1024 * 1024:
            raise ValueError("invalid manifest size")
        manifest = TransferManifest.model_validate_json(await reader.readexactly(size))
        payload = await reader.readexactly(manifest.payload_bytes)
        return manifest, payload, (perf_counter() - started) * 1000


__all__ = ["SendResult", "TcpTokenTransport", "TcpByteTransport", "WireReceipt"]
