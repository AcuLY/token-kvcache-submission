"""Validate a real server model through the HuggingFace KV runtime.

Unlike ``validate_gpu_model.py`` (a loopback TCP experiment), this drives the
actual ``HuggingFaceKvModelRuntime`` used by the Model Service: two isolated
logical sessions, real KV export/import, and continuation logits compared with
a from-scratch prefill of the same canonical tokens.

Example (on the server, inside its isolated venv)::

    PYTHONPATH=. python scripts/validate_server_model.py \
        --model "/home/lpdai/LLM Agent Communication/LLM Model/Llama-3.2-1B-Instruct" \
        --dtype bfloat16 --device cuda:0 --prompt-tokens 256 --max-new-tokens 8
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path

from model_service.hf_runtime import HuggingFaceKvModelRuntime
from shared.contracts import (
    CreateSessionRequest,
    GenerateRequest,
    GenerationConfig,
    PrefillRequest,
    SealRequest,
    TokenizeRequest,
    TransferManifest,
)
from shared.kv import (
    CacheExportRequest,
    CacheImportRequest,
    TokenImportRequest,
    prefix_digest,
    runtime_fingerprint,
)


async def new_session(runtime: HuggingFaceKvModelRuntime, agent_id: str) -> str:
    response = await runtime.create_session(
        CreateSessionRequest(agent_id=agent_id, conversation_id="server-validation")
    )
    return response.session.session_id


async def seed_prefix(
    runtime: HuggingFaceKvModelRuntime,
    session_id: str,
    tokens: list[int],
    state_id: str = "s0",
) -> None:
    await runtime.prefill(
        PrefillRequest(
            session_id=session_id,
            token_ids=tokens,
            state_id=state_id,
            replace=True,
            pinned_prefix_tokens=len(tokens),
        )
    )
    await runtime.seal(
        SealRequest(
            session_id=session_id,
            state_id=state_id,
            expected_token_count=len(tokens),
        )
    )


async def encode(runtime: HuggingFaceKvModelRuntime, text: str, *, bos: bool = False) -> list[int]:
    response = await runtime.tokenize(TokenizeRequest(text=text, add_bos=bos))
    return list(response.token_ids)


async def build_prompt(runtime: HuggingFaceKvModelRuntime, target_tokens: int) -> list[int]:
    tokens = await encode(
        runtime,
        "You participate in a Token/KV cache transfer experiment. Answer briefly.",
        bos=True,
    )
    record = 0
    while len(tokens) < target_tokens:
        record += 1
        tokens.extend(
            await encode(
                runtime,
                f"记录 {record:04d}：会话编号 {record * 17}；缓存前缀 {128 + record * 31} Token；"
                f"网络配置 {10 + record % 9 * 10} Mbps；测量重复次数 {3 + record % 5}。",
            )
        )
    return tokens[:target_tokens]


async def validate_mode(
    runtime: HuggingFaceKvModelRuntime,
    mode: str,
    prefix: list[int],
    suffix: list[int],
    probe: list[int],
    max_new_tokens: int,
) -> dict:
    sender = await new_session(runtime, f"sender-{mode}")
    await seed_prefix(runtime, sender, prefix)
    started = time.perf_counter()
    generated = await runtime.generate(
        GenerateRequest(
            session_id=sender,
            turn_id=1,
            suffix_token_ids=suffix,
            generation_config=GenerationConfig(max_new_tokens=max_new_tokens, temperature=0.0),
        )
    )
    generate_ms = (time.perf_counter() - started) * 1000
    await runtime.seal(
        SealRequest(
            session_id=sender,
            state_id=generated.state_id,
            expected_token_count=generated.token_count,
        )
    )

    receiver = await new_session(runtime, f"receiver-{mode}")
    export_ms = import_ms = 0.0
    wire_bytes = 0
    if mode == "KV_FULL":
        started = time.perf_counter()
        payload = await runtime.export_cache(
            CacheExportRequest(session_id=sender, state_id=generated.state_id, start_token=0)
        )
        export_ms = (time.perf_counter() - started) * 1000
        wire_bytes = len(base64.b64decode(payload.kv_base64))
        started = time.perf_counter()
        imported = await runtime.import_cache(
            CacheImportRequest(session_id=receiver, transfer_id=f"t-{mode}", payload=payload)
        )
        import_ms = (time.perf_counter() - started) * 1000
    elif mode == "KV_DELTA":
        await seed_prefix(runtime, receiver, prefix)
        started = time.perf_counter()
        payload = await runtime.export_cache(
            CacheExportRequest(
                session_id=sender,
                state_id=generated.state_id,
                base_state_id="s0",
                start_token=len(prefix),
                base_digest=prefix_digest(prefix),
                context_epoch=0,
            )
        )
        export_ms = (time.perf_counter() - started) * 1000
        wire_bytes = len(base64.b64decode(payload.kv_base64))
        started = time.perf_counter()
        imported = await runtime.import_cache(
            CacheImportRequest(session_id=receiver, transfer_id=f"t-{mode}", payload=payload)
        )
        import_ms = (time.perf_counter() - started) * 1000
    else:
        await seed_prefix(runtime, receiver, prefix)
        descriptor = await runtime.describe()
        manifest = TransferManifest(
            transfer_id=f"t-{mode}",
            conversation_id="server-validation",
            turn_id=1,
            source_agent=f"sender-{mode}",
            target_agent=f"receiver-{mode}",
            mode="TOKEN",
            scope="DELTA",
            state_id=generated.state_id,
            parent_state_id=generated.parent_state_id,
            start_token=len(prefix),
            base_state_id="s0",
            base_digest=prefix_digest(prefix),
            context_epoch=0,
            token_count=generated.token_count,
            payload_bytes=(generated.token_count - len(prefix)) * 4,
            network_snapshot_id="local",
            network_version=1,
            target_digest=prefix_digest(generated.token_ids),
            runtime_fingerprint=runtime_fingerprint(descriptor),
        )
        started = time.perf_counter()
        imported = await runtime.import_tokens(
            TokenImportRequest(
                session_id=receiver,
                manifest=manifest,
                token_ids=generated.token_ids[len(prefix) :],
            )
        )
        import_ms = (time.perf_counter() - started) * 1000

    if imported.token_ids != generated.token_ids:
        raise AssertionError("imported tokens differ from the generated state")

    reference_session = await new_session(runtime, f"reference-{mode}")
    await runtime.prefill(
        PrefillRequest(
            session_id=reference_session,
            token_ids=generated.token_ids,
            state_id="reference",
            replace=True,
        )
    )
    await runtime.seal(
        SealRequest(
            session_id=reference_session,
            state_id="reference",
            expected_token_count=generated.token_count,
        )
    )
    actual = await runtime.probe_logits(receiver, probe)
    reference = await runtime.probe_logits(reference_session, probe)
    max_error = max(abs(left - right) for left, right in zip(actual, reference, strict=True))
    descriptor = await runtime.describe()
    range_tokens = generated.token_count - (0 if mode == "KV_FULL" else len(prefix))
    expected_wire = (
        range_tokens * descriptor.kv_bytes_per_token + descriptor.kv_container_bytes
        if mode.startswith("KV")
        else 0
    )
    return {
        "mode": mode,
        "generated_tokens": len(generated.generated_token_ids),
        "context_tokens": generated.token_count,
        "transfer_range_tokens": range_tokens,
        "wire_bytes": wire_bytes,
        "expected_wire_bytes": expected_wire,
        "wire_bytes_match_declared_layout": wire_bytes == expected_wire,
        "generate_ms": round(generate_ms, 2),
        "export_ms": round(export_ms, 2),
        "import_ms": round(import_ms, 2),
        "probe_max_abs_logit_error": max_error,
        "probe_top1_equal": actual.index(max(actual)) == reference.index(max(reference)),
        "text": generated.text,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dtype", default="bfloat16")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--prompt-tokens", type=int, default=256)
    parser.add_argument("--max-new-tokens", type=int, default=8)
    parser.add_argument("--prefill-chunk-tokens", type=int, default=512)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    runtime = HuggingFaceKvModelRuntime.load(
        model_id=args.model,
        dtype=args.dtype,
        device=args.device,
        prefill_chunk_tokens=args.prefill_chunk_tokens,
        local_files_only=True,
    )
    descriptor = await runtime.describe()
    print("descriptor:", json.dumps(descriptor.model_dump(mode="json"), ensure_ascii=False))

    import torch

    print(
        "gpu:",
        torch.cuda.get_device_name(0),
        torch.cuda.get_device_capability(0),
        "sm120_in_arch_list",
        "sm_120" in torch.cuda.get_arch_list(),
    )

    prefix = await build_prompt(runtime, args.prompt_tokens)
    suffix = await encode(runtime, "\n[assistant] ", bos=False)
    probe = (await encode(runtime, "next", bos=False))[:2] or [1]
    started = time.perf_counter()
    await seed_prefix(runtime, await new_session(runtime, "warmup"), prefix[:64])
    print(f"warmup_ms={((time.perf_counter() - started) * 1000):.1f}")

    results = []
    for mode in ("KV_FULL", "KV_DELTA", "TOKEN_DELTA"):
        result = await validate_mode(
            runtime, mode, prefix, suffix, probe, args.max_new_tokens
        )
        results.append(result)
        print("mode:", json.dumps(result, ensure_ascii=False))

    report = {
        "model": args.model,
        "dtype": args.dtype,
        "device": args.device,
        "prompt_tokens": args.prompt_tokens,
        "descriptor": descriptor.model_dump(mode="json"),
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(0),
        "sm120_in_arch_list": "sm_120" in torch.cuda.get_arch_list(),
        "results": results,
    }
    # bf16/fp16 accumulation differs between incremental attention and a single
    # full-sequence forward; these are the tolerances validated by
    # validate_gpu_model.py (float32 0.002, float16 0.15, bfloat16 0.5).
    tolerance = {"float32": 0.002, "float16": 0.15, "bfloat16": 0.5}.get(
        descriptor.dtype, 0.5
    )
    report["logit_tolerance"] = tolerance
    report["passed"] = all(
        result["probe_top1_equal"]
        and result["probe_max_abs_logit_error"] <= tolerance
        and result["wire_bytes_match_declared_layout"]
        for result in results
    )
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("wrote", args.output)
    print("PASSED" if report["passed"] else "FAILED")


if __name__ == "__main__":
    asyncio.run(main())
