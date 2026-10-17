"""Run one configured Agent peer as a standalone service."""

import uvicorn

from .config import AgentSettings


def main() -> None:
    settings = AgentSettings()
    uvicorn.run(
        "agent_peer.app:app",
        host=settings.control_bind_host,
        port=settings.control_bind_port,
        reload=False,
    )


if __name__ == "__main__":
    main()

