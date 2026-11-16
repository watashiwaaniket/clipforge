import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_local_frontend_origin_is_allowed() -> None:
    response = client.options(
        "/api/v1/health",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_parses_explicit_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", " http://localhost:3000 , https://clipforge.app ")
    settings = Settings()
    assert settings.cors_origin_list == [
        "http://localhost:3000",
        "https://clipforge.app",
    ]


def test_cors_rejects_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "*")
    settings = Settings()
    with pytest.raises(RuntimeError):
        _ = settings.cors_origin_list
