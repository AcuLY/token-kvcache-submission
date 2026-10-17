"""Causal, calibrated handoff estimator for the independent real-GPU experiment."""

from __future__ import annotations

import math
from statistics import median


class CalibratedSelector:
    """Use only completed calibration samples and the current network snapshot.

    The schedule and the current turn's measured candidates are deliberately not
    inputs. The calibration remains frozen throughout the evaluated conversation.
    """

    def __init__(self, calibration: dict, runtime: dict) -> None:
        if calibration.get("passed") is not True:
            raise ValueError("calibration must have passed correctness validation")
        for field in (
            "model",
            "model_revision",
            "dtype",
            "initial_tokens",
            "prefill_chunk_tokens",
            "tcp_chunk_bytes",
            "receive_buffer_bytes",
            "kv_layout",
            "rtt_ms",
        ):
            if calibration.get(field) != runtime.get(field):
                raise ValueError(f"calibration/runtime mismatch: {field}")
        self.chunk_bytes = runtime["tcp_chunk_bytes"]
        self.samples = {
            mode: [r.copy() for r in calibration["rows"] if r["mode"] == mode]
            for mode in ("TOKEN_DELTA", "KV_DELTA")
        }
        if any(not rows for rows in self.samples.values()):
            raise ValueError("both Token and KV DELTA calibration samples are required")

    def choose(
        self,
        new_tokens: int,
        prefix_tokens: int,
        bandwidth_mbps: float,
        rtt_ms: float,
        kv_bytes_per_token: int,
    ) -> dict:
        if new_tokens <= 0 or prefix_tokens <= 0 or bandwidth_mbps <= 0 or rtt_ms < 0:
            raise ValueError("invalid current handoff state")
        candidates = {}
        for mode, samples in self.samples.items():
            exporting = median(r["export_ms"] * new_tokens / r["new_tokens"] for r in samples)
            restoring = median(
                r["restore_ms"]
                * (
                    new_tokens / r["new_tokens"]
                    if mode == "TOKEN_DELTA"
                    else prefix_tokens / r["prefix_tokens"]
                )
                for r in samples
            )
            container_bytes = median(
                r["payload_bytes"] - r["new_tokens"] * 4 - r["kv_tensor_bytes"] for r in samples
            )
            payload_bytes = new_tokens * (4 + (kv_bytes_per_token if mode == "KV_DELTA" else 0))
            payload_bytes += container_bytes
            frame_bytes = payload_bytes + median(
                r["frame_bytes"] - r["payload_bytes"] for r in samples
            )
            # Windows timer overshoot depends on the duration of each shaped chunk.
            # Use the nearest already-calibrated bandwidth, never future turn measurements.
            nearest = min(
                samples, key=lambda r: abs(math.log(r["bandwidth_mbps"] / bandwidth_mbps))
            )
            neighbours = [r for r in samples if r["bandwidth_mbps"] == nearest["bandwidth_mbps"]]
            overshoot_ms = median(
                max(0, r["wire_ms"] - r["ideal_shaped_wire_ms"])
                / math.ceil(r["frame_bytes"] / self.chunk_bytes)
                for r in neighbours
            ) * math.ceil(frame_bytes / self.chunk_bytes)
            wire = rtt_ms + frame_bytes * 8 / (bandwidth_mbps * 1000) + overshoot_ms
            candidates[mode] = {
                "payload_bytes": payload_bytes,
                "export_ms": exporting,
                "restore_ms": restoring,
                "wire_ms": wire,
                "handoff_ms": exporting + wire + restoring,
                "timer_and_socket_overhead_ms": overshoot_ms,
            }
        selected = min(candidates, key=lambda name: candidates[name]["handoff_ms"])
        return {
            "selected_mode": selected,
            "reason": "lowest_calibrated_handoff_cost",
            "candidates": candidates,
            "token_minus_kv_ms": (
                candidates["TOKEN_DELTA"]["handoff_ms"] - candidates["KV_DELTA"]["handoff_ms"]
            ),
        }
