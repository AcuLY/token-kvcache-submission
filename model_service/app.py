"""FastAPI application exposing the deterministic mock model runtime."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException

from shared.contracts import (
    CreateSessionRequest,
    CreateSessionResponse,
    GenerateRequest,
    GenerateResponse,
    HealthResponse,
    ModelBehaviorConfig,
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

from .runtime import (
    MockModelRuntime,
    ModelRuntimeError,
    SessionNotFoundError,
    StateConflictError,
)


def create_app(
    runtime: Any = None,
    *,
    service_name: str = "mock-model-service",
    title: str = "Token KVCache Mock Model Service",
    description: str = "Deterministic Token and Mock KV model runtime.",
) -> FastAPI:
    model_runtime = runtime or MockModelRuntime()
    application = FastAPI(
        title=title,
        version="0.1.0",
        description=description,
    )
    application.state.runtime = model_runtime

    def translate_error(exc: ModelRuntimeError) -> HTTPException:
        if isinstance(exc, SessionNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, StateConflictError):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    @application.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(service=service_name)

    @application.get("/descriptor", response_model=RuntimeDescriptor)
    @application.get("/v1/descriptor", response_model=RuntimeDescriptor, include_in_schema=False)
    async def descriptor() -> RuntimeDescriptor:
        return await model_runtime.describe()

    @application.post("/create-session", response_model=CreateSessionResponse)
    @application.post(
        "/v1/create-session", response_model=CreateSessionResponse, include_in_schema=False
    )
    async def create_session(request: CreateSessionRequest) -> CreateSessionResponse:
        return await model_runtime.create_session(request)

    @application.post("/tokenize", response_model=TokenizeResponse)
    @application.post("/v1/tokenize", response_model=TokenizeResponse, include_in_schema=False)
    async def tokenize(request: TokenizeRequest) -> TokenizeResponse:
        return await model_runtime.tokenize(request)

    @application.post("/render-local-input", response_model=RenderLocalInputResponse)
    @application.post(
        "/v1/render-local-input",
        response_model=RenderLocalInputResponse,
        include_in_schema=False,
    )
    async def render_local_input(
        request: RenderLocalInputRequest,
    ) -> RenderLocalInputResponse:
        try:
            return await model_runtime.render_local_input(request)
        except ModelRuntimeError as exc:
            raise translate_error(exc) from exc

    @application.post("/prefill", response_model=PrefillResponse)
    @application.post("/v1/prefill", response_model=PrefillResponse, include_in_schema=False)
    async def prefill(request: PrefillRequest) -> PrefillResponse:
        try:
            return await model_runtime.prefill(request)
        except ModelRuntimeError as exc:
            raise translate_error(exc) from exc

    @application.post("/generate", response_model=GenerateResponse)
    @application.post("/v1/generate", response_model=GenerateResponse, include_in_schema=False)
    async def generate(request: GenerateRequest) -> GenerateResponse:
        try:
            return await model_runtime.generate(request)
        except ModelRuntimeError as exc:
            raise translate_error(exc) from exc

    @application.post("/seal", response_model=SealResponse)
    @application.post("/v1/seal", response_model=SealResponse, include_in_schema=False)
    async def seal(request: SealRequest) -> SealResponse:
        try:
            return await model_runtime.seal(request)
        except ModelRuntimeError as exc:
            raise translate_error(exc) from exc

    @application.get("/behavior", response_model=ModelBehaviorConfig)
    async def get_behavior() -> ModelBehaviorConfig:
        return await model_runtime.get_behavior()

    @application.put("/behavior", response_model=ModelBehaviorConfig)
    async def put_behavior(behavior: ModelBehaviorConfig) -> ModelBehaviorConfig:
        return await model_runtime.set_behavior(behavior)

    @application.post("/v2/import-tokens", response_model=CacheImportResponse)
    async def import_tokens(request: TokenImportRequest) -> CacheImportResponse:
        try:
            return await model_runtime.import_tokens(request)
        except ModelRuntimeError as exc:
            raise translate_error(exc) from exc

    @application.post("/v1/evict-cache", response_model=CacheInventory)
    async def evict_cache(request: CacheStatusRequest) -> CacheInventory:
        try:
            return await model_runtime.evict_cache(request)
        except ModelRuntimeError as exc:
            raise translate_error(exc) from exc

    @application.post("/v1/cache-status", response_model=CacheInventory)
    async def cache_status(request: CacheStatusRequest) -> CacheInventory:
        try:
            return await model_runtime.cache_status(request)
        except ModelRuntimeError as exc:
            raise translate_error(exc) from exc

    @application.post("/v1/export-cache", response_model=CachePayload)
    async def export_cache(request: CacheExportRequest) -> CachePayload:
        try:
            return await model_runtime.export_cache(request)
        except ModelRuntimeError as exc:
            raise translate_error(exc) from exc

    @application.post("/v1/import-cache", response_model=CacheImportResponse)
    async def import_cache(request: CacheImportRequest) -> CacheImportResponse:
        try:
            return await model_runtime.import_cache(request)
        except ModelRuntimeError as exc:
            raise translate_error(exc) from exc

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "model_service.app:app",
        host=os.getenv("MOCK_MODEL_HOST", "0.0.0.0"),
        port=int(os.getenv("MOCK_MODEL_PORT", "8100")),
        reload=False,
    )
