"""Opt-in CPU KV append validation with a tiny, randomly initialized Llama.

This validates real attention tensors, not language quality or a production Model Service adapter.
Install scripts/hf-validation-requirements.txt before running this script.
"""

import asyncio
import io

import torch
from transformers import DynamicCache, LlamaConfig, LlamaForCausalLM

from agent_peer.transport import TcpByteTransport
from shared.contracts import NetworkState, TransferManifest, WireReceipt


async def main() -> None:
    torch.manual_seed(7)
    torch.set_num_threads(1)
    config = LlamaConfig(
        vocab_size=128,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        max_position_embeddings=128,
    )
    model = LlamaForCausalLM(config).eval()
    prefix = torch.tensor([[1, 11, 12, 13, 14]])
    message = torch.tensor([[21, 22, 23]])
    next_token = torch.tensor([[31]])
    with torch.no_grad():
        sender = model(torch.cat([prefix, message], dim=1), use_cache=True)
        receiver = model(prefix, use_cache=True)
        by_token = model(message, past_key_values=receiver.past_key_values, use_cache=True)
        for actual, expected in zip(
            by_token.past_key_values.to_legacy_cache(),
            sender.past_key_values.to_legacy_cache(),
            strict=True,
        ):
            for value, reference in zip(actual, expected, strict=True):
                torch.testing.assert_close(value, reference, rtol=1e-5, atol=1e-6)
        base = model(prefix, use_cache=True).past_key_values.to_legacy_cache()
    k = prefix.shape[1]
    segments = tuple(
        tuple(value[:, :, k:, :].clone() for value in layer)
        for layer in sender.past_key_values.to_legacy_cache()
    )
    buffer = io.BytesIO()
    torch.save(segments, buffer)
    payload = buffer.getvalue()
    received: list[bytes] = []

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        manifest, data, elapsed = await TcpByteTransport.receive(reader)
        received.append(data)
        await TcpByteTransport.write_receipt(
            writer,
            WireReceipt(
                transfer_id=manifest.transfer_id,
                accepted=True,
                receiver_wire_ms=elapsed,
            ),
        )
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        manifest = TransferManifest(
            transfer_id="hf-validation",
            conversation_id="validation",
            turn_id=1,
            source_agent="a",
            target_agent="b",
            mode="HF_VALIDATION",
            scope="DELTA",
            state_id="target",
            start_token=k,
            token_count=k + message.shape[1],
            payload_bytes=len(payload),
            network_snapshot_id="validation",
            network_version=1,
        )
        await TcpByteTransport().send(
            f"127.0.0.1:{port}",
            manifest,
            payload,
            NetworkState(source_agent="a", target_agent="b", bandwidth_mbps=100),
        )
    finally:
        server.close()
        await server.wait_closed()
    assert received == [payload]
    imported = torch.load(io.BytesIO(received[0]), weights_only=True)
    joined = tuple(
        tuple(torch.cat([left, right], dim=2) for left, right in zip(a, b, strict=True))
        for a, b in zip(base, imported, strict=True)
    )
    with torch.no_grad():
        resumed = model(next_token, past_key_values=DynamicCache.from_legacy_cache(joined))
        reference = model(torch.cat([prefix, message, next_token], dim=1))
    torch.testing.assert_close(resumed.logits[:, -1], reference.logits[:, -1], rtol=1e-5, atol=1e-6)
    print(
        f"PASS: Token Prefill == KV append; GQA/RoPE; "
        f"TCP {len(payload)} bytes; resumed logits match"
    )
    print(f"torch={torch.__version__}; random tiny Llama, not deployed/pretrained model acceptance")


if __name__ == "__main__":
    asyncio.run(main())
