"""Select the model-service backend from the environment.

``MODEL_BACKEND`` keeps the deterministic Mock runtime as the default while
allowing the same entry point and HTTP contract to serve a real HuggingFace
model, either on this machine or on a server reached through an SSH tunnel.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from .app import create_app
from .runtime import MockModelRuntime

MOCK_BACKENDS = {"mock", "mock_http", "deterministic"}
HF_BACKENDS = {"hf", "huggingface", "transformers", "remote_kv_aware"}


def selected_backend() -> str:
    return (os.getenv("MODEL_BACKEND") or "mock").strip().lower()


def build_app() -> FastAPI:
    backend = selected_backend()
    if backend in MOCK_BACKENDS:
        return create_app(MockModelRuntime())
    if backend in HF_BACKENDS:
        # Imported lazily so the Mock deployment never needs torch/transformers.
        from .hf_runtime import HuggingFaceKvModelRuntime

        runtime = HuggingFaceKvModelRuntime.from_env()
        return create_app(
            runtime,
            service_name="hf-model-service",
            title="Token KVCache HF Model Service",
            description="Real HuggingFace causal-LM runtime with range KV export/import.",
        )
    raise RuntimeError(
        f"unsupported MODEL_BACKEND '{backend}'; expected one of "
        f"{sorted(MOCK_BACKENDS | HF_BACKENDS)}"
    )


__all__ = ["HF_BACKENDS", "MOCK_BACKENDS", "build_app", "selected_backend"]
