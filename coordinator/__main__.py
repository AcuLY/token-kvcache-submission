"""Run the Coordinator with ``python -m coordinator``."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "coordinator.app:app",
        host=os.getenv("COORDINATOR_HOST", "0.0.0.0"),
        port=int(os.getenv("COORDINATOR_PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    main()
