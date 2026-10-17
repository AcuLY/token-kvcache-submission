"""Policy tests independent of CUDA, model downloads or wall-clock timing."""

from copy import deepcopy

import pytest

from scripts.gpu_adaptive import CalibratedSelector


def profile():
    runtime = {
        "model": "same-model",
        "model_revision": "revision",
        "dtype": "float32",
        "initial_tokens": 8192,
        "prefill_chunk_tokens": 512,
        "tcp_chunk_bytes": 8388608,
        "receive_buffer_bytes": 16777216,
        "kv_layout": "stacked-layer-kv-v1",
        "rtt_ms": 1,
    }
    data = {**runtime, "passed": True, "rows": []}
    for bandwidth in (100, 10000):
        for mode in ("TOKEN_DELTA", "KV_DELTA"):
            tensor_bytes = 57344 * 100 if mode == "KV_DELTA" else 0
            payload = tensor_bytes + 400
            ideal = 1 + (payload + 1000) * 8 / (bandwidth * 1000)
            data["rows"].append(
                {
                    "mode": mode,
                    "new_tokens": 100,
                    "prefix_tokens": 8192,
                    "kv_tensor_bytes": tensor_bytes,
                    "payload_bytes": payload,
                    "frame_bytes": payload + 1000,
                    "bandwidth_mbps": bandwidth,
                    "ideal_shaped_wire_ms": ideal,
                    "wire_ms": ideal + 2,
                    "export_ms": 10 if mode == "KV_DELTA" else 0,
                    "restore_ms": 10 if mode == "KV_DELTA" else 100,
                }
            )
    return data, runtime


def test_bandwidth_changes_the_selected_mode_without_forcing_a_sequence():
    data, runtime = profile()
    selector = CalibratedSelector(data, runtime)
    sequence = [
        selector.choose(100, 8192, bw, 1, 57344)["selected_mode"] for bw in (100, 10000, 10000, 100)
    ]
    assert sequence == ["TOKEN_DELTA", "KV_DELTA", "KV_DELTA", "TOKEN_DELTA"]


def test_cheap_token_path_stays_token_even_at_high_bandwidth():
    data, runtime = profile()
    for row in data["rows"]:
        if row["mode"] == "TOKEN_DELTA":
            row["restore_ms"] = 1
    choice = CalibratedSelector(data, runtime).choose(100, 8192, 20000, 1, 57344)
    assert choice["selected_mode"] == "TOKEN_DELTA"


def test_calibration_is_frozen_and_cannot_be_changed_by_later_observations():
    data, runtime = profile()
    selector = CalibratedSelector(data, runtime)
    expected = selector.choose(100, 8192, 10000, 1, 57344)
    for row in data["rows"]:
        row["restore_ms"] = 100000
    assert selector.choose(100, 8192, 10000, 1, 57344) == expected


@pytest.mark.parametrize("field", ["dtype", "model_revision", "kv_layout", "tcp_chunk_bytes"])
def test_incompatible_calibration_is_rejected(field):
    data, runtime = profile()
    changed = deepcopy(runtime)
    changed[field] = "different"
    with pytest.raises(ValueError, match=field):
        CalibratedSelector(data, changed)


def test_failed_calibration_is_rejected():
    data, runtime = profile()
    data["passed"] = False
    with pytest.raises(ValueError, match="passed"):
        CalibratedSelector(data, runtime)
