import base64
import hashlib
import hmac
import json
import logging
import socket
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.logging import configure_logging
from app.main import app
from app.models import ApiKey, User
from app.security import decrypt_api_key, hash_password
from app.services.providers import (
    ANTIGRAVITY_BRIDGE_HTTP_POLICY,
    ANTIGRAVITY_DEFAULT_BASE_URL,
    ANTIGRAVITY_DEFAULT_MODEL,
    FALLBACK_ANTIGRAVITY_MODELS,
    PROVIDER_PRESETS,
    AntigravityApiError,
    AntigravityCliError,
    ProviderConfigError,
    ResolvedProviderConfig,
    build_antigravity_provider,
    list_antigravity_models,
    parse_agy_models_output,
    pick_antigravity_default_model,
    resolve_provider_config,
    validate_outbound_url,
)
from app.services.scorer import HEURISTIC_HOOK, score_transcript
from app.services.transcribe import Word


def _ya29_canary() -> str:
    return "ya29.a0AfB_canary_DO_NOT_LEAK_0123456789abcdef"


def _refresh_canary() -> str:
    return "1//0g_canary_REFRESH_DO_NOT_LEAK_abcdef0123456789"


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


def _mock_public_dns(monkeypatch: pytest.MonkeyPatch, ip: str = "142.250.190.10") -> None:
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(
        host: str,
        port: int | str | None,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ):
        if host in {"db", "redis"}:
            return real_getaddrinfo(host, port, family, type, proto, flags)
        af = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(af, socket.SOCK_STREAM, 6, "", (ip, int(port or 443)))]

    monkeypatch.setattr("app.services.providers.url_policy.socket.getaddrinfo", fake_getaddrinfo)


_REAL_HTTPX_CLIENT = httpx.Client


def _mock_httpx_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    class FakeClient(_REAL_HTTPX_CLIENT):
        def __init__(self, *args, **kwargs):
            assert kwargs.get("follow_redirects") is False
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("app.api.keys.httpx.Client", FakeClient)
    monkeypatch.setattr("app.api.providers.httpx.Client", FakeClient)
    monkeypatch.setattr("app.services.providers.catalog.httpx.Client", FakeClient)
    monkeypatch.setattr("app.services.providers.antigravity.adapter.httpx.Client", FakeClient)


def _make_words(tokens: list[str], *, start: float = 0.0, step: float = 0.5) -> list[Word]:
    words: list[Word] = []
    t = start
    for tok in tokens:
        words.append(Word(word=tok, start=t, end=t + 0.4))
        t += step
    return words


@pytest.fixture
def auth_headers(clean_db: None) -> tuple[dict[str, str], UUID]:
    with SessionLocal() as session:
        user = User(email="agy@example.com", password_hash=hash_password("correct-horse-battery"))
        session.add(user)
        session.commit()
        user_id = user.id
    return {"Authorization": f"Bearer {_sign_token(user_id)}"}, user_id


@pytest.fixture
def client(clean_db: None) -> Generator[TestClient, None, None]:
    with TestClient(app) as tc:
        yield tc


def test_antigravity_is_not_in_openai_presets_and_resolves_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "antigravity" not in PROVIDER_PRESETS
    _mock_public_dns(monkeypatch)

    resolved = resolve_provider_config("antigravity", None, None)
    assert resolved == ResolvedProviderConfig(
        provider="antigravity",
        base_url=ANTIGRAVITY_DEFAULT_BASE_URL,
        model=ANTIGRAVITY_DEFAULT_MODEL,
    )
    assert resolved.model == "gemini-4-hydrogen"

    custom_model = resolve_provider_config("antigravity", "   ", "  gemini-3.8-flash-low  ")
    assert custom_model == ResolvedProviderConfig(
        provider="antigravity",
        base_url=ANTIGRAVITY_DEFAULT_BASE_URL,
        model="gemini-3.8-flash-low",
    )

    with pytest.raises(ProviderConfigError, match="fixed base URL"):
        resolve_provider_config("antigravity", "https://evil.example.com", None)


