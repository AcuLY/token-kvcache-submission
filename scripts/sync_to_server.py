"""Push this checkout to a remote model-service workspace and restart it.

The model service already lives in this repository (``model_service/``); this
script is the missing update path so the server copy is refreshed from a known
commit instead of a hand-made snapshot. The remote workspace keeps its own
isolated venv, so only code is replaced.

Example::

    python scripts/sync_to_server.py --host token-kv --restart-service

Note on shared GPU hosts: some servers throttle new SSH connections per source
IP, and a failed attempt can leave a local ``ssh`` process holding the slot.
If a run fails, remove stale ``ssh`` processes and wait before retrying; the
script retries with a delay but cannot clear those processes for you.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_TOP_LEVEL = {"frontend", ".git", ".venv", ".tmp", "token_kvcache.egg-info"}
EXCLUDED_ANYWHERE = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
EXCLUDED_PREFIXES = ("docs/experiments",)


def _excluded(relative: str) -> bool:
    parts = relative.split("/")
    if any(part in EXCLUDED_ANYWHERE for part in parts):
        return True
    if parts[0] in EXCLUDED_TOP_LEVEL:
        return True
    return relative.startswith(EXCLUDED_PREFIXES)


def build_payload(destination: Path, root: Path = ROOT) -> Path:
    """Archive the service-relevant files; the frontend and caches are skipped."""

    with tarfile.open(destination, "w:gz") as archive:
        for current, dirnames, filenames in os.walk(root):
            current_path = Path(current)
            relative_dir = current_path.relative_to(root).as_posix()
            prefix = "" if relative_dir == "." else f"{relative_dir}/"
            dirnames[:] = sorted(
                name for name in dirnames if not _excluded(f"{prefix}{name}")
            )
            for name in sorted(filenames):
                relative = f"{prefix}{name}"
                if _excluded(relative):
                    continue
                archive.add(current_path / name, arcname=relative, recursive=False)
    return destination


def ssh_options(args: argparse.Namespace) -> list[str]:
    options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=25"]
    if args.ssh_port:
        options += ["-p", str(args.ssh_port)]
    if args.identity:
        options += ["-i", args.identity]
    return options


def run(command: list[str], *, attempts: int, delay: float, description: str) -> str:
    last_error = ""
    for attempt in range(1, attempts + 1):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
        last_error = (result.stderr or result.stdout).strip()
        print(f"{description}: attempt {attempt}/{attempts} failed", file=sys.stderr)
        if attempt < attempts:
            time.sleep(delay)
    raise SystemExit(f"{description} failed after {attempts} attempts: {last_error}")


def run_with_stdin(
    command: list[str],
    *,
    stdin: Path,
    attempts: int,
    delay: float,
    description: str,
) -> str:
    last_error = ""
    for attempt in range(1, attempts + 1):
        with stdin.open("rb") as handle:
            result = subprocess.run(command, stdin=handle, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
        last_error = (result.stderr or result.stdout).strip()
        print(f"{description}: attempt {attempt}/{attempts} failed", file=sys.stderr)
        if attempt < attempts:
            time.sleep(delay)
    raise SystemExit(f"{description} failed after {attempts} attempts: {last_error}")


def remote_script(args: argparse.Namespace, *, restart: bool, stream: bool) -> str:
    workspace = shlex.quote(args.remote_workspace)
    repo = shlex.quote(args.remote_repo)
    payload = shlex.quote(args.remote_payload)
    launcher = shlex.quote(args.launcher)
    log = shlex.quote(f"{args.remote_workspace}/logs/model-service.log")
    steps = [
        f"mkdir -p {repo}",
        f"tar -xzf {'-' if stream else payload} -C {repo}",
        f'echo "synced files: $(find {repo} -type f | wc -l)"',
    ]
    if restart:
        # The bracket expression keeps the pattern from matching this very
        # command line, which would otherwise kill the SSH session itself.
        steps += [
            'pkill -f "[.]venv/bin/python -m model_service"',
            "sleep 2",
            f"mkdir -p {workspace}/logs",
            f"setsid nohup {launcher} > {log} 2>&1 < /dev/null &",
            "sleep 5",
            "for i in $(seq 1 40); do sleep 3; "
            'if curl -sf "${MODEL_HEALTH_URL:-http://127.0.0.1:18100/health}" '
            ">/dev/null 2>&1; then echo health-ok; break; fi; done",
            "curl -s --max-time 10 "
            '"${MODEL_HEALTH_URL:-http://127.0.0.1:18100/health}" || true',
        ]
    # Newline-separated so a trailing "&" for the background launcher stays a
    # valid command terminator; "cmd & ; next" is a shell syntax error.
    return "\n".join(steps)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="token-kv", help="SSH host alias or user@host")
    parser.add_argument("--ssh-port", type=int)
    parser.add_argument("--identity")
    parser.add_argument(
        "--remote-workspace",
        default="/home/lpdai/LLM Agent Communication/YLJ",
        help="isolated workspace on the server",
    )
    parser.add_argument(
        "--remote-repo",
        help="repo path (defaults to <workspace>/src/token-kvcache)",
    )
    parser.add_argument("--remote-payload", default="/tmp/tokenkvcache_payload.tar.gz")
    parser.add_argument(
        "--launcher",
        help="launcher path (defaults to <workspace>/run-model-service.sh)",
    )
    parser.add_argument("--restart-service", action="store_true")
    parser.add_argument(
        "--scp",
        action="store_true",
        help="upload with scp first instead of streaming the archive over one ssh connection",
    )
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--retry-delay", type=float, default=20.0)
    parser.add_argument("--keep-payload", action="store_true")
    args = parser.parse_args(argv)
    if not args.remote_repo:
        args.remote_repo = f"{args.remote_workspace}/src/token-kvcache"
    if not args.launcher:
        args.launcher = f"{args.remote_workspace}/run-model-service.sh"
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if shutil.which("ssh") is None or shutil.which("scp") is None:
        raise SystemExit("OpenSSH client (ssh/scp) was not found on PATH")

    with tempfile.TemporaryDirectory(prefix="token-kvcache-sync-") as temp_dir:
        payload = build_payload(Path(temp_dir) / "payload.tar.gz")
        size_mb = payload.stat().st_size / 1024 / 1024
        print(f"payload: {size_mb:.2f} MB")

        stream = not args.scp
        if not stream:
            run(
                [
                    "scp",
                    *ssh_options(args),
                    str(payload),
                    f"{args.host}:{args.remote_payload}",
                ],
                attempts=args.attempts,
                delay=args.retry_delay,
                description="scp",
            )
        script = remote_script(args, restart=args.restart_service, stream=stream)
        if stream:
            # One SSH connection carries both the archive and the commands,
            # which matters on hosts that throttle new connections per source.
            output = run_with_stdin(
                ["ssh", *ssh_options(args), args.host, script],
                stdin=payload,
                attempts=args.attempts,
                delay=args.retry_delay,
                description="remote sync (streamed)",
            )
        else:
            output = run(
                ["ssh", *ssh_options(args), args.host, script],
                attempts=args.attempts,
                delay=args.retry_delay,
                description="remote sync",
            )
        print(output.strip())


if __name__ == "__main__":
    main()
