"""Run the deterministic model service using environment-backed bind settings."""

import os

import uvicorn

from .factory import build_app


def main() -> None:
    host = os.getenv("MODEL_SERVICE_HOST") or os.getenv("MOCK_MODEL_HOST") or "0.0.0.0"
    port = int(os.getenv("MODEL_SERVICE_PORT") or os.getenv("MOCK_MODEL_PORT") or "8100")
    uvicorn.run(
        build_app(),
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
