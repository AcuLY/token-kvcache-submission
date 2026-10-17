"""Model-service backend selection keeps Mock as the default."""

import pytest
from fastapi.testclient import TestClient

from model_service.factory import MOCK_BACKENDS, build_app, selected_backend


def test_default_backend_is_the_deterministic_mock(monkeypatch) -> None:
    monkeypatch.delenv("MODEL_BACKEND", raising=False)
    assert selected_backend() == "mock"
    app = build_app()
    assert type(app.state.runtime).__name__ == "MockModelRuntime"
    assert TestClient(app).get("/health").json() == {
        "status": "ok",
        "service": "mock-model-service",
    }


@pytest.mark.parametrize("name", sorted(MOCK_BACKENDS))
def test_mock_backend_aliases(monkeypatch, name: str) -> None:
    monkeypatch.setenv("MODEL_BACKEND", name)
    assert selected_backend() == name
    assert type(build_app().state.runtime).__name__ == "MockModelRuntime"


def test_unknown_backend_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_BACKEND", "not-a-backend")
    with pytest.raises(RuntimeError):
        build_app()


def test_huggingface_backend_requires_a_model_id(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_BACKEND", "huggingface")
    monkeypatch.delenv("HF_MODEL_ID", raising=False)
    with pytest.raises(RuntimeError):
        build_app()
