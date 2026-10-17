"""Environment-backed Agent endpoint configuration."""

from __future__ import annotations

from uuid import uuid4

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    agent_id: str = "agent-a"
    agent_display_name: str = "Agent A"
    instance_id: str = Field(default_factory=lambda: str(uuid4()))

    coordinator_url: str = "http://127.0.0.1:8000"
    model_service_url: str = "http://127.0.0.1:8100"
    control_advertise_url: str = "http://127.0.0.1:8201"
    control_bind_host: str = "0.0.0.0"
    control_bind_port: int = Field(default=8000, ge=1, le=65535)

    data_bind_host: str = "0.0.0.0"
    data_bind_port: int = Field(default=9100, ge=1, le=65535)
    data_advertise_host: str = "127.0.0.1"
    data_advertise_port: int = Field(default=9100, ge=1, le=65535)

    heartbeat_interval_seconds: float = Field(default=2.0, gt=0)
    # Real KV tensors cross the model-service link as well as the peer link.
    # Allow remote cache import to finish before the sender waits out its receipt.
    request_timeout_seconds: float = Field(default=120.0, gt=0)
    transfer_chunk_bytes: int = Field(default=16 * 1024, ge=1024, le=1024 * 1024)

    @property
    def data_bind(self) -> str:
        return f"{self.data_bind_host}:{self.data_bind_port}"

    @property
    def data_advertise(self) -> str:
        return f"{self.data_advertise_host}:{self.data_advertise_port}"


__all__ = ["AgentSettings"]
