"""HTTP implementation of the replaceable model runtime client port."""

from __future__ import annotations

from typing import TypeVar

import httpx
from pydantic import BaseModel

from shared.contracts import (
    CreateSessionRequest,
    CreateSessionResponse,
    GenerateRequest,
    GenerateResponse,
    PrefillRequest,
    PrefillResponse,
    RenderLocalInputRequest,
    RenderLocalInputResponse,
    RuntimeDescriptor,
    SealRequest,
    SealResponse,
    TokenizeRequest,
    TokenizeResponse,
)
from shared.kv import (
    CacheExportRequest,
    CacheImportRequest,
    CacheImportResponse,
    CacheInventory,
    CachePayload,
    CacheStatusRequest,
    TokenImportRequest,
)


class ModelRuntimeClientError(RuntimeError):
    pass


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class HttpModelRuntimeClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            trust_env=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def describe(self) -> RuntimeDescriptor:
        return await self._get("/v1/descriptor", RuntimeDescriptor)

    async def create_session(self, request: CreateSessionRequest) -> CreateSessionResponse:
        return await self._post("/v1/create-session", request, CreateSessionResponse)

    async def tokenize(self, request: TokenizeRequest) -> TokenizeResponse:
        return await self._post("/v1/tokenize", request, TokenizeResponse)

    async def render_local_input(
        self, request: RenderLocalInputRequest
    ) -> RenderLocalInputResponse:
        return await self._post("/v1/render-local-input", request, RenderLocalInputResponse)

    async def prefill(self, request: PrefillRequest) -> PrefillResponse:
        return await self._post("/v1/prefill", request, PrefillResponse)

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        return await self._post("/v1/generate", request, GenerateResponse)

    async def seal(self, request: SealRequest) -> SealResponse:
        return await self._post("/v1/seal", request, SealResponse)

    async def import_tokens(self, request: TokenImportRequest) -> CacheImportResponse:
        return await self._post("/v2/import-tokens", request, CacheImportResponse)

    async def evict_cache(self, request: CacheStatusRequest) -> CacheInventory:
        return await self._post("/v1/evict-cache", request, CacheInventory)

    async def cache_status(self, request: CacheStatusRequest) -> CacheInventory:
        return await self._post("/v1/cache-status", request, CacheInventory)

    async def export_cache(self, request: CacheExportRequest) -> CachePayload:
        return await self._post("/v1/export-cache", request, CachePayload)

    async def import_cache(self, request: CacheImportRequest) -> CacheImportResponse:
        return await self._post("/v1/import-cache", request, CacheImportResponse)

    async def _get(self, path: str, response_model: type[ResponseModel]) -> ResponseModel:
        try:
            response = await self._client.get(path)
            response.raise_for_status()
            return response_model.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelRuntimeClientError(f"model service GET {path} failed: {exc}") from exc

    async def _post(
        self,
        path: str,
        request: BaseModel,
        response_model: type[ResponseModel],
    ) -> ResponseModel:
        try:
            response = await self._client.post(
                path,
                json=request.model_dump(mode="json"),
            )
            response.raise_for_status()
            return response_model.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelRuntimeClientError(f"model service POST {path} failed: {exc}") from exc


__all__ = ["HttpModelRuntimeClient", "ModelRuntimeClientError"]
