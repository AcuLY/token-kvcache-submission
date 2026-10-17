"""Reach a server-hosted model service over SSH, or run a local one.

The Coordinator and both Agent peers only ever need an HTTP base URL for the
model service. Two supported layouts:

* Local model (no SSH): run a real HuggingFace backend in this checkout with
  ``--local``, or use ``scripts/run_local.py --model-backend huggingface``.
* Server model: start the HuggingFace backend on the server and forward its
  port to localhost with an SSH tunnel. ``--with-stack`` then brings up the
  Coordinator and the two Agents against that tunnel.

Examples
--------
Start the real backend on the server and forward it to localhost:8100::

    python scripts/model_service_remote.py --host gpu-box \
        --remote-dir ~/token-kvcache --start-remote \
        --model Qwen/Qwen2.5-1.5B-Instruct --dtype bfloat16 --device cuda

Forward an already-running server model service::

    python scripts/model_service_remote.py --host gpu-box --tunnel-only

Run the real backend locally instead (no SSH)::

    python scripts/model_service_remote.py --local --model Qwen/Qwen2.5-0.5B-Instruct
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def ssh_options(*, port: int | None = None, identity: str | None = None) -> list[str]:
    """SSH connection flags shared by the tunnel and the remote start command."""

    options: list[str] = []
    if port:
        options += ["-p", str(port)]
    if identity:
        options += ["-i", identity]
    return options


def tunnel_command(
    host: str,
    *,
    local_port: int,
    remote_port: int,
    port: int | None = None,
    identity: str | None = None,
    remote_host: str = "127.0.0.1",
) -> list[str]:
    """Local port-forward from ``local_port`` to the server's model service."""

    return [
        "ssh",
        *ssh_options(port=port, identity=identity),
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-L",
        f"{local_port}:{remote_host}:{remote_port}",
        host,
    ]


def remote_start_command(
    host: str,
    *,
    remote_dir: str,
    remote_python: str = "python",
    model: str,
    dtype: str = "float32",
    device: str = "cuda",
    revision: str | None = None,
    remote_port: int = 8100,
    prefill_chunk_tokens: int = 512,
    max_context_tokens: int | None = None,
    port: int | None = None,
    identity: str | None = None,
) -> list[str]:
    """Background the real backend on the server and return immediately."""

    env = [
        "MODEL_BACKEND=huggingface",
        f"HF_MODEL_ID={shlex.quote(model)}",
        f"HF_DTYPE={shlex.quote(dtype)}",
        f"HF_DEVICE={shlex.quote(device)}",
        f"HF_PREFILL_CHUNK_TOKENS={prefill_chunk_tokens}",
        "MODEL_SERVICE_HOST=127.0.0.1",
        f"MODEL_SERVICE_PORT={remote_port}",
    ]
    if revision:
        env.append(f"HF_MODEL_REVISION={shlex.quote(revision)}")
    if max_context_tokens:
        env.append(f"HF_MAX_CONTEXT_TOKENS={max_context_tokens}")
    inner = (
        f"cd {shlex.quote(remote_dir)} && "
        f"{' '.join(env)} "
        f"nohup {shlex.quote(remote_python)} -m model_service "
        f"> model-service.log 2>&1 < /dev/null & echo model-service-started"
    )
    return [
        "ssh",
        *ssh_options(port=port, identity=identity),
        "-n",
        host,
        inner,
    ]


def local_env(args: argparse.Namespace) -> dict[str, str]:
    env = {
        **os.environ,
        "MODEL_BACKEND": "huggingface",
        "HF_MODEL_ID": args.model,
        "HF_DTYPE": args.dtype,
        "HF_DEVICE": args.device,
        "HF_PREFILL_CHUNK_TOKENS": str(args.prefill_chunk_tokens),
        "MODEL_SERVICE_HOST": "127.0.0.1",
        "MODEL_SERVICE_PORT": str(args.local_port),
    }
    if args.revision:
        env["HF_MODEL_REVISION"] = args.revision
    if args.max_context_tokens:
        env["HF_MAX_CONTEXT_TOKENS"] = str(args.max_context_tokens)
    return env


def wait_for_health(url: str, timeout_seconds: float, proc: subprocess.Popen[bytes]) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urlopen(f"{url}/health", timeout=2):
                return True
        except (OSError, URLError):
            time.sleep(1.0)
    return False