def test_antigravity_cli_mode_validation_completion_and_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _mock_public_dns(monkeypatch)
    fake_agy = tmp_path / "agy"
    fake_agy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_agy.chmod(0o755)
    monkeypatch.setattr(settings, "antigravity_cli_bin", str(fake_agy))
    monkeypatch.setattr(settings, "antigravity_oauth_creds_path", "")

    seen_cmds: list[list[str]] = []

    def fake_runner(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        seen_cmds.append(cmd)
        if "--json-schema" in cmd:
            payload = {
                "status": "SUCCESS",
                "response": "ignored",
                "structured_output": {
                    "highlights": [
                        {
                            "chunk_word_start": 0,
                            "chunk_word_end": 5,
                            "score": 91,
                            "title": "CLI highlight",
                            "hook": "Returned structured_output.",
                        }
                    ]
                },
            }
        else:
            payload = {"status": "SUCCESS", "response": "pong"}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    provider = build_antigravity_provider(
        credential="local",
        model="gemini-4-hydrogen",
        cli_runner=fake_runner,
    )
    assert provider.mode == "cli"
    provider.validate_credentials()
    assert seen_cmds[0][:6] == [
        str(fake_agy),
        "--disable-slash-commands",
        "--output-format",
        "json",
        "--model",
        "gemini-4-hydrogen",
    ]
    assert "--print" in seen_cmds[0]

    reply = provider.complete(
        messages=[
            {"role": "system", "content": "Be strict."},
            {"role": "user", "content": "Score this."},
        ],
        json_schema={"type": "object"},
    )
    assert reply is not None
    assert json.loads(reply)["highlights"][0]["title"] == "CLI highlight"

    def unauth_runner(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=json.dumps(
                {"status": "ERROR", "error": "You are not logged into Antigravity."}
            ),
            stderr="",
        )

    unauth_provider = build_antigravity_provider(credential=None, cli_runner=unauth_runner)
    with pytest.raises(ProviderConfigError, match="not signed in"):
        unauth_provider.validate_credentials()

    def timeout_runner(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5.0)

    timeout_provider = build_antigravity_provider(credential="", cli_runner=timeout_runner)
    with pytest.raises(AntigravityCliError) as exc_info:
        timeout_provider.complete(messages=[{"role": "user", "content": "hi"}])
    assert exc_info.value.retryable is True


def test_antigravity_oauth_refresh_and_load_code_assist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _mock_public_dns(monkeypatch)
    monkeypatch.setattr(settings, "antigravity_cli_bin", "")
    monkeypatch.setattr(settings, "antigravity_bridge_url", "")
    monkeypatch.setattr(
        "app.services.providers.antigravity.adapter._resolve_cli_binary",
        lambda: None,
    )

    creds_path = tmp_path / "oauth_creds.json"
    fresh_token = _ya29_canary()
    refresh_token = _refresh_canary()
    creds_path.write_text(
        json.dumps(
            {
                "access_token": "ya29.expired_old_token_value",
                "refresh_token": refresh_token,
                "client_id": "test-client-id.apps.googleusercontent.com",
                "client_secret": "test-client-secret",
                "expiry_date": (time.time() - 300) * 1000,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "antigravity_oauth_creds_path", str(creds_path))

    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if str(request.url) == "https://oauth2.googleapis.com/token":
            return httpx.Response(200, json={"access_token": fresh_token, "expires_in": 3600})
        if str(request.url).endswith("/v1internal:loadCodeAssist"):
            assert request.headers.get("authorization") == f"Bearer {fresh_token}"
            return httpx.Response(200, json={"cloudaicompanionProject": "proj-agy-123"})
        if str(request.url).endswith("/v1internal:generateContent"):
            body = json.loads(request.content.decode("utf-8"))
            assert body["project"] == "proj-agy-123"
            assert body["model"] == "gemini-4-hydrogen"
            assert body["request"]["generationConfig"]["responseMimeType"] == "application/json"
            return httpx.Response(
                200,
                json={
                    "response": {
                        "candidates": [
                            {"content": {"parts": [{"text": '{"highlights":[]}'}]}}
                        ]
                    }
                },
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    _mock_httpx_client(monkeypatch, handler)

    with build_antigravity_provider(credential="local") as provider:
        assert provider.mode == "oauth"
        reply = provider.complete(
            messages=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "usr"},
            ]
        )
        assert reply == '{"highlights":[]}'

    assert seen_urls == [
        "https://oauth2.googleapis.com/token",
        "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
        "https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent",
    ]


def test_antigravity_expired_oauth_without_client_id_and_missing_local_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _mock_public_dns(monkeypatch)
    monkeypatch.setattr(settings, "antigravity_bridge_url", "")
    monkeypatch.setattr(
        "app.services.providers.antigravity.adapter._resolve_cli_binary",
        lambda: None,
    )

    expired_file = tmp_path / "expired_oauth.json"
    expired_file.write_text(
        json.dumps(
            {
                "access_token": _ya29_canary(),
                "refresh_token": _refresh_canary(),
                "expiry_date": (time.time() - 600) * 1000,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "antigravity_oauth_creds_path", str(expired_file))

    with build_antigravity_provider(credential="local") as provider:
        with pytest.raises(ProviderConfigError, match="expired and cannot be refreshed"):
            provider.validate_credentials()

    monkeypatch.setattr(settings, "antigravity_oauth_creds_path", "")
    monkeypatch.setattr(
        "app.services.providers.antigravity.adapter._resolve_local_oauth_path", lambda: None
    )
    with pytest.raises(ProviderConfigError, match="Antigravity CLI"):
        build_antigravity_provider(credential=None)


def test_keys_api_persists_antigravity_without_api_key_and_redacts_oauth_tokens(
    client: TestClient,
    auth_headers: tuple[dict[str, str], UUID],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging()
    headers, user_id = auth_headers
    _mock_public_dns(monkeypatch)
    monkeypatch.setattr(settings, "antigravity_bridge_url", "")

    fake_agy = tmp_path / "agy"
    fake_agy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_agy.chmod(0o755)
    monkeypatch.setattr(settings, "antigravity_cli_bin", str(fake_agy))
    monkeypatch.setattr(
        "app.services.providers.antigravity.adapter.subprocess.run",
        lambda cmd, **_kw: subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"status": "SUCCESS", "response": "pong"}), stderr=""
        ),
    )

    # 1. Save antigravity with NO api_key field (uses local CLI)
    res = client.post("/api/v1/keys", headers=headers, json={"provider": "antigravity"})
    assert res.status_code == 200
    assert res.json() == {
        "configured": True,
        "provider": "antigravity",
        "model": "gemini-4-hydrogen",
    }

    meta = client.get("/api/v1/keys", headers=headers)
    assert meta.status_code == 200
    assert meta.json() == {
        "configured": True,
        "provider": "antigravity",
        "model": "gemini-4-hydrogen",
        "base_url": "https://daily-cloudcode-pa.googleapis.com",
    }

    with SessionLocal() as session:
        row = session.scalar(select(ApiKey).where(ApiKey.user_id == user_id))
        assert row is not None
        assert row.provider == "antigravity"
        assert decrypt_api_key(row.encrypted_key) == "local"

    # 2. OAuth token failure never leaks ya29 canary into response or logs
    canary = _ya29_canary()

    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"rejected oauth token {canary}", request=request)

    _mock_httpx_client(monkeypatch, failing_handler)
    with caplog.at_level(logging.INFO):
        fail_res = client.post(
            "/api/v1/keys",
            headers=headers,
            json={"provider": "antigravity", "api_key": canary},
        )
    assert fail_res.status_code == 400
    assert fail_res.json() == {"detail": "That key was rejected by the provider."}
    assert canary not in fail_res.text

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert canary not in logs
    assert "[REDACTED]" in logs


def test_score_transcript_with_antigravity_retries_and_heuristic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_public_dns(monkeypatch)
    monkeypatch.setattr("app.services.scorer.MIN_WORDS", 1)
    monkeypatch.setattr(
        "app.services.providers.antigravity.adapter._resolve_default_project_id", lambda: None
    )

    words = _make_words(["bro", "that", "POLICE", "crash!", "was", "so", "crazy", "here"])
    token = _ya29_canary()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if str(request.url).endswith("/v1internal:loadCodeAssist"):
            return httpx.Response(200, json={"cloudaicompanionProject": "proj-1"})
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        if calls == 2:
            return httpx.Response(
                200,
                json={
                    "response": {
                        "candidates": [{"content": {"parts": [{"text": "not valid json"}]}}]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "response": {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "highlights": [
                                                    {
                                                        "chunk_word_start": 0,
                                                        "chunk_word_end": 5,
                                                        "score": 89,
                                                        "title": "Antigravity recovered",
                                                        "hook": "Worked on corrective retry.",
                                                    }
                                                ]
                                            }
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                }
            },
        )

    _mock_httpx_client(monkeypatch, handler)

    windows = score_transcript(
        words,
        duration_s=20.0,
        api_key=token,
        base_url="https://daily-cloudcode-pa.googleapis.com",
        model="gemini-4-hydrogen",
        provider="antigravity",
    )
    assert calls == 3
    assert len(windows) == 1
    assert windows[0].score_source == "llm"
    assert windows[0].title == "Antigravity recovered"

    # Two bad JSON replies fall back to heuristic scoring
    def always_bad_json(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/v1internal:loadCodeAssist"):
            return httpx.Response(200, json={"cloudaicompanionProject": "proj-1"})
        return httpx.Response(
            200,
            json={"response": {"candidates": [{"content": {"parts": [{"text": "still bad"}]}}]}},
        )

    _mock_httpx_client(monkeypatch, always_bad_json)
    fallback_windows = score_transcript(
        words,
        duration_s=20.0,
        api_key=token,
        base_url="https://daily-cloudcode-pa.googleapis.com",
        model="gemini-4-hydrogen",
        provider="antigravity",
    )
    assert len(fallback_windows) == 1
    assert fallback_windows[0].score_source == "heuristic"
    assert fallback_windows[0].hook == HEURISTIC_HOOK


def test_parse_agy_models_output_handles_headers_duplicates_and_json_noise() -> None:
    raw = (
        "Fetching available models...\n"
        "Available models:\n"
        '{"status": "SUCCESS", "response": "pong"}\n'
        "Error: temporary warning\n"
        "gemini-3.8-flash-high     Gemini 3.8 Flash (High)\n"
        "gemini-3.8-flash-medium   Gemini 3.8 Flash (Medium)\n"
        "claude-sonnet-4-6         Claude Sonnet 4.6 (Thinking)\n"
        "gemini-3.8-flash-high     Duplicate Ignored\n"
    )
    parsed = parse_agy_models_output(raw)
    assert [(m.id, m.name) for m in parsed] == [
        ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
        ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
        ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)"),
    ]


def test_antigravity_bridge_mode_completion_auth_and_ssrf_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_public_dns(monkeypatch)
    monkeypatch.setattr(
        "app.services.providers.antigravity.adapter._resolve_cli_binary",
        lambda: None,
    )
    monkeypatch.setattr(settings, "antigravity_bridge_url", "http://host.docker.internal:11436")
    monkeypatch.setattr(settings, "antigravity_bridge_token", "bridge-secret-123")

    with pytest.raises(ProviderConfigError):
        validate_outbound_url(
            "http://evil.example.com:11436",
            policy=ANTIGRAVITY_BRIDGE_HTTP_POLICY,
        )

    seen_auth: list[str | None] = []
    mode_case = {"status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True, "cli_found": True})
        assert request.url.path == "/v1/complete"
        seen_auth.append(request.headers.get("authorization"))
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "claude-sonnet-4-6"
        if mode_case["status"] == 400:
            return httpx.Response(
                400,
                json={"detail": "error: invalid model selection"},
            )
        if mode_case["status"] == 502:
            return httpx.Response(502, json={"detail": "upstream failure"})
        if mode_case["status"] == 401:
            return httpx.Response(401, json={"detail": "Unauthorized."})
        if "json_schema" in body:
            return httpx.Response(
                200,
                json={
                    "status": "SUCCESS",
                    "response": "ignored",
                    "structured_output": {"highlights": [{"score": 95}]},
                },
            )
        return httpx.Response(200, json={"status": "SUCCESS", "response": "pong"})

    _mock_httpx_client(monkeypatch, handler)

    with build_antigravity_provider(credential="local", model="claude-sonnet-4-6") as provider:
        assert provider.mode == "bridge"
        provider.validate_credentials()
        structured = provider.complete(
            messages=[{"role": "user", "content": "score"}],
            json_schema={"type": "object"},
        )
        assert json.loads(structured or "{}") == {"highlights": [{"score": 95}]}
        assert seen_auth == ["Bearer bridge-secret-123", "Bearer bridge-secret-123"]

        mode_case["status"] = 400
        with pytest.raises(ProviderConfigError, match="rejected model"):
            provider.complete(messages=[{"role": "user", "content": "hi"}])

        mode_case["status"] = 401
        with pytest.raises(ProviderConfigError, match="rejected authentication"):
            provider.complete(messages=[{"role": "user", "content": "hi"}])

        mode_case["status"] = 502
        with pytest.raises(AntigravityApiError) as exc_info:
            provider.complete(messages=[{"role": "user", "content": "hi"}])
        assert exc_info.value.retryable is True


def test_list_antigravity_models_sources_and_default_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_public_dns(monkeypatch)
    monkeypatch.setattr(
        "app.services.providers.antigravity.adapter._resolve_cli_binary",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.services.providers.antigravity.adapter._resolve_local_oauth_path",
        lambda: None,
    )

    # 1. Fallback when neither CLI, bridge, nor OAuth is available
    monkeypatch.setattr(settings, "antigravity_bridge_url", "")
    models, source, connected = list_antigravity_models(credential="local")
    assert connected is False
    assert source == "fallback"
    assert models == list(FALLBACK_ANTIGRAVITY_MODELS)
    assert pick_antigravity_default_model(credential="local") == ANTIGRAVITY_DEFAULT_MODEL

    # 2. Bridge live discovery
    monkeypatch.setattr(settings, "antigravity_bridge_url", "http://host.docker.internal:11436")
    monkeypatch.setattr(settings, "antigravity_bridge_token", "")

    def bridge_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
                        {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)"},
                    ]
                },
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    _mock_httpx_client(monkeypatch, bridge_handler)
    models, source, connected = list_antigravity_models(credential="local")
    assert connected is True
    assert source == "bridge_live"
    assert [m.id for m in models] == ["claude-sonnet-4-6", "gemini-3.8-flash-medium"]
    assert pick_antigravity_default_model(credential="local") == "gemini-3.8-flash-medium"

    # 3. CLI live discovery takes precedence over bridge
    monkeypatch.setattr(
        "app.services.providers.antigravity.adapter._resolve_cli_binary",
        lambda: "/usr/local/bin/agy",
    )
    models, source, connected = list_antigravity_models(
        credential="local",
        cli_runner=lambda cmd, **_kw: subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "gemini-4-hydrogen        Gemini 4 Hydrogen\n"
                "gpt-oss-120b-medium      GPT-OSS 120B (Medium)\n"
            ),
            stderr="",
        ),
    )
    assert connected is True
    assert source == "cli_live"
    assert [m.id for m in models] == ["gemini-4-hydrogen", "gpt-oss-120b-medium"]

    # 4. OAuth live discovery via v1internal:fetchAvailableModels
    token = _ya29_canary()

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/v1internal:loadCodeAssist"):
            return httpx.Response(200, json={"cloudaicompanionProject": "proj-oauth"})
        if str(request.url).endswith("/v1internal:fetchAvailableModels"):
            assert request.headers.get("authorization") == f"Bearer {token}"
            return httpx.Response(
                200,
                json={
                    "models": {
                        "gemini-4-argon": {"displayName": "Gemini 4 Argon"},
                        "claude-opus-4-6-thinking": {"displayName": "Claude Opus 4.6"},
                    }
                },
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    _mock_httpx_client(monkeypatch, oauth_handler)
    models, source, connected = list_antigravity_models(credential=token)
    assert connected is True
    assert source == "oauth_live"
    assert [m.id for m in models] == ["gemini-4-argon", "claude-opus-4-6-thinking"]
    assert pick_antigravity_default_model(credential=token) == "gemini-4-argon"


def test_providers_catalog_endpoints_and_auto_default_key_save(
    client: TestClient,
    auth_headers: tuple[dict[str, str], UUID],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging()
    headers, _user_id = auth_headers
    _mock_public_dns(monkeypatch)
    monkeypatch.setattr(
        "app.services.providers.antigravity.adapter._resolve_cli_binary",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.services.providers.antigravity.adapter._resolve_local_oauth_path",
        lambda: None,
    )
    monkeypatch.setattr(settings, "antigravity_bridge_url", "http://host.docker.internal:11436")
    monkeypatch.setattr(settings, "antigravity_bridge_token", "")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "http://host.docker.internal:11436/health":
            return httpx.Response(200, json={"ok": True})
        if url == "http://host.docker.internal:11436/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Thinking)"},
                        {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)"},
                        {"id": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)"},
                    ]
                },
            )
        if url == "http://host.docker.internal:11436/v1/complete":
            body = json.loads(request.content.decode("utf-8"))
            assert body["model"] == "gemini-3.8-flash-medium"
            return httpx.Response(200, json={"status": "SUCCESS", "response": "pong"})
        if url == "http://host.docker.internal:11434/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "qwen2.5:14b"}, {"name": "llama3.2:latest"}]},
            )
        if url.endswith("/v1internal:loadCodeAssist"):
            return httpx.Response(200, json={"cloudaicompanionProject": "proj-probe"})
        if url.endswith("/v1internal:fetchAvailableModels"):
            return httpx.Response(
                200,
                json={"models": {"gemini-4-carbon": {"displayName": "Gemini 4 Carbon"}}},
            )
        raise AssertionError(f"Unexpected URL: {url}")

    _mock_httpx_client(monkeypatch, handler)

    # Unauthenticated access is rejected
    assert client.get("/api/v1/providers").status_code == 401

    # Full provider catalog lists all 6 providers with live models for Antigravity and Ollama
    catalog_res = client.get("/api/v1/providers", headers=headers)
    assert catalog_res.status_code == 200
    providers_by_id = {item["id"]: item for item in catalog_res.json()["providers"]}
    assert list(providers_by_id.keys()) == [
        "openai",
        "groq",
        "together",
        "ollama",
        "compatible",
        "antigravity",
    ]
    agy = providers_by_id["antigravity"]
    assert agy["requires_api_key"] is False
    assert agy["connected"] is True
    assert agy["model_source"] == "bridge_live"
    assert agy["default_model"] == "gemini-3.8-flash-medium"
    assert [m["id"] for m in agy["models"]] == [
        "claude-sonnet-4-6",
        "gemini-3.8-flash-medium",
        "gpt-oss-120b-medium",
    ]
    ollama = providers_by_id["ollama"]
    assert ollama["connected"] is True
    assert ollama["model_source"] == "ollama_live"
    assert ollama["default_model"] == "qwen2.5:14b"

    # Per-provider models endpoint works and rejects invalid providers
    agy_models_res = client.get("/api/v1/providers/antigravity/models", headers=headers)
    assert agy_models_res.status_code == 200
    assert agy_models_res.json()["default_model"] == "gemini-3.8-flash-medium"
    assert client.get("/api/v1/providers/unknown/models", headers=headers).status_code == 400

    # OAuth model probe returns live models without persisting or leaking token
    canary = _ya29_canary()
    with caplog.at_level(logging.INFO):
        probe_res = client.post(
            "/api/v1/providers/antigravity/models",
            headers=headers,
            json={"api_key": canary},
        )
    assert probe_res.status_code == 200
    assert probe_res.json()["model_source"] == "oauth_live"
    assert probe_res.json()["default_model"] == "gemini-4-carbon"
    assert canary not in probe_res.text
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert canary not in logs

    # Saving antigravity with omitted model auto-selects the live bridge model
    save_res = client.post("/api/v1/keys", headers=headers, json={"provider": "antigravity"})
    assert save_res.status_code == 200
    assert save_res.json() == {
        "configured": True,
        "provider": "antigravity",
        "model": "gemini-3.8-flash-medium",
    }

