import base64
import hashlib
import hmac
import json
import logging
import socket
import time
from collections.abc import Generator
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.logging import configure_logging
from app.main import app
from app.models import ApiKey, User
from app.security import decrypt_api_key, hash_password


def _sk_canary() -> str:
    return "sk-canary-" + "DO-NOT-LEAK-" + "0123456789abcdef"


def _gsk_canary() -> str:
    return "gsk_" + "canary-" + "DO-NOT-LEAK-" + "abcdef0123456789"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_token(user_id: UUID) -> str:
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "HS256"}, separators=(",", ":")).encode("utf-8"))
    payload = _b64url(
        json.dumps(
            {
                "sub": str(user_id),
                "iss": "clipforge",
                "aud": "clipforge-api",
                "iat": now,
                "exp": now + 3600,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    sig = hmac.new(
        settings.jwt_secret.encode("utf-8"),
        f"{header}.{payload}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def _mock_public_dns(monkeypatch: pytest.MonkeyPatch, ip: str = "93.184.216.34") -> None:
    def fake_getaddrinfo(
        host: str, port: int, family: int = 0, type: int = 0
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        af = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(af, socket.SOCK_STREAM, 6, "", (ip, int(port)))]

    monkeypatch.setattr("app.services.providers.socket.getaddrinfo", fake_getaddrinfo)


def _mock_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> None:
    real_client = httpx.Client

    class FakeClient(real_client):
        def __init__(self, *args, **kwargs):
            assert kwargs.get("follow_redirects") is False
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("app.api.keys.httpx.Client", FakeClient)


@pytest.fixture
def auth_headers(clean_db: None) -> tuple[dict[str, str], UUID]:
    with SessionLocal() as session:
        user = User(email="keys@example.com", password_hash=hash_password("correct-horse-battery"))
        session.add(user)
        session.commit()
        user_id = user.id
    return {"Authorization": f"Bearer {_sign_token(user_id)}"}, user_id


@pytest.fixture
def client(clean_db: None) -> Generator[TestClient, None, None]:
    with TestClient(app) as tc:
        yield tc


def test_keys_endpoints_require_auth(client: TestClient) -> None:
    assert client.get("/api/v1/keys").status_code == 401
    res = client.post("/api/v1/keys", json={"provider": "openai", "api_key": _sk_canary()})
    assert res.status_code == 401
    assert client.delete("/api/v1/keys").status_code == 401


def test_keys_upsert_replace_get_and_delete(
    client: TestClient,
    auth_headers: tuple[dict[str, str], UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, user_id = auth_headers
    _mock_public_dns(monkeypatch)

    seen_requests: list[tuple[str, dict[str, object], str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_requests.append((str(request.url), body, request.headers.get("authorization")))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1700000000,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    _mock_httpx_client(monkeypatch, handler)

    # Empty state
    initial = client.get("/api/v1/keys", headers=headers)
    assert initial.status_code == 200
    assert initial.json() == {"configured": False}

    # First save with sk canary
    sk = _sk_canary()
    save_one = client.post(
        "/api/v1/keys",
        headers=headers,
        json={"provider": "openai", "api_key": sk},
    )
    assert save_one.status_code == 200
    assert save_one.json() == {
        "configured": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
    }
    assert sk not in save_one.text
    assert "encrypted_key" not in save_one.json()

    assert len(seen_requests) == 1
    req_url, req_body, req_auth = seen_requests[0]
    assert req_url == "https://api.openai.com/v1/chat/completions"
    assert req_body["model"] == "gpt-4o-mini"
    assert req_body["max_tokens"] == 1
    assert req_body["messages"] == [{"role": "user", "content": "ping"}]
    assert req_auth == f"Bearer {sk}"

    with SessionLocal() as session:
        row = session.scalar(select(ApiKey).where(ApiKey.user_id == user_id))
        assert row is not None
        assert bytes(row.encrypted_key) != sk.encode("utf-8")
        assert b"sk-" not in bytes(row.encrypted_key)
        assert decrypt_api_key(row.encrypted_key) == sk

    # Second POST replaces existing row rather than inserting a second row
    gsk = _gsk_canary()
    save_two = client.post(
        "/api/v1/keys",
        headers=headers,
        json={"provider": "groq", "api_key": gsk},
    )
    assert save_two.status_code == 200
    assert save_two.json() == {
        "configured": True,
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
    }
    assert gsk not in save_two.text

    with SessionLocal() as session:
        count = session.scalar(
            select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user_id)
        )
        assert count == 1
        row = session.scalar(select(ApiKey).where(ApiKey.user_id == user_id))
        assert row is not None
        assert b"gsk_" not in bytes(row.encrypted_key)
        assert decrypt_api_key(row.encrypted_key) == gsk

    meta = client.get("/api/v1/keys", headers=headers)
    assert meta.status_code == 200
    assert meta.json() == {
        "configured": True,
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
    }
    assert sk not in meta.text
    assert gsk not in meta.text

    # Delete and idempotent second delete
    del_one = client.delete("/api/v1/keys", headers=headers)
    assert del_one.status_code == 204
    after_del = client.get("/api/v1/keys", headers=headers)
    assert after_del.status_code == 200
    assert after_del.json() == {"configured": False}
    assert client.delete("/api/v1/keys", headers=headers).status_code == 204


def test_preset_base_url_locked_and_compatible_requires_fields(
    client: TestClient,
    auth_headers: tuple[dict[str, str], UUID],
) -> None:
    headers, _ = auth_headers
    for provider in ("openai", "groq", "together"):
        res = client.post(
            "/api/v1/keys",
            headers=headers,
            json={
                "provider": provider,
                "api_key": _sk_canary(),
                "base_url": "https://evil.example.com/v1",
            },
        )
        assert res.status_code == 400
        assert res.json()["detail"] == "That provider uses a fixed base URL."

    missing_both = client.post(
        "/api/v1/keys",
        headers=headers,
        json={"provider": "compatible", "api_key": _sk_canary()},
    )
    assert missing_both.status_code == 400
    assert missing_both.json()["detail"] == 'Provider "compatible" needs a base URL and a model.'

    missing_model = client.post(
        "/api/v1/keys",
        headers=headers,
        json={
            "provider": "compatible",
            "api_key": _sk_canary(),
            "base_url": "https://api.example.com/v1",
        },
    )
    assert missing_model.status_code == 400
    assert missing_model.json()["detail"] == 'Provider "compatible" needs a base URL and a model.'


def test_ssrf_blocks_metadata_private_ips_userinfo_and_redirects(
    client: TestClient,
    auth_headers: tuple[dict[str, str], UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, _ = auth_headers
    outbound_calls = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal outbound_calls
        outbound_calls += 1
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    _mock_httpx_client(monkeypatch, counting_handler)

    blocked_urls = [
        "https://169.254.169.254/",
        "https://10.0.0.5/",
        "https://127.0.0.1/v1",
        "https://[::1]/v1",
        "http://api.example.com/v1",
        "https://user:pass@api.example.com/v1",
        "https://localhost/v1",
        "https://host.docker.internal/v1",
        "https://metadata.google.internal/v1",
        "https://metadata.google.com/v1",
    ]
    for bad_url in blocked_urls:
        res = client.post(
            "/api/v1/keys",
            headers=headers,
            json={
                "provider": "compatible",
                "api_key": _sk_canary(),
                "base_url": bad_url,
                "model": "custom-model",
            },
        )
        assert res.status_code == 400, bad_url
    assert outbound_calls == 0

    # DNS resolving to private/loopback/link-local/0.0.0.0/8/multicast is rejected before HTTP.
    for bad_ip in (
        "127.0.0.1",
        "169.254.169.254",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.10",
        "0.1.2.3",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:169.254.169.254",
    ):
        _mock_public_dns(monkeypatch, bad_ip)
        res = client.post(
            "/api/v1/keys",
            headers=headers,
            json={
                "provider": "compatible",
                "api_key": _sk_canary(),
                "base_url": "https://rebind.example.com/v1",
                "model": "custom-model",
            },
        )
        assert res.status_code == 400, bad_ip
    assert outbound_calls == 0

    # 302 redirect to metadata records exactly one outbound request and returns 400.
    _mock_public_dns(monkeypatch, "93.184.216.34")
    redirect_res = client.post(
        "/api/v1/keys",
        headers=headers,
        json={
            "provider": "compatible",
            "api_key": _sk_canary(),
            "base_url": "https://redirect.example.com/v1",
            "model": "custom-model",
        },
    )
    assert redirect_res.status_code == 400
    assert redirect_res.json()["detail"] == "That key was rejected by the provider."
    assert outbound_calls == 1


def test_ollama_allowed_and_blocked_urls(
    client: TestClient,
    auth_headers: tuple[dict[str, str], UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, _ = auth_headers

    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "llama3.1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    _mock_httpx_client(monkeypatch, ok_handler)

    for allowed_url in (
        "http://host.docker.internal:11434/v1",
        "http://localhost:11434/v1",
        "http://127.0.0.1:11434",
    ):
        res = client.post(
            "/api/v1/keys",
            headers=headers,
            json={"provider": "ollama", "api_key": "ollama", "base_url": allowed_url},
        )
        assert res.status_code == 200, allowed_url

    for blocked_url in (
        "https://localhost:11434/v1",
        "http://169.254.169.254/v1",
        "http://example.com:11434/v1",
        "http://localhost:11434/other",
    ):
        res = client.post(
            "/api/v1/keys",
            headers=headers,
            json={"provider": "ollama", "api_key": "ollama", "base_url": blocked_url},
        )
        assert res.status_code == 400, blocked_url


def test_canary_secrets_never_appear_in_logs_or_responses_on_exception(
    client: TestClient,
    auth_headers: tuple[dict[str, str], UUID],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging()
    headers, _ = auth_headers
    _mock_public_dns(monkeypatch)

    sk = _sk_canary()
    gsk = _gsk_canary()

    for provider, canary in (("openai", sk), ("groq", gsk)):
        def failing_handler(request: httpx.Request, *, secret: str = canary) -> httpx.Response:
            raise httpx.ConnectError(f"upstream rejected credential {secret}", request=request)

        _mock_httpx_client(monkeypatch, failing_handler)
        with caplog.at_level(logging.INFO):
            res = client.post(
                "/api/v1/keys",
                headers=headers,
                json={"provider": provider, "api_key": canary},
            )
        assert res.status_code == 400
        assert res.json() == {"detail": "That key was rejected by the provider."}
        assert canary not in res.text

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert sk not in logs
    assert gsk not in logs
    assert "[REDACTED]" in logs
