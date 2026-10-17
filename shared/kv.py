"""Versioned KV service contracts. Payloads stay off the control plane."""

import hashlib
import json

from pydantic import Field

from shared.codec import encode_token_payload
from shared.contracts import ContractModel, RuntimeDescriptor, TransferManifest


def prefix_digest(tokens: list[int]) -> str:
    return hashlib.sha256(encode_token_payload(tokens)).hexdigest()


def kv_wire_bytes(
    token_count: int,
    kv_bytes_per_token: int,
    kv_container_bytes: int = 0,
) -> int:
    """Wire size of a KV payload: uint32 Token sidecar plus cache bytes.

    The Mock runtime uses 16 bytes per position and no container overhead, so
    the historical 20 bytes/position layout is the default. A real HF backend
    reports its actual tensor size and serialization overhead through the
    RuntimeDescriptor, and the same formula sizes the planned payload.
    """

    if token_count < 0 or kv_bytes_per_token < 0 or kv_container_bytes < 0:
        raise ValueError("KV wire size inputs must be non-negative")
    return token_count * (4 + kv_bytes_per_token) + kv_container_bytes


class CacheStatusRequest(ContractModel):
    session_id: str
    retain_prefix_tokens: int = Field(default=0, ge=0)


class CacheInventory(ContractModel):
    known_state_id: str | None = None
    known_token_count: int = Field(default=0, ge=0)
    known_token_digest: str = ""
    runtime_fingerprint: str = ""
    initialization_ms: float = Field(default=0, ge=0)
    state_id: str | None = None
    token_count: int = Field(default=0, ge=0)
    context_epoch: int = Field(default=0, ge=0)
    prefix_digest: str = ""
    cache_format: str = "mock-kv-v1"


class CacheExportRequest(ContractModel):
    session_id: str
    state_id: str
    base_state_id: str | None = None
    start_token: int = Field(default=0, ge=0)
    base_digest: str = ""
    context_epoch: int = Field(default=0, ge=0)


class CachePayload(ContractModel):
    target_digest: str = ""
    cache_format: str = "mock-kv-v1"
    runtime: RuntimeDescriptor
    state_id: str
    parent_state_id: str | None = None
    base_state_id: str | None = None
    start_token: int = Field(default=0, ge=0)
    token_count: int = Field(ge=0)
    context_epoch: int = Field(default=0, ge=0)
    base_digest: str = ""
    token_ids: list[int]
    kv_base64: str


class CacheImportRequest(ContractModel):
    session_id: str
    transfer_id: str
    payload: CachePayload


class CacheImportResponse(ContractModel):
    inventory: CacheInventory
    token_ids: list[int]
    elapsed_ms: float = Field(ge=0)


def runtime_fingerprint(descriptor: RuntimeDescriptor) -> str:
    data = json.dumps([descriptor.protocol_version, *descriptor.compatibility_key()])
    return hashlib.sha256(data.encode()).hexdigest()


class TokenImportRequest(ContractModel):
    session_id: str
    manifest: TransferManifest
    token_ids: list[int]
