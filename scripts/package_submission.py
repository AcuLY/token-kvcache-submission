"""Create a source archive from the current checkout, without Git mutation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT_FILES = {
    "README.md",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "Dockerfile",
    "compose.yaml",
    ".dockerignore",
    ".gitignore",
}
SOURCE_DIRS = {"agent_peer", "coordinator", "model_service", "shared", "tests", "scripts"}
FRONTEND_FILES = {
    "package.json",
    "pnpm-lock.yaml",
    "index.html",
    ".gitignore",
    "eslint.config.ts",
    "eslint.config.js",
    "eslint.config.mjs",
    "vite.config.ts",
    "vitest.config.ts",
    "playwright.config.ts",
    "playwright.live.config.ts",
    "env.d.ts",
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.node.json",
    "README.md",
}
DOC_FILES = {
    "docs/README.md",
    "docs/reproduce.md",
    "docs/shared-context-v2.md",
    "docs/real-model-service.md",
}
TEXT_CODE = {".py", ".ps1", ".sh", ".txt"}
WEB_CODE = {".ts", ".js", ".vue", ".css", ".json", ".svg", ".png", ".ico", ".html"}
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
    "test-results",
    "playwright-report",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}


def include(path: Path) -> bool:
    """Explicit input classes; never archive an entire workspace recursively."""
    parts = path.parts
    if any(part in EXCLUDED_PARTS for part in parts) or path.name.startswith(".env"):
        return False
    if len(parts) == 1:
        return path.name in ROOT_FILES
    if parts[0] in SOURCE_DIRS:
        return path.suffix in TEXT_CODE
    if parts[0] == "frontend":
        if len(parts) == 2:
            return path.name in FRONTEND_FILES
        return parts[1] in {"src", "tests", "e2e", "public"} and path.suffix in WEB_CODE
    if parts[0] == "examples":
        return path.suffix in {".md", ".json", ".txt"}
    if parts[0] == "docs":
        return path.as_posix() in DOC_FILES
    return False


def git_value(root: Path, *args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, help="New ZIP path; existing files are not overwritten"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output or root / "dist" / f"token-kvcache-source-{stamp}.zip"
    selected = []
    # Walk only the intended roots, pruning dependency/cache directories before descent.
    for base, dirs, files in os.walk(root):
        relative = Path(base).relative_to(root)
        dirs[:] = [
            name
            for name in dirs
            if name not in EXCLUDED_PARTS
            and not Path(base, name).is_symlink()
            and (relative.parts or name in SOURCE_DIRS | {"frontend", "docs", "examples"})
        ]
        for name in files:
            file = Path(base, name)
            rel = file.relative_to(root)
            if not file.is_symlink() and include(rel):
                selected.append(rel)
    selected.sort()
    status = git_value(root, "status", "--porcelain")
    manifest = {
        "created_utc": datetime.now(UTC).isoformat(),
        "source": "current working tree, including selected uncommitted and untracked files",
        "base_commit": git_value(root, "rev-parse", "HEAD"),
        "dirty": None if status is None else bool(status),
        "file_count": len(selected),
        "files": [{"path": p.as_posix(), "bytes": (root / p).stat().st_size} for p in selected],
        "excluded": [
            "credentials and .env",
            "model weights",
            "virtual environments",
            "dependency and build caches",
            "raw connection exports",
            "author chat images",
            "design drafts and historical status documents",
            "experiment results and report materials",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel in selected:
            archive.write(root / rel, f"token-kvcache/{rel.as_posix()}")
        archive.writestr(
            "token-kvcache/SUBMISSION-MANIFEST.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
    print(f"Created {output.resolve()}")
    print(
        f"{len(selected)} source/documentation files + manifest; "
        f"{output.stat().st_size:,} ZIP bytes"
    )


if __name__ == "__main__":
    main()
