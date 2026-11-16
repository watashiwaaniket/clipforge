import base64
import hashlib
import hmac
import json
import math
import os
import time
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.errors import InvalidStoredKey

SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 256 * 1024 * 1024

# Valid scrypt$131072$8$1$... hash so unknown-email logins still pay the scrypt cost.
DUMMY_PASSWORD_HASH = (
    "scrypt$131072$8$1$AAAAAAAAAAAAAAAAAAAAAA==$"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)


class InvalidJwtError(ValueError):
    pass


def _build_fernet() -> Fernet:
    key = settings.encryption_key.strip()
    if not key:
        raise RuntimeError("ENCRYPTION_KEY must be set and non-empty.")
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:
        raise RuntimeError("ENCRYPTION_KEY must be a valid Fernet key.") from exc


def validate_runtime_secrets() -> None:
    _build_fernet()
    if len(settings.jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET must be at least 32 characters.")


def encrypt_api_key(plaintext: str) -> bytes:
    fernet = _build_fernet()
    return fernet.encrypt(plaintext.encode("utf-8"))


def decrypt_api_key(token: bytes) -> str:
    try:
        fernet = _build_fernet()
        return fernet.decrypt(bytes(token)).decode("utf-8")
    except (InvalidToken, ValueError, TypeError, RuntimeError) as exc:
        raise InvalidStoredKey() from exc


def _scrypt_derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=SCRYPT_MAXMEM,
        dklen=SCRYPT_DKLEN,
    )


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = _scrypt_derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(derived).decode("ascii")
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt_b64}${hash_b64}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algo, n_s, r_s, p_s, salt_b64, hash_b64 = stored_hash.split("$")
        if algo != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
    except Exception:
        return False

    derived = _scrypt_derive(password, salt, n, r, p)
    try:
        return hmac.compare_digest(derived, expected)
    except TypeError:
        return False


def verify_dummy_password(password: str) -> None:
    verify_password(password, DUMMY_PASSWORD_HASH)


def validate_email(raw_email: str) -> str:
    email = raw_email.strip().lower()
    if not (3 <= len(email) <= 254):
        raise ValueError("Invalid email address.")
    if any(ch.isspace() for ch in email):
        raise ValueError("Invalid email address.")
    if email.count("@") != 1:
        raise ValueError("Invalid email address.")
    local, domain = email.split("@")
    if (
        not local
        or not domain
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
    ):
        raise ValueError("Invalid email address.")
    return email


def _b64url_decode(segment: str) -> bytes:
    if not segment or not segment.isascii():
        raise InvalidJwtError("Invalid segment encoding.")
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise InvalidJwtError("Base64url decode failed.") from exc


def decode_jwt_sub(token: str, *, now_ts: float | None = None) -> UUID:
    # Step 1: require string and 3 non-empty segments
    if not isinstance(token, str):
        raise InvalidJwtError("Token must be a string.")
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise InvalidJwtError("Malformed token segments.")
    h_b64, p_b64, s_b64 = parts

    # Step 2: base64url-decode header and payload
    try:
        header_bytes = _b64url_decode(h_b64)
        payload_bytes = _b64url_decode(p_b64)
        header = json.loads(header_bytes.decode("utf-8"))
        payload = json.loads(payload_bytes.decode("utf-8"))
    except InvalidJwtError:
        raise
    except Exception as exc:
        raise InvalidJwtError("Malformed JSON segment.") from exc

    # Step 3: header object, alg == "HS256", reject "crit"
    if not isinstance(header, dict):
        raise InvalidJwtError("Header must be an object.")
    if header.get("alg") != "HS256":
        raise InvalidJwtError("Unsupported algorithm.")
    if "crit" in header:
        raise InvalidJwtError("Critical header not supported.")

    # Step 4: decode signature, require exact 32 bytes
    signature = _b64url_decode(s_b64)
    if len(signature) != 32:
        raise InvalidJwtError("Invalid signature length.")

    # Step 5: compute HMAC-SHA256 over ASCII "{h_b64}.{p_b64}" using UTF-8 jwt_secret
    key = settings.jwt_secret.encode("utf-8")
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    mac = hmac.new(key, signing_input, hashlib.sha256).digest()

    # Step 6: constant-time compare of raw digests
    try:
        valid_sig = hmac.compare_digest(mac, signature)
    except TypeError as exc:
        raise InvalidJwtError("Signature comparison error.") from exc
    if not valid_sig:
        raise InvalidJwtError("Invalid signature.")

    # Step 7: validate payload claims
    if not isinstance(payload, dict):
        raise InvalidJwtError("Payload must be an object.")
    if payload.get("iss") != "clipforge":
        raise InvalidJwtError("Invalid issuer.")
    if payload.get("aud") != "clipforge-api":
        raise InvalidJwtError("Invalid audience.")

    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise InvalidJwtError("Invalid subject.")
    try:
        user_id = UUID(sub)
    except ValueError as exc:
        raise InvalidJwtError("Subject is not a UUID.") from exc

    iat = payload.get("iat")
    exp = payload.get("exp")
    if (
        isinstance(iat, bool)
        or isinstance(exp, bool)
        or not isinstance(iat, (int, float))
        or not isinstance(exp, (int, float))
        or not math.isfinite(iat)
        or not math.isfinite(exp)
    ):
        raise InvalidJwtError("Invalid timestamps.")

    # Step 8: expiry, issued-at skew, and 26-hour max lifetime
    now = time.time() if now_ts is None else now_ts
    if exp < now - 30:
        raise InvalidJwtError("Token expired.")
    if iat > now + 30:
        raise InvalidJwtError("Token issued in the future.")
    if exp - iat > 26 * 60 * 60:
        raise InvalidJwtError("Token lifetime exceeds limit.")

    return user_id
