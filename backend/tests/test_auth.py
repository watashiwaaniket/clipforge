import base64
import hashlib
import hmac
import json
import time
from collections.abc import Generator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import redis
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import User
from app.security import decode_jwt_sub, hash_password


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_hs256(header: dict[str, object], payload: dict[str, object], secret: str) -> str:
    h_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h_b64}.{p_b64}.{_b64url(sig)}"


@pytest.fixture(autouse=True)
def flush_test_redis() -> Generator[None, None, None]:
    client = redis.Redis.from_url(settings.redis_url)
    try:
        client.flushdb()
        yield
        client.flushdb()
    finally:
        client.close()


@pytest.fixture
def client(clean_db: None) -> Generator[TestClient, None, None]:
    with TestClient(app) as tc:
        yield tc


def test_register_login_and_error_parity(client: TestClient) -> None:
    res = client.post(
        "/api/v1/auth/register",
        json={"email": "  Streamer@Example.COM ", "password": "correct-horse-battery"},
    )
    assert res.status_code == 201
    created = res.json()
    assert created["email"] == "streamer@example.com"
    user_id = UUID(created["id"])

    # Second session sees the committed user.
    with SessionLocal() as verify_session:
        db_user = verify_session.get(User, user_id)
        assert db_user is not None
        assert db_user.email == "streamer@example.com"
        assert db_user.password_hash.startswith("scrypt$131072$8$1$")

    # Valid login returns 200 {id, email}
    login_ok = client.post(
        "/api/v1/auth/login",
        json={"email": "streamer@example.com", "password": "correct-horse-battery"},
    )
    assert login_ok.status_code == 200
    assert login_ok.json() == {"id": str(user_id), "email": "streamer@example.com"}

    # Wrong password -> 400 Invalid email or password.
    wrong_pw = client.post(
        "/api/v1/auth/login",
        json={"email": "streamer@example.com", "password": "wrong-password-123"},
    )
    assert wrong_pw.status_code == 400
    assert wrong_pw.json()["detail"] == "Invalid email or password."

    # Unknown email -> exact same 400 detail
    unknown_email = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "wrong-password-123"},
    )
    assert unknown_email.status_code == 400
    assert unknown_email.json()["detail"] == wrong_pw.json()["detail"]

    # Duplicate email -> exact same 400 detail as bad password
    dup = client.post(
        "/api/v1/auth/register",
        json={"email": "STREAMER@example.com", "password": "another-strong-password"},
    )
    assert dup.status_code == 400
    assert dup.json()["detail"] == wrong_pw.json()["detail"]

    # Short password -> distinct 400 detail
    short_pw = client.post(
        "/api/v1/auth/register",
        json={"email": "short@example.com", "password": "short"},
    )
    assert short_pw.status_code == 400
    assert short_pw.json()["detail"] == "Password must be at least 10 characters."
    assert short_pw.json()["detail"] != dup.json()["detail"]