def require_ssh() -> str:
    binary = shutil.which("ssh")
    if binary is None:
        raise SystemExit("OpenSSH client 'ssh' was not found on PATH")
    return binary


def run_local(args: argparse.Namespace) -> None:
    env = local_env(args)
    url = f"http://127.0.0.1:{args.local_port}"
    print(f"Starting local HuggingFace model service: {args.model} ({args.dtype}, {args.device})")
    print(f"MODEL_SERVICE_URL={url}")
    command = [sys.executable, "-m", "model_service"]
    process = subprocess.Popen(command, cwd=ROOT, env=env)
    try:
        process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def run_remote(args: argparse.Namespace) -> None:
    require_ssh()
    url = f"http://127.0.0.1:{args.local_port}"
    if args.start_remote and not args.tunnel_only:
        start = remote_start_command(
            args.host,
            remote_dir=args.remote_dir,
            remote_python=args.remote_python,
            model=args.model,
            dtype=args.dtype,
            device=args.device,
            revision=args.revision,
            remote_port=args.remote_port,
            prefill_chunk_tokens=args.prefill_chunk_tokens,
            max_context_tokens=args.max_context_tokens,
            port=args.ssh_port,
            identity=args.identity,
        )
        print("Starting remote model service:", " ".join(start))
        result = subprocess.run(start, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise SystemExit(f"remote start failed: {result.stderr.strip()}")
        print(result.stdout.strip() or "remote model service start requested")

    tunnel = tunnel_command(
        args.host,
        local_port=args.local_port,
        remote_port=args.remote_port,
        port=args.ssh_port,
        identity=args.identity,
    )
    print("Opening SSH tunnel:", " ".join(tunnel))
    tunnel_process = subprocess.Popen(tunnel)
    children: list[subprocess.Popen[bytes]] = [tunnel_process]
    try:
        if not wait_for_health(url, args.wait_timeout, tunnel_process):
            raise SystemExit(
                "model service did not become healthy; check the remote log "
                "(model-service.log) and the tunnel options"
            )
        print(f"Model service ready. MODEL_SERVICE_URL={url}")
        if args.with_stack:
            stack = [
                sys.executable,
                str(ROOT / "scripts" / "run_local.py"),
                "--model-backend",
                "external",
                "--model-service-url",
                url,
                "--port",
                str(args.stack_port),
            ]
            print("Starting Coordinator and Agents:", " ".join(stack))
            children.append(subprocess.Popen(stack, cwd=ROOT))
        while all(child.poll() is None for child in children):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--local", action="store_true", help="run the real backend locally (no SSH)"
    )
    parser.add_argument("--host", help="SSH host or user@host of the model server")
    parser.add_argument("--ssh-port", type=int, help="SSH port on the server")
    parser.add_argument("--identity", help="path to the SSH private key")
    parser.add_argument(
        "--remote-port", type=int, default=8100, help="model service port on the server"
    )
    parser.add_argument(
        "--local-port",
        type=int,
        default=8100,
        help="localhost port for the forward or local run",
    )
    parser.add_argument("--remote-dir", default=".", help="repository path on the server")
    parser.add_argument("--remote-python", default="python", help="python executable on the server")
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--revision")
    parser.add_argument("--dtype", default="float32")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--prefill-chunk-tokens", type=int, default=512)
    parser.add_argument("--max-context-tokens", type=int)
    parser.add_argument(
        "--start-remote",
        action="store_true",
        help="launch the backend on the server first",
    )
    parser.add_argument("--tunnel-only", action="store_true", help="never launch a remote process")
    parser.add_argument(
        "--with-stack", action="store_true", help="also launch Coordinator and Agents"
    )
    parser.add_argument(
        "--stack-port",
        type=int,
        default=18080,
        help="Coordinator base port for --with-stack",
    )
    parser.add_argument(
        "--wait-timeout", type=float, default=900.0, help="seconds to wait for model load"
    )
    args = parser.parse_args(argv)
    if not args.local and not args.host:
        parser.error("--host is required unless --local is used")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.local:
        run_local(args)
    else:
        run_remote(args)


if __name__ == "__main__":
    main()
