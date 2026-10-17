"""Pretrained CUDA experiment using the project's real TCP data plane.

Two isolated caches share one read-only model on one GPU. This is an opt-in
experiment, not the four-service Mock runtime or a two-device benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import platform
from pathlib import Path
from statistics import median
from time import perf_counter

import torch
import transformers
from gpu_adaptive import CalibratedSelector
from transformers import AutoModelForCausalLM, AutoTokenizer, DynamicCache

from agent_peer.transport import TcpByteTransport
from shared.codec import decode_token_payload, encode_token_payload
from shared.contracts import NetworkState, TransferManifest, WireReceipt
from shared.kv import prefix_digest


def timed(function):
    torch.cuda.synchronize()
    start = perf_counter()
    result = function()
    torch.cuda.synchronize()
    return result, (perf_counter() - start) * 1000


def clone_cache(cache):
    return DynamicCache.from_legacy_cache(
        tuple(tuple(t.clone() for t in layer) for layer in cache.to_legacy_cache())
    )


def pack_cache(cache, start):
    # One device-to-host copy rather than synchronizing once per key/value tensor.
    layers = torch.stack(
        [torch.stack([t[:, :, start:, :] for t in layer]) for layer in cache.to_legacy_cache()]
    ).cpu()
    stream = io.BytesIO()
    torch.save(layers, stream)
    return stream.getvalue(), layers.numel() * layers.element_size()


def unpack_cache(data, base, start, count):
    # Only tensors from our loopback experiment; never unpickle arbitrary classes.
    layers = torch.load(io.BytesIO(data), weights_only=True, map_location="cpu")
    expected = base.to_legacy_cache()
    if not isinstance(layers, torch.Tensor) or layers.ndim != 6 or len(layers) != len(expected):
        raise ValueError("KV layer count differs")
    exemplar = expected[0][0]
    expected_shape = (len(expected), 2, 1, exemplar.shape[1], count - start, exemplar.shape[3])
    if tuple(layers.shape) != expected_shape or layers.dtype != exemplar.dtype:
        raise ValueError("KV shape or precision differs")
    # One host-to-device copy; each cache layer keeps a view of its own tensor.
    layers = layers.cuda()
    restored = []
    for incoming, existing in zip(layers, expected, strict=True):
        if len(incoming) != 2:
            raise ValueError("expected key and value")
        pair = []
        for value, old in zip(incoming, existing, strict=True):
            if value.shape != (1, old.shape[1], count - start, old.shape[3]):
                raise ValueError("KV tensor shape differs")
            if value.dtype != old.dtype:
                raise ValueError("KV precision differs")
            pair.append(torch.cat([old[:, :, :start, :], value], dim=2) if start else value)
        restored.append(tuple(pair))
    return DynamicCache.from_legacy_cache(tuple(restored))


def lengthen_prompt(tokenizer, target_tokens, task, short_prompt):
    """Deterministic synthetic background; retain the entire task at the end."""
    if not target_tokens:
        return short_prompt
    opening = short_prompt + tokenizer.encode(
        "<|im_start|>user\n以下为通信实验的合成背景记录，仅用于长上下文压力测试。\n",
        add_special_tokens=False,
    )
    ending = tokenizer.encode(
        "\n背景结束。请根据最初角色继续讨论，不要复述背景。\n" + task + "<|im_end|>\n",
        add_special_tokens=False,
    )
    remaining = target_tokens - len(opening) - len(ending)
    if remaining < 1:
        raise ValueError("context-tokens must leave room for prompt, background and task")
    background = []
    record = 0
    while len(background) < remaining:
        record += 1
        background.extend(
            tokenizer.encode(
                f"记录 {record:04d}：会话编号 {record * 17}；缓存前缀 {128 + record * 31} Token；"
                f"网络配置 {10 + record % 9 * 10} Mbps；测量重复次数 {3 + record % 5}。"
                "发送端与接收端保持相同模型、相同规范历史，分别记录传输与恢复时间。\n",
                add_special_tokens=False,
            )
        )
    return opening + background[:remaining] + ending


async def main(args):
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA unavailable: install the optional GPU environment first")
    torch.set_num_threads(4)
    torch.manual_seed(7)
    torch.backends.cuda.matmul.allow_tf32 = False
    dtype = {
        "float32": torch.float32,
        "float16": torch.float16,
        "bfloat16": torch.bfloat16,
    }[args.dtype]
    print(f"Loading {args.model} on {torch.cuda.get_device_name(0)} ({args.dtype})", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=args.offline)
    model = (
        AutoModelForCausalLM.from_pretrained(
            args.model,
            dtype=dtype,
            attn_implementation="eager",
            local_files_only=args.offline,
        )
        .eval()
        .cuda()
    )
    torch.cuda.reset_peak_memory_stats()

    def forward(tokens, cache=None, *, chunk_tokens=None):
        chunk_tokens = chunk_tokens or args.prefill_chunk_tokens
        with torch.inference_mode():
            # Bound attention workspace and avoid an [all positions, vocabulary] logits tensor.
            for offset in range(0, len(tokens), chunk_tokens):
                output = model(
                    torch.tensor(
                        [tokens[offset : offset + chunk_tokens]], dtype=torch.long, device="cuda"
                    ),
                    past_key_values=cache,
                    use_cache=True,
                    logits_to_keep=1,
                )
                cache = output.past_key_values
            return output

    task = "设计一个可以验证 Token 传输与 KV Cache 传输差异的实验，说明如何保证比较公平。"
    prompt = tokenizer.apply_chat_template(
        [
            {
                "role": "system",
                "content": (
                    "你参与双 Agent 讨论。Agent A 负责提出具体实验方案；"
                    "Agent B 负责检查方案的漏洞并提出改进。所有上下文共享。"
                    "按照每轮指定身份，用中文简洁回复，不要代替另一个 Agent 发言。"
                ),
            },
            {"role": "user", "content": task},
        ],
        tokenize=True,
        add_generation_prompt=False,
    )
    prompt = lengthen_prompt(tokenizer, args.context_tokens, task, prompt)
    max_tokens = len(prompt) + args.turns * (args.max_new_tokens + 128) + 16
    if max_tokens > model.config.max_position_embeddings:
        raise ValueError("context plus turn budget exceeds the model's position limit")
    cfg = model.config
    kv_bytes_per_token = (
        2
        * cfg.num_hidden_layers
        * cfg.num_key_value_heads
        * (cfg.hidden_size // cfg.num_attention_heads)
        * model.dtype.itemsize
    )
    free_bytes, _ = torch.cuda.mem_get_info()
    # Two live sessions plus temporary candidate clones; eager attention workspace and headroom.
    additional_bytes = (
        8 * max_tokens * kv_bytes_per_token
        + 3 * cfg.num_attention_heads * args.prefill_chunk_tokens * max_tokens * 4
        + 2 * 1024**3
    )
    if additional_bytes > free_bytes:
        raise RuntimeError(
            f"preflight refused: estimated extra {additional_bytes / 1024**3:.2f} GiB "
            f"exceeds free {free_bytes / 1024**3:.2f} GiB; reduce context or prefill chunk"
        )
    print(
        f"Preflight: P0={len(prompt)} tokens; extra budget "
        f"{additional_bytes / 1024**3:.2f} GiB / free {free_bytes / 1024**3:.2f} GiB",
        flush=True,
    )
    forward(prompt)  # CUDA/kernel warm-up, excluded from measured initialization.
    left, init_a_ms = timed(lambda: forward(prompt))
    right, init_b_ms = timed(lambda: forward(prompt))
    caches = [left.past_key_values, right.past_key_values]
    histories = [list(prompt), list(prompt)]
    del left, right
    arrivals = asyncio.Queue()

    async def receive(reader, writer):
        try:
            manifest, data, elapsed = await TcpByteTransport.receive(reader)
            await arrivals.put((manifest, data))
            await TcpByteTransport.write_receipt(
                writer,
                WireReceipt(
                    transfer_id=manifest.transfer_id, accepted=True, receiver_wire_ms=elapsed
                ),
            )
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(
        receive,
        "127.0.0.1",
        0,
        limit=args.receive_buffer_bytes,
    )
    endpoint = f"127.0.0.1:{server.sockets[0].getsockname()[1]}"
    rows, conversation = [], []
    result = {
        "passed": False,
        "model": args.model,
        "model_revision": getattr(model.config, "_commit_hash", None),
        "gpu": torch.cuda.get_device_name(0),
        "dtype": args.dtype,
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "python": platform.python_version(),
        "cuda": torch.version.cuda,
        "scope": "one GPU, one model, two isolated caches, actual loopback TCP, shaped network",
        "not_validated": [
            "four-service HF adapter",
            "Coordinator adaptive policy",
            "two physical devices",
            "language quality benchmark",
        ],
        "kv_layout": "stacked-layer-kv-v1 plus uint32 Token sidecar; experiment only",
        "network_mbps": args.bandwidth,
        "bandwidth_schedule_mbps": args.bandwidth_schedule,
        "rtt_ms": args.rtt,
        "tcp_chunk_bytes": args.tcp_chunk_bytes,
        "receive_buffer_bytes": args.receive_buffer_bytes,
        "prefill_chunk_tokens": args.prefill_chunk_tokens,
        "reference_chunk_tokens": args.reference_chunk_tokens,
        "logits_to_keep": 1,
        "initial_prompt_digest": prefix_digest(prompt),
        "context_source": "synthetic numbered records" if args.context_tokens else "short task",
        "preflight_extra_bytes": additional_bytes,
        "preflight_free_bytes": free_bytes,
        "initial_tokens": len(prompt),
        "initialization_ms": [init_a_ms, init_b_ms],
        "max_new_tokens": args.max_new_tokens,
        "repeats": args.repeats,
        "task": task,
        "rows": rows,
        "conversation": conversation,
    }
    selector = None
    if args.adaptive_calibration:
        calibration = json.loads(args.adaptive_calibration.read_text(encoding="utf-8"))
        selector = CalibratedSelector(calibration, result)
        result["adaptive_calibration"] = str(args.adaptive_calibration)
        result["decisions"] = []
        result["selection_inputs"] = "frozen prior calibration and current network snapshot only"
    experiment_started = perf_counter()
    try:
        for turn in range(args.turns):
            sender, receiver = turn % 2, 1 - turn % 2
            name = "AB"[sender]
            assert histories[sender] == histories[receiver]
            prefix = histories[sender].copy()
            bandwidth = args.bandwidth_schedule[turn] if args.bandwidth_schedule else args.bandwidth
            instruction = (
                "请提出一个比较 Token 与 KV 传输耗时的具体实验步骤。"
                if sender == 0
                else "请指出上面实验方案中一个不公平的条件，并给出修正方法。"
            )
            wrapper = tokenizer.encode(
                f"<|im_start|>user\n你现在是 Agent {name}。{instruction}"
                "直接回答技术内容，限三句话，不要宣布轮到谁。"
                "<|im_end|>\n<|im_start|>assistant\n",
                add_special_tokens=False,
            )

            def generate(wrapper=wrapper, sender=sender):
                output = forward(wrapper, caches[sender])
                emitted = []
                for _ in range(args.max_new_tokens):
                    if not torch.isfinite(output.logits[:, -1]).all():
                        raise RuntimeError(f"non-finite generation logits with {args.dtype}")
                    token = int(output.logits[0, -1].argmax())
                    emitted.append(token)
                    # Materialize the LAST sampled token into KV as well.
                    output = forward([token], output.past_key_values)
                    if token == tokenizer.eos_token_id:
                        break
                ending = tokenizer.encode(
                    "\n" if emitted[-1] == tokenizer.eos_token_id else "<|im_end|>\n",
                    add_special_tokens=False,
                )
                output = forward(ending, output.past_key_values)
                return emitted, ending, output.past_key_values

            (emitted, ending, sender_cache), generation_ms = timed(generate)
            target = prefix + wrapper + emitted + ending
            caches[sender], histories[sender] = sender_cache, target
            conversation.append(
                {
                    "turn": turn + 1,
                    "agent": name,
                    "text": tokenizer.decode(emitted, skip_special_tokens=True),
                    "generated_tokens": len(emitted),
                    "generate_with_wrapper_ms": generation_ms,
                }
            )
            print(f"Turn {turn + 1} Agent {name}: {conversation[-1]['text']}", flush=True)
            # Independent whole-history recomputation detects position/append errors.
            probe = tokenizer.encode("<|im_start|>user\n", add_special_tokens=False)
            reference = (
                forward(target + probe, chunk_tokens=args.reference_chunk_tokens)
                .logits[:, -1]
                .float()
                .cpu()
            )
            network = NetworkState(
                source_agent=f"agent-{name.lower()}",
                target_agent=f"agent-{'AB'[receiver].lower()}",
                bandwidth_mbps=bandwidth,
                rtt_ms=args.rtt,
                version=turn + 1,
            )
            decision = None
            selected_mode = "TOKEN_DELTA" if turn % 2 == 0 else "KV_DELTA"
            if selector:
                decision = selector.choose(
                    len(target) - len(prefix),
                    len(prefix),
                    bandwidth,
                    args.rtt,
                    kv_bytes_per_token,
                )
                selected_mode = decision["selected_mode"]
                decision.update(
                    {
                        "turn": turn + 1,
                        "network": network.model_dump(mode="json"),
                        "decided_before_transfer": True,
                    }
                )
                result["decisions"].append(decision)
                print(
                    f"  DECISION turn={turn + 1} bandwidth={bandwidth:g} Mbps "
                    f"selected={selected_mode} "
                    f"predicted Token-KV={decision['token_minus_kv_ms']:.2f} ms",
                    flush=True,
                )
            adopted = None
            for repeat in range(args.repeats):
                # Rotate order to reduce consistent first-path timing bias.
                modes = list(args.modes)
                offset = (turn + repeat) % len(modes)
                modes = modes[offset:] + modes[:offset]
                if selector:
                    modes = [selected_mode] + [m for m in modes if m != selected_mode]
                for mode in modes:
                    start = 0 if mode == "KV_FULL" else len(prefix)
                    suffix = target[start:]

                    def prepare(suffix=suffix, mode=mode, sender_cache=sender_cache, start=start):
                        sidecar = encode_token_payload(suffix)
                        if mode == "TOKEN_DELTA":
                            return sidecar, 0
                        packed, tensor_bytes = pack_cache(sender_cache, start)
                        return sidecar + packed, tensor_bytes

                    (payload, tensor_bytes), export_ms = timed(prepare)
                    manifest = TransferManifest(
                        transfer_id=f"gpu-{turn}-{repeat}-{mode}",
                        conversation_id="pretrained-gpu-validation",
                        turn_id=turn + 1,
                        source_agent=network.source_agent,
                        target_agent=network.target_agent,
                        mode="TOKEN" if mode == "TOKEN_DELTA" else "HF_VALIDATION",
                        scope="FULL" if start == 0 else "DELTA",
                        state_id=f"turn-{turn + 1}",
                        base_state_id=f"turn-{turn}",
                        start_token=start,
                        token_count=len(target),
                        payload_bytes=len(payload),
                        base_digest=prefix_digest(target[:start]),
                        target_digest=prefix_digest(target),
                        network_snapshot_id=network.snapshot_id,
                        network_version=network.version,
                    )
                    # Budget for serialization and conservative per-chunk Windows timer overhead.
                    chunk_count = (len(payload) + 4096) // args.tcp_chunk_bytes + 1
                    timeout_seconds = max(
                        120,
                        30
                        + 2 * len(payload) * 8 / (bandwidth * 1_000_000)
                        + chunk_count * 0.03
                        + args.rtt / 1000,
                    )
                    transport = TcpByteTransport(
                        chunk_bytes=args.tcp_chunk_bytes,
                        timeout_seconds=timeout_seconds,
                    )
                    print(
                        f"  turn={turn + 1} {mode} {len(payload) / 1024**2:.2f} MiB; "
                        f"timeout={timeout_seconds:.0f}s",
                        flush=True,
                    )
                    receipt = await transport.send(endpoint, manifest, payload, network)
                    incoming, received = await asyncio.wait_for(arrivals.get(), timeout=5)
                    assert incoming == manifest and received == payload
                    # Clone/reset outside restore timing; every candidate starts at the same base.
                    base = clone_cache(caches[receiver])

                    def restore(
                        target=target,
                        start=start,
                        received=received,
                        prefix=prefix,
                        incoming=incoming,
                        mode=mode,
                        base=base,
                    ):
                        sidecar_size = (len(target) - start) * 4
                        ids = decode_token_payload(
                            received[:sidecar_size], expected_count=len(target) - start
                        )
                        rebuilt_tokens = prefix[:start] + ids
                        if prefix_digest(rebuilt_tokens) != incoming.target_digest:
                            raise ValueError("received canonical Token digest differs")
                        if mode == "TOKEN_DELTA":
                            return forward(ids, base).past_key_values
                        return unpack_cache(received[sidecar_size:], base, start, len(target))

                    restored, restore_ms = timed(restore)
                    assert restored.get_seq_length() == len(target)
                    actual = forward(probe, clone_cache(restored)).logits[:, -1].float().cpu()
                    error = float((actual - reference).abs().max())
                    top1_equal = bool(actual.argmax() == reference.argmax())
                    # FP16 accumulation differs between full and incremental attention.
                    tolerance = {"float32": 0.002, "float16": 0.15, "bfloat16": 0.5}[args.dtype]
                    torch.testing.assert_close(actual, reference, rtol=0, atol=tolerance)
                    assert top1_equal, "continuation top-1 differs from full recomputation"
                    if mode.startswith("KV"):
                        for got, expected in zip(
                            restored.to_legacy_cache(), sender_cache.to_legacy_cache(), strict=True
                        ):
                            for value, original in zip(got, expected, strict=True):
                                # Imported sender suffix must survive TCP/serialization bit-exactly.
                                assert torch.equal(value[:, :, start:], original[:, :, start:])
                    rows.append(
                        {
                            "turn": turn + 1,
                            "repeat": repeat + 1,
                            "mode": mode,
                            "execution_role": (
                                "selected" if mode == selected_mode else "isolated_counterfactual"
                            )
                            if selector
                            else "calibration_or_comparison",
                            "network_snapshot_id": network.snapshot_id,
                            "network_version": network.version,
                            "bandwidth_mbps": bandwidth,
                            "rtt_ms": args.rtt,
                            "prefix_tokens": len(prefix),
                            "new_tokens": len(target) - len(prefix),
                            "total_tokens": len(target),
                            "payload_bytes": len(payload),
                            "kv_tensor_bytes": tensor_bytes,
                            "frame_bytes": receipt.frame_bytes,
                            "timeout_seconds": timeout_seconds,
                            "export_ms": export_ms,
                            "wire_ms": receipt.sender_elapsed_ms,
                            "ideal_shaped_wire_ms": (
                                args.rtt + receipt.frame_bytes * 8 / (bandwidth * 1000)
                            ),
                            "restore_ms": restore_ms,
                            "handoff_ms": export_ms + receipt.sender_elapsed_ms + restore_ms,
                            "logits_max_abs_error": error,
                            "continuation_top1_equal": top1_equal,
                        }
                    )
                    if mode == selected_mode:
                        adopted = restored
            # Carry ONLY the selected received state into the next turn.
            # The other candidate has run on an isolated clone and is discarded.
            assert adopted is not None
            caches[receiver], histories[receiver] = adopted, target.copy()
            if decision is not None:
                observed = {r["mode"]: r for r in rows if r["turn"] == turn + 1}
                decision["observed_handoff_ms"] = {m: r["handoff_ms"] for m, r in observed.items()}
                decision["observed_fastest_mode"] = min(
                    observed, key=lambda m: observed[m]["handoff_ms"]
                )
                decision["carried_forward_mode"] = selected_mode
                decision["selection_was_fastest"] = (
                    decision["observed_fastest_mode"] == selected_mode
                )
                print(
                    f"  OBSERVED selected={observed[selected_mode]['handoff_ms']:.2f} ms; "
                    f"fastest={decision['observed_fastest_mode']}",
                    flush=True,
                )
            print(f"  turn={turn + 1} PASS; context={len(target)} tokens", flush=True)
        result["summary"] = {
            mode: {
                field: median(row[field] for row in rows if row["mode"] == mode)
                for field in ("payload_bytes", "export_ms", "wire_ms", "restore_ms", "handoff_ms")
            }
            for mode in args.modes
        }
        result["peak_allocated_mib"] = torch.cuda.max_memory_allocated() / 1024**2
        result["peak_reserved_mib"] = torch.cuda.max_memory_reserved() / 1024**2
        result["experiment_wall_ms_including_validation_and_counterfactuals"] = (
            perf_counter() - experiment_started
        ) * 1000
        if selector:
            decisions = result["decisions"]
            sequence = [d["selected_mode"] for d in decisions]
            result["adaptive_summary"] = {
                "selected_sequence": sequence,
                "switches": sum(a != b for a, b in zip(sequence, sequence[1:], strict=False)),
                "selection_fastest_count": sum(d["selection_was_fastest"] for d in decisions),
                "selected_handoff_total_ms": sum(
                    r["handoff_ms"] for r in rows if r["execution_role"] == "selected"
                ),
                "same_state_counterfactual_totals_ms": {
                    m: sum(r["handoff_ms"] for r in rows if r["mode"] == m) for m in args.modes
                },
                "counterfactuals_are_not_independent_fixed_policy_runs": True,
            }
        result["passed"] = True
    except Exception as exc:
        result["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        server.close()
        await server.wait_closed()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2), flush=True)
    if selector:
        print(json.dumps(result["adaptive_summary"], indent=2), flush=True)
    print(f"PASS: {len(rows)} transfers; results: {args.output.resolve()}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--dtype", choices=["float32", "float16", "bfloat16"], default="float32")
    parser.add_argument("--turns", type=int, default=6)
    parser.add_argument("--max-new-tokens", type=int, default=64)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--bandwidth", type=float, default=100)
    parser.add_argument("--bandwidth-schedule", type=lambda s: [float(x) for x in s.split(",")])
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=["TOKEN_DELTA", "KV_FULL", "KV_DELTA"],
        default=["TOKEN_DELTA", "KV_FULL", "KV_DELTA"],
    )
    parser.add_argument("--rtt", type=float, default=1)
    parser.add_argument("--context-tokens", type=int, default=0)
    parser.add_argument("--prefill-chunk-tokens", type=int, default=512)
    parser.add_argument("--reference-chunk-tokens", type=int, default=257)
    parser.add_argument("--tcp-chunk-bytes", type=int, default=16384)
    parser.add_argument("--receive-buffer-bytes", type=int, default=65536)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--adaptive-calibration", type=Path)
    parser.add_argument("--output", type=Path, default=Path("docs/experiments/gpu-qwen05b.json"))
    arguments = parser.parse_args()
    if arguments.adaptive_calibration:
        arguments.modes = ["TOKEN_DELTA", "KV_DELTA"]
        if arguments.repeats != 1:
            parser.error("adaptive evaluation requires --repeats 1")
    if (
        min(
            arguments.turns,
            arguments.max_new_tokens,
            arguments.repeats,
            arguments.prefill_chunk_tokens,
            arguments.reference_chunk_tokens,
            arguments.tcp_chunk_bytes,
            arguments.receive_buffer_bytes,
        )
        < 1
    ):
        parser.error("turn, output, repeat and chunk sizes must be positive")
    if arguments.context_tokens < 0 or arguments.bandwidth <= 0 or arguments.rtt < 0:
        parser.error("invalid context length or network parameters")
    if not {"TOKEN_DELTA", "KV_DELTA"}.issubset(arguments.modes):
        parser.error("Token and KV DELTA are required for cross-mode validation")
    if arguments.bandwidth_schedule and (
        len(arguments.bandwidth_schedule) != arguments.turns
        or any(x <= 0 for x in arguments.bandwidth_schedule)
    ):
        parser.error("bandwidth-schedule must contain one positive value per turn")
    asyncio.run(main(arguments))
