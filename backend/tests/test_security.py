import base64
import hashlib
import hmac
import json
import logging
from pathlib import Path
from uuid import UUID

import pytest
import structlog

from app.config import settings
from app.db import SessionLocal, get_session
from app.errors import InvalidStoredKey
from app.logging import configure_logging, redact, secret_scope
from app.models import User
from app.security import (
    decode_jwt_sub,
    decrypt_api_key,
    encrypt_api_key,
    hash_password,
    validate_email,
    validate_runtime_secrets,
    verify_dummy_password,
    verify_password,
)


def _sk_canary() -> str:
    return "sk-canary-" + "DO-NOT-LEAK-" + "0123456789abcdef"


def _gsk_canary() -> str:
    return "gsk_" + "canary-" + "DO-NOT-LEAK-" + "abcdef0123456789"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_hs256(header: dict[str, object], payload: dict[str, object], secret: str) -> str:
    h_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h_b64}.{p_b64}.{_b64url(sig)}"


def test_validate_runtime_secrets_valid() -> None:
    validate_runtime_secrets()


def test_validate_runtime_secrets_rejects_bad_encryption_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "encryption_key", "")
    with pytest.raises(RuntimeError):
        validate_runtime_secrets()

    monkeypatch.setattr(settings, "encryption_key", "not-a-fernet-key")
    with pytest.raises(RuntimeError):
        validate_runtime_secrets()


def test_validate_runtime_secrets_rejects_short_jwt_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "too-short")
    with pytest.raises(RuntimeError):
        validate_runtime_secrets()


def test_fernet_roundtrip_and_invalid_token() -> None:
    for canary in (_sk_canary(), _gsk_canary()):
        ciphertext = encrypt_api_key(canary)
        assert canary.encode("utf-8") not in ciphertext
        assert decrypt_api_key(ciphertext) == canary

    with pytest.raises(InvalidStoredKey):
        decrypt_api_key(b"corrupted-token")


def test_scrypt_password_hashing_and_dummy_verify() -> None:
    stored = hash_password("correct-horse-battery")
    assert stored.startswith("scrypt$131072$8$1$")
    assert verify_password("correct-horse-battery", stored) is True
    assert verify_password("wrong-password", stored) is False
    assert verify_password("anything", "not-scrypt") is False
    verify_dummy_password("anything")


def test_validate_email() -> None:
    assert validate_email("  Streamer@Example.COM ") == "streamer@example.com"
    for bad in ("", "no-at", "a b@example.com", "@example.com", "a@localhost", "a@.com"):
        with pytest.raises(ValueError):
            validate_email(bad)


def test_decode_jwt_sub_validation_cases() -> None:
    now = 1_700_000_000.0
    sub = "00000000-0000-4000-8000-000000000001"
    base_payload: dict[str, object] = {
        "sub": sub,
        "iss": "clipforge",
        "aud": "clipforge-api",
        "iat": int(now),
        "exp": int(now) + 3600,
    }

    token = _sign_hs256({"alg": "HS256", "typ": "JWT"}, base_payload, settings.jwt_secret)
    assert decode_jwt_sub(token, now_ts=now + 60) == UUID(sub)

    # Expired within 30s skew passes; past 30s skew fails.
    assert decode_jwt_sub(token, now_ts=int(now) + 3630) == UUID(sub)
    with pytest.raises(ValueError):
        decode_jwt_sub(token, now_ts=int(now) + 3631)

    # alg=none
    h_none = _b64url(json.dumps({"alg": "none"}).encode("utf-8"))
    p_b64 = _b64url(json.dumps(base_payload).encode("utf-8"))
    with pytest.raises(ValueError):
        decode_jwt_sub(f"{h_none}.{p_b64}.{_b64url(b'x' * 32)}", now_ts=now)

    # crit header rejected
    crit_tok = _sign_hs256({"alg": "HS256", "crit": ["exp"]}, base_payload, settings.jwt_secret)
    with pytest.raises(ValueError):
        decode_jwt_sub(crit_tok, now_ts=now)

    # wrong secret rejected
    bad_sig = _sign_hs256(
        {"alg": "HS256"}, base_payload, "wrong-secret-at-least-32-characters-long"
    )
    with pytest.raises(ValueError):
        decode_jwt_sub(bad_sig, now_ts=now)

    # array aud rejected
    arr_aud = _sign_hs256(
        {"alg": "HS256"}, {**base_payload, "aud": ["clipforge-api"]}, settings.jwt_secret
    )
    with pytest.raises(ValueError):
        decode_jwt_sub(arr_aud, now_ts=now)

    # 7-day token rejected (> 26h)
    seven_day = _sign_hs256(
        {"alg": "HS256"}, {**base_payload, "exp": int(now) + 7 * 86400}, settings.jwt_secret
    )
    with pytest.raises(ValueError):
        decode_jwt_sub(seven_day, now_ts=now)


def test_structlog_redaction_strips_canaries_from_fields_and_tracebacks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging()
    sk = _sk_canary()
    gsk = _gsk_canary()
    with secret_scope(gsk):
        with caplog.at_level(logging.INFO):
            log = structlog.get_logger("test.redact")
            log.info("secret_field_check", api_key=sk, password="my-secret-password")
            try:
                raise RuntimeError(f"upstream failed for {sk} and {gsk}")
            except RuntimeError:
                log.exception("upstream_boom")

        assert redact(f"Bearer {gsk}") == "Bearer [REDACTED]"

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert sk not in joined
    assert gsk not in joined
    assert "my-secret-password" not in joined
    assert "[REDACTED]" in joined


def test_get_session_commits_and_rolls_back(clean_db: None) -> None:
    gen = get_session()
    session = next(gen)
    user = User(email="commit-check@example.com", password_hash=hash_password("supersecret123"))
    session.add(user)
    with pytest.raises(StopIteration):
        next(gen)

    with SessionLocal() as verify_session:
        found = verify_session.query(User).filter(User.email == "commit-check@example.com").one()
        assert found.email == "commit-check@example.com"

    gen_err = get_session()
    bad_session = next(gen_err)
    bad_session.add(
        User(email="rollback-check@example.com", password_hash=hash_password("supersecret123"))
    )
    with pytest.raises(RuntimeError):
        gen_err.throw(RuntimeError("boom"))

    with SessionLocal() as verify_session:
        missing = (
            verify_session.query(User)
            .filter(User.email == "rollback-check@example.com")
            .one_or_none()
        )
        assert missing is None


def test_canaries_absent_from_repo() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    backend_root = Path(__file__).resolve().parents[1]
    root = repo_root if (repo_root / "AGENTS.md").exists() else backend_root
    sk = _sk_canary()
    gsk = _gsk_canary()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(
            part in {".git", "node_modules", ".next", "__pycache__", ".pytest_cache"}
            for part in path.parts
        ):
            continue
        text = path.read_text(errors="ignore")
        assert sk not in text, f"Found sk canary in {path}"
        assert gsk not in text, f"Found gsk canary in {path}"
