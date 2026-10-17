"""Run the same four service entry points without Docker for development/QA."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument(
        "--model-backend",
        choices=["mock", "huggingface", "external"],
        default="mock",
        help="local Mock runtime, a local real HF runtime, or a model service you run elsewhere",
    )
    parser.add_argument(
        "--model-service-url",
        help="base URL of an existing model service when --model-backend external is used",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    base = args.port
    coordinator = f"http://127.0.0.1:{base}"
    if args.model_backend == "external":
        model = (args.model_service_url or "http://127.0.0.1:8100").rstrip("/")
    else:
        model = f"http://127.0.0.1:{base + 1}"
    services: list[tuple[str, dict[str, str], int]] = []
    if args.model_backend != "external":
        services.append(
            (
                "model_service",
                {
                    "MODEL_BACKEND": args.model_backend,
                    "MODEL_SERVICE_HOST": "127.0.0.1",
                    "MODEL_SERVICE_PORT": str(base + 1),
                },
                base + 1,
            )
        )
    services.append(
        (
            "coordinator",
            {
                "COORDINATOR_HOST": "127.0.0.1",
                "COORDINATOR_PORT": str(base),
                "MODEL_SERVICE_URL": model,
                "FRONTEND_DIST": os.getenv("FRONTEND_DIST", str(root / "frontend/dist")),
            },
            base,
        ),
    )
    for index, label in enumerate(("a", "b")):
        control, data = base + 2 + index, base + 4 + index
        services.append(
            (
                "agent_peer",
                {
                    "AGENT_ID": f"agent-{label}",
                    "COORDINATOR_URL": coordinator,
                    "MODEL_SERVICE_URL": model,
                    "CONTROL_BIND_HOST": "127.0.0.1",
                    "CONTROL_BIND_PORT": str(control),
                    "CONTROL_ADVERTISE_URL": f"http://127.0.0.1:{control}",
                    "DATA_BIND_HOST": "127.0.0.1",
                    "DATA_BIND_PORT": str(data),
                    "DATA_ADVERTISE_HOST": "127.0.0.1",
                    "DATA_ADVERTISE_PORT": str(data),
                },
                control,
            )
        )
    children: list[subprocess.Popen[bytes]] = []
    logs = Path(tempfile.mkdtemp(prefix="token-kvcache-"))
    print(f"Logs: {logs}", flush=True)
    if args.model_backend == "external":
        for _ in range(60):
            try:
                with urlopen(f"{model}/health", timeout=2):
                    break
            except OSError:
                time.sleep(1.0)
        else:
            raise RuntimeError(f"external model service {model} is not healthy")
        print(f"Using external model service: {model}", flush=True)
    try:
        for index, (module, overrides, port) in enumerate(services):
            with (logs / f"{module}-{index}.log").open("wb") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", module],
                    cwd=root,
                    env={**os.environ, **overrides},
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            children.append(process)
            # Loading local HF weights and initializing CUDA can take minutes.
            startup_timeout = (
                180 if module == "model_service" and args.model_backend == "huggingface" else 30
            )
            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"{module} exited; inspect {logs}")
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError(f"{module} not healthy; inspect {logs}")
        print(f"All services ready: {coordinator}", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)


if __name__ == "__main__":
    main()
