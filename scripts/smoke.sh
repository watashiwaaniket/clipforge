#!/usr/bin/env bash
# End-to-end operator smoke test (drives a real VOD URL -> completed job + ready clip).
#
# Usage:
#   SMOKE_EMAIL=smoke@example.com SMOKE_PASSWORD=your-password-10+chars \
#   SMOKE_URL="https://www.youtube.com/watch?v=..." \
#   SMOKE_API_KEY="sk-..." SMOKE_PROVIDER=openai ./scripts/smoke.sh
#
# Notes:
# - Whisper transcription is billed to the user's OpenAI key when SMOKE_PROVIDER=openai,
#   and falls back to OPENAI_API_KEY in .env only when SMOKE_PROVIDER is not an OpenAI host.
# - Highlight scoring is always billed to the user's configured provider.
# - Killing the worker once (docker compose kill -s KILL worker) resumes the job from valid
#   artifacts; failure and raw-disk cleanup occur at the 4th delivery attempt cap, the 45-minute
#   soft limit, or the reaper sweep.
set +x
set -euo pipefail

if [[ -z "${SMOKE_API_KEY:-}" ]]; then
  echo "SMOKE_API_KEY must be set and non-empty." >&2
  exit 1
fi
if [[ -z "${SMOKE_EMAIL:-}" || -z "${SMOKE_PASSWORD:-}" || -z "${SMOKE_URL:-}" ]]; then
  echo "SMOKE_EMAIL, SMOKE_PASSWORD, and SMOKE_URL are required." >&2
  exit 1
fi

SMOKE_PROVIDER="${SMOKE_PROVIDER:-openai}"
API_URL="${API_URL:-http://localhost:8000}"

if [[ -z "${JWT_SECRET:-}" && -f ".env" ]]; then
  JWT_SECRET="$(python3 - <<'PY'
import pathlib
for line in pathlib.Path(".env").read_text(encoding="utf-8").splitlines():
    stripped = line.strip()
    if stripped.startswith("JWT_SECRET="):
        print(stripped.split("=", 1)[1].strip().strip('"').strip("'"))
        break
PY
)"
fi

if [[ -z "${JWT_SECRET:-}" || "${#JWT_SECRET}" -lt 32 ]]; then
  echo "JWT_SECRET (32+ chars) must be set in the environment or .env." >&2
  exit 1
fi

export SMOKE_EMAIL SMOKE_PASSWORD SMOKE_URL SMOKE_PROVIDER SMOKE_API_KEY API_URL JWT_SECRET
export SMOKE_BASE_URL="${SMOKE_BASE_URL:-}"
export SMOKE_MODEL="${SMOKE_MODEL:-}"

exec python3 - <<'PY'
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_hs256_jwt(user_id: str, secret: str) -> str:
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {
                "sub": user_id,
                "iss": "clipforge",
                "aud": "clipforge-api",
                "iat": now,
                "exp": now + 3600,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = _b64url(hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def request_json(method: str, url: str, payload: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"detail": body}
        return exc.code, parsed


api_url = os.environ["API_URL"].rstrip("/")
email = os.environ["SMOKE_EMAIL"]
password = os.environ["SMOKE_PASSWORD"]

reg_status, reg_body = request_json(
    "POST",
    f"{api_url}/api/v1/auth/register",
    {"email": email, "password": password},
)
if reg_status not in (201, 400):
    print(f"register failed ({reg_status}): {reg_body.get('detail')}", file=sys.stderr)
    sys.exit(1)

login_status, login_body = request_json(
    "POST",
    f"{api_url}/api/v1/auth/login",
    {"email": email, "password": password},
)
if login_status != 200 or not login_body.get("id"):
    print(f"login failed ({login_status}): {login_body.get('detail')}", file=sys.stderr)
    sys.exit(1)

token = sign_hs256_jwt(str(login_body["id"]), os.environ["JWT_SECRET"])

key_payload: dict[str, object] = {
    "provider": os.environ["SMOKE_PROVIDER"],
    "api_key": os.environ["SMOKE_API_KEY"],
}
if os.environ.get("SMOKE_BASE_URL"):
    key_payload["base_url"] = os.environ["SMOKE_BASE_URL"]
if os.environ.get("SMOKE_MODEL"):
    key_payload["model"] = os.environ["SMOKE_MODEL"]

key_status, key_body = request_json("POST", f"{api_url}/api/v1/keys", key_payload, token=token)
if key_status != 200:
    print(f"key save failed ({key_status}): {key_body.get('detail')}", file=sys.stderr)
    sys.exit(1)

job_status, job_body = request_json(
    "POST",
    f"{api_url}/api/v1/jobs",
    {"url": os.environ["SMOKE_URL"]},
    token=token,
)
if job_status != 202 or not job_body.get("job_id"):
    print(f"job create failed ({job_status}): {job_body.get('detail')}", file=sys.stderr)
    sys.exit(1)

job_id = job_body["job_id"]
print(f"Enqueued job {job_id}")

deadline = time.monotonic() + 45 * 60
last_detail: dict = {}
while time.monotonic() < deadline:
    status_code, detail = request_json("GET", f"{api_url}/api/v1/jobs/{job_id}", token=token)
    if status_code != 200:
        print(f"poll failed ({status_code}): {detail.get('detail')}", file=sys.stderr)
        sys.exit(1)
    last_detail = detail
    state = detail.get("status")
    if state in ("completed", "failed"):
        clips = detail.get("clips") or []
        ready_clips = [c for c in clips if c.get("status") == "ready"]
        if state == "completed" and ready_clips:
            print(f"status={state} ready_clips={len(ready_clips)}")
            sys.exit(0)
        print(
            f"status={state} error={detail.get('error')} progress_message={detail.get('progress_message')}",
            file=sys.stderr,
        )
        sys.exit(1)
    time.sleep(5)

print(
    f"status={last_detail.get('status')} error={last_detail.get('error')} "
    f"progress_message={last_detail.get('progress_message')}",
    file=sys.stderr,
)
sys.exit(1)
PY