def test_login_unknown_email_runs_dummy_scrypt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = 0

    def spy_dummy(password: str) -> None:
        nonlocal called
        called += 1

    monkeypatch.setattr("app.api.auth.verify_dummy_password", spy_dummy)
    res = client.post(
        "/api/v1/auth/login",
        json={"email": "missing@example.com", "password": "any-password-123"},
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "Invalid email or password."
    assert called == 1


def test_jose_fixture_and_jwt_rejection_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "jwt_jose.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    monkeypatch.setattr(settings, "jwt_secret", fixture["secret"])
    iat = int(fixture["iat"])
    exp = int(fixture["exp"])
    monkeypatch.setattr(time, "time", lambda: float(iat + 60))

    # Committed jose token verifies at iat + 60.
    assert decode_jwt_sub(fixture["token"]) == UUID(fixture["sub"])

    # jose token signed with wrong secret fails.
    with pytest.raises(ValueError):
        decode_jwt_sub(fixture["wrong_secret_token"])

    # alg=none (hand-built token) fails.
    header_none = _b64url(json.dumps({"alg": "none"}).encode("utf-8"))
    payload_b64 = _b64url(
        json.dumps(
            {
                "sub": fixture["sub"],
                "iss": "clipforge",
                "aud": "clipforge-api",
                "iat": iat,
                "exp": exp,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    with pytest.raises(ValueError):
        decode_jwt_sub(f"{header_none}.{payload_b64}.{_b64url(b'x' * 32)}")

    # Expired by more than 30 seconds fails.
    monkeypatch.setattr(time, "time", lambda: float(exp + 31))
    with pytest.raises(ValueError):
        decode_jwt_sub(fixture["token"])

    # Restore clock to valid window.
    monkeypatch.setattr(time, "time", lambda: float(iat + 60))

    # aud as array fails.
    arr_aud = _sign_hs256(
        {"alg": "HS256"},
        {
            "sub": fixture["sub"],
            "iss": "clipforge",
            "aud": ["clipforge-api"],
            "iat": iat,
            "exp": exp,
        },
        fixture["secret"],
    )
    with pytest.raises(ValueError):
        decode_jwt_sub(arr_aud)

    # exp - iat of 7 days fails (> 26h).
    seven_day = _sign_hs256(
        {"alg": "HS256"},
        {
            "sub": fixture["sub"],
            "iss": "clipforge",
            "aud": "clipforge-api",
            "iat": iat,
            "exp": iat + 7 * 86400,
        },
        fixture["secret"],
    )
    with pytest.raises(ValueError):
        decode_jwt_sub(seven_day)


def test_get_current_user_dependency(clean_db: None) -> None:
    probe_app = FastAPI()

    @probe_app.get("/me")
    def me(user: User = Depends(get_current_user)) -> dict[str, str]:
        return {"id": str(user.id), "email": user.email}

    with SessionLocal() as session:
        user = User(email="owner@example.com", password_hash=hash_password("correct-horse-battery"))
        session.add(user)
        session.commit()
        user_id = user.id

    now = int(time.time())
    valid_token = _sign_hs256(
        {"alg": "HS256"},
        {
            "sub": str(user_id),
            "iss": "clipforge",
            "aud": "clipforge-api",
            "iat": now,
            "exp": now + 3600,
        },
        settings.jwt_secret,
    )
    unknown_sub_token = _sign_hs256(
        {"alg": "HS256"},
        {
            "sub": str(uuid4()),
            "iss": "clipforge",
            "aud": "clipforge-api",
            "iat": now,
            "exp": now + 3600,
        },
        settings.jwt_secret,
    )

    with TestClient(probe_app) as tc:
        assert tc.get("/me").status_code == 401
        assert tc.get("/me").json()["detail"] == "Sign in required."

        bad_scheme = tc.get("/me", headers={"Authorization": f"Basic {valid_token}"})
        assert bad_scheme.status_code == 401
        assert bad_scheme.json()["detail"] == "Sign in required."

        unknown_sub = tc.get("/me", headers={"Authorization": f"Bearer {unknown_sub_token}"})
        assert unknown_sub.status_code == 401
        assert unknown_sub.json()["detail"] == "Sign in required."

        ok = tc.get("/me", headers={"Authorization": f"Bearer {valid_token}"})
        assert ok.status_code == 200
        assert ok.json() == {"id": str(user_id), "email": "owner@example.com"}


def test_auth_rate_limit_uses_rightmost_trusted_hop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "trust_proxy_count", 1)

    redis_client = redis.Redis.from_url(settings.redis_url)
    try:
        for _ in range(10):
            res = client.post(
                "/api/v1/auth/login",
                json={"email": "nobody@example.com", "password": "wrong-password-123"},
                headers={"X-Forwarded-For": "forged, real"},
            )
            assert res.status_code == 400

        assert redis_client.get("rl:auth:real") == b"10"
        assert redis_client.get("rl:auth:forged") is None

        blocked = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "wrong-password-123"},
            headers={"X-Forwarded-For": "forged, real"},
        )
        assert blocked.status_code == 429
        assert blocked.json()["detail"] == "Too many sign-in attempts. Wait a minute."

        other_real = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "wrong-password-123"},
            headers={"X-Forwarded-For": "forged, other"},
        )
        assert other_real.status_code == 400
        assert redis_client.get("rl:auth:other") == b"1"
    finally:
        redis_client.close()


def test_auth_rate_limit_ignores_xff_when_untrusted_or_short(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    redis_client = redis.Redis.from_url(settings.redis_url)
    try:
        monkeypatch.setattr(settings, "trust_proxy_count", 0)
        res = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "wrong-password-123"},
            headers={"X-Forwarded-For": "forged, real"},
        )
        assert res.status_code == 400
        assert redis_client.get("rl:auth:real") is None
        assert redis_client.get("rl:auth:testclient") == b"1"

        monkeypatch.setattr(settings, "trust_proxy_count", 2)
        res2 = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "wrong-password-123"},
            headers={"X-Forwarded-For": "only-one-hop"},
        )
        assert res2.status_code == 400
        assert redis_client.get("rl:auth:only-one-hop") is None
        assert redis_client.get("rl:auth:testclient") == b"2"
    finally:
        redis_client.close()
