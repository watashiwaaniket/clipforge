import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.logging import push_secret
from app.services.providers.antigravity.errors import (
    AntigravityApiError,
    AntigravityCliError,
)
from app.services.providers.base import ProviderModelInfo, ResolvedProviderConfig
from app.services.providers.errors import ProviderConfigError
from app.services.providers.url_policy import (
    ANTIGRAVITY_BRIDGE_HTTP_POLICY,
    DEFAULT_HTTPS_POLICY,
    validate_outbound_url,
)

ANTIGRAVITY_PROVIDER = "antigravity"
ANTIGRAVITY_DEFAULT_BASE_URL = "https://daily-cloudcode-pa.googleapis.com"
ANTIGRAVITY_DEFAULT_MODEL = "gemini-4-hydrogen"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
LOCAL_CREDENTIAL_SENTINELS = frozenset({"", "local", "cli", "oauth", "antigravity-local"})
DEFAULT_CLI_TIMEOUT_S = 30.0
CLI_VALIDATION_TIMEOUT_S = 15.0
BRIDGE_PROBE_TIMEOUT = httpx.Timeout(1.5, connect=1.0)
_MODEL_LINE_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._:-]+)\s{2,}(.+?)\s*$")

FALLBACK_ANTIGRAVITY_MODELS: tuple[ProviderModelInfo, ...] = (
    ProviderModelInfo(id="gemini-4-hydrogen", name="Gemini 4 Hydrogen"),
    ProviderModelInfo(id="gemini-4-argon", name="Gemini 4 Argon"),
    ProviderModelInfo(id="gemini-4-carbon", name="Gemini 4 Carbon"),
    ProviderModelInfo(id="gemini-3.8-flash-high", name="Gemini 3.8 Flash (High)"),
    ProviderModelInfo(id="gemini-3.8-flash-medium", name="Gemini 3.8 Flash (Medium)"),
    ProviderModelInfo(id="gemini-3.8-flash-low", name="Gemini 3.8 Flash (Low)"),
    ProviderModelInfo(id="claude-sonnet-4-6", name="Claude Sonnet 4.6 (Thinking)"),
    ProviderModelInfo(id="claude-opus-4-6-thinking", name="Claude Opus 4.6 (Thinking)"),
    ProviderModelInfo(id="gpt-oss-120b-medium", name="GPT-OSS 120B (Medium)"),
)


@dataclass
class _OAuthCredentials:
    access_token: str | None = None
    refresh_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    project_id: str | None = None
    expiry_epoch_s: float | None = None


def is_local_antigravity_sentinel(credential: str | None) -> bool:
    if credential is None:
        return True
    return credential.strip().lower() in LOCAL_CREDENTIAL_SENTINELS


def resolve_antigravity_config(
    base_url: str | None,
    model: str | None,
) -> ResolvedProviderConfig:
    clean_base_url = base_url.strip() if base_url is not None else None
    if clean_base_url == "":
        clean_base_url = None

    clean_model = model.strip() if model is not None else None
    if clean_model == "":
        clean_model = None

    if clean_base_url is not None:
        raise ProviderConfigError("That provider uses a fixed base URL.")

    resolved_model = clean_model if clean_model is not None else ANTIGRAVITY_DEFAULT_MODEL
    validated_url = validate_outbound_url(
        ANTIGRAVITY_DEFAULT_BASE_URL,
        policy=DEFAULT_HTTPS_POLICY,
    )
    return ResolvedProviderConfig(
        provider=ANTIGRAVITY_PROVIDER,
        base_url=validated_url,
        model=resolved_model,
    )


_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]+$")


def parse_agy_models_output(raw: str) -> list[ProviderModelInfo]:
    models: list[ProviderModelInfo] = []
    seen: set[str] = set()
    for chunk in raw.replace("\r", "\n").splitlines():
        line = chunk.strip()
        if not line:
            continue
        match = _MODEL_LINE_RE.match(line)
        if match:
            model_id, display_name = match.group(1), match.group(2).strip()
        else:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            model_id, display_name = parts[0], parts[1].strip()
        if (
            not model_id
            or model_id.endswith(":")
            or not _MODEL_ID_RE.match(model_id)
            or model_id in seen
        ):
            continue
        if model_id.lower() in {"fetching", "available", "error"}:
            continue
        seen.add(model_id)
        models.append(ProviderModelInfo(id=model_id, name=display_name))
    return models


def _parse_expiry_epoch(raw_expiry: object) -> float | None:
    if isinstance(raw_expiry, bool) or not isinstance(raw_expiry, (int, float)):
        return None
    val = float(raw_expiry)
    if val > 1e11:
        val /= 1000.0
    return val


def _clean_optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _parse_oauth_credential(raw: str) -> _OAuthCredentials:
    text = raw.strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderConfigError("Antigravity OAuth credentials must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ProviderConfigError("Antigravity OAuth credentials JSON must be an object.")
        access_token = _clean_optional_str(payload.get("access_token"))
        refresh_token = _clean_optional_str(payload.get("refresh_token"))
        client_id = _clean_optional_str(payload.get("client_id"))
        client_secret = _clean_optional_str(payload.get("client_secret"))
        project_id = _clean_optional_str(payload.get("project_id")) or _clean_optional_str(
            payload.get("cloudaicompanionProject")
        )
        expiry_epoch_s = _parse_expiry_epoch(payload.get("expiry_date"))
        if expiry_epoch_s is None:
            expiry_epoch_s = _parse_expiry_epoch(payload.get("expires_at"))

        if not access_token and not refresh_token:
            raise ProviderConfigError(
                "Antigravity OAuth JSON must include access_token or refresh_token."
            )
        for secret in (access_token, refresh_token, client_secret):
            if secret:
                push_secret(secret)
        return _OAuthCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            project_id=project_id,
            expiry_epoch_s=expiry_epoch_s,
        )

    push_secret(text)
    return _OAuthCredentials(access_token=text)


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _resolve_cli_binary() -> str | None:
    configured = settings.antigravity_cli_bin.strip()
    if configured:
        candidate = Path(configured)
        if _is_executable_file(candidate):
            return str(candidate)
        resolved = shutil.which(configured)
        if resolved:
            return resolved
        raise ProviderConfigError(f"Configured ANTIGRAVITY_CLI_BIN was not found: {configured}")

    found = shutil.which("agy")
    if found:
        return found

    home_bin = Path.home() / ".local" / "bin" / "agy"
    if _is_executable_file(home_bin):
        return str(home_bin)
    return None


def _resolve_local_oauth_path() -> Path | None:
    configured = settings.antigravity_oauth_creds_path.strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return candidate
        raise ProviderConfigError(
            f"Configured ANTIGRAVITY_OAUTH_CREDS_PATH was not found: {configured}"
        )

    for candidate in (
        Path.home() / ".gemini" / "oauth_creds.json",
        Path.home() / ".gemini" / "antigravity-cli" / "oauth_creds.json",
    ):
        if candidate.is_file():
            return candidate
    return None


def _resolve_default_project_id() -> str | None:
    configured = settings.antigravity_project_id.strip()
    if configured:
        return configured

    project_file = Path.home() / ".gemini" / "antigravity-cli" / "cache" / "default_project_id.txt"
    if project_file.is_file():
        try:
            text = project_file.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            return None
    return None


def _validate_bridge_url(raw_url: str) -> str:
    url = raw_url.strip()
    if url.lower().startswith("https://"):
        return validate_outbound_url(url, policy=DEFAULT_HTTPS_POLICY)
    return validate_outbound_url(url, policy=ANTIGRAVITY_BRIDGE_HTTP_POLICY)


def _bridge_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = settings.antigravity_bridge_token.strip()
    if token:
        push_secret(token)
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _probe_bridge(client_factory: Callable[..., httpx.Client]) -> str | None:
    raw_url = settings.antigravity_bridge_url.strip()
    if not raw_url:
        return None
    try:
        validated_bridge = _validate_bridge_url(raw_url)
    except ProviderConfigError:
        return None

    try:
        with client_factory(follow_redirects=False, timeout=BRIDGE_PROBE_TIMEOUT) as client:
            resp = client.get(f"{validated_bridge}/health")
            if resp.status_code != 200:
                return None
            payload = resp.json()
            if isinstance(payload, dict) and payload.get("ok") is True:
                return validated_bridge
    except (httpx.HTTPError, ValueError):
        return None
    return None


def _format_cli_prompt(messages: list[dict[str, str]]) -> str:
    system_parts: list[str] = []
    dialogue_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        elif role == "assistant":
            dialogue_parts.append(f"Assistant:\n{content}")
        else:
            dialogue_parts.append(content if len(messages) <= 2 else f"User:\n{content}")

    sections: list[str] = []
    if system_parts:
        sections.append("System instructions:\n" + "\n\n".join(system_parts))
    if dialogue_parts:
        sections.append("\n\n".join(dialogue_parts))
    return "\n\n".join(sections).strip() or "ping"


def _parse_cli_json_output(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            line_s = line.strip()
            if not line_s.startswith("{"):
                continue
            try:
                parsed = json.loads(line_s)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    return None


def _extract_gemini_text(payload: dict[str, Any]) -> str | None:
    inner = payload.get("response")
    root = inner if isinstance(inner, dict) else payload
    candidates = root.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    if not isinstance(first, dict):
        return None
    content = first.get("content")
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    texts = [
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    joined = "".join(texts)
    return joined if joined else None


def _mentions_response_mime_type(status_code: int, body_text: str) -> bool:
    if status_code != 400:
        return False
    lowered = body_text.lower()
    return "responsemimetype" in lowered or "response_mime_type" in lowered


def _parse_fetch_available_models(payload: dict[str, Any]) -> list[ProviderModelInfo]:
    raw_models = payload.get("models")
    if not isinstance(raw_models, dict):
        return []
    parsed: list[ProviderModelInfo] = []
    for model_id, meta in raw_models.items():
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        display_name = model_id.strip()
        if isinstance(meta, dict):
            raw_name = meta.get("displayName") or meta.get("name")
            if isinstance(raw_name, str) and raw_name.strip():
                display_name = raw_name.strip()
        parsed.append(ProviderModelInfo(id=model_id.strip(), name=display_name))
    return parsed


class AntigravityProvider:
    def __init__(
        self,
        *,
        mode: str,
        model: str,
        base_url: str = ANTIGRAVITY_DEFAULT_BASE_URL,
        bridge_url: str | None = None,
        cli_bin: str | None = None,
        oauth: _OAuthCredentials | None = None,
        http_client: httpx.Client | None = None,
        owns_http_client: bool = False,
        timeout: httpx.Timeout | float = DEFAULT_CLI_TIMEOUT_S,
        cli_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.mode = mode
        self.model = model
        self.base_url = base_url
        self._bridge_url = bridge_url
        self._cli_bin = cli_bin
        self._oauth = oauth
        self._http_client = http_client
        self._owns_http_client = owns_http_client
        self._timeout = timeout
        self._cli_runner = cli_runner or subprocess.run
        self._auth_lock = threading.Lock()

    def close(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            self._http_client.close()

    def __enter__(self) -> "AntigravityProvider":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _timeout_seconds(self, override: float | None = None) -> float:
        if override is not None:
            return override
        if isinstance(self._timeout, httpx.Timeout):
            return float(self._timeout.read or self._timeout.connect or DEFAULT_CLI_TIMEOUT_S)
        return float(self._timeout)

    def _ensure_access_token(self) -> str:
        assert self._oauth is not None
        assert self._http_client is not None

        with self._auth_lock:
            now = time.time()
            needs_refresh = not self._oauth.access_token or (
                self._oauth.expiry_epoch_s is not None and now >= self._oauth.expiry_epoch_s - 60.0
            )
            if not needs_refresh and self._oauth.access_token:
                return self._oauth.access_token

            if self._oauth.refresh_token and self._oauth.client_id:
                form_data = {
                    "client_id": self._oauth.client_id,
                    "refresh_token": self._oauth.refresh_token,
                    "grant_type": "refresh_token",
                }
                if self._oauth.client_secret:
                    form_data["client_secret"] = self._oauth.client_secret
                try:
                    resp = self._http_client.post(GOOGLE_OAUTH_TOKEN_URL, data=form_data)
                except httpx.TimeoutException as exc:
                    raise AntigravityApiError(
                        408, "Antigravity OAuth token refresh timed out.", retryable=True
                    ) from exc
                except httpx.HTTPError as exc:
                    raise AntigravityApiError(
                        502, "Antigravity OAuth token refresh failed.", retryable=True
                    ) from exc

                if resp.status_code >= 400:
                    raise ProviderConfigError(
                        "Antigravity OAuth token refresh was rejected. Re-authenticate and retry."
                    )

                try:
                    token_data = resp.json()
                except ValueError as exc:
                    raise ProviderConfigError(
                        "Antigravity OAuth refresh returned an invalid response."
                    ) from exc
                if not isinstance(token_data, dict):
                    raise ProviderConfigError(
                        "Antigravity OAuth refresh returned an unexpected response."
                    )
                new_token = _clean_optional_str(token_data.get("access_token"))
                if not new_token:
                    raise ProviderConfigError("Antigravity OAuth refresh did not return a token.")
                push_secret(new_token)
                self._oauth.access_token = new_token
                expires_in = token_data.get("expires_in")
                if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
                    self._oauth.expiry_epoch_s = time.time() + float(expires_in)
                return new_token

            raise ProviderConfigError(
                "Antigravity OAuth credentials are expired and cannot be refreshed without "
                "refresh_token and client_id."
            )

    def _ensure_project_id(self, access_token: str) -> str | None:
        assert self._oauth is not None
        assert self._http_client is not None

        if self._oauth.project_id:
            return self._oauth.project_id

        with self._auth_lock:
            if self._oauth.project_id:
                return self._oauth.project_id

            try:
                resp = self._http_client.post(
                    f"{self.base_url}/v1internal:loadCodeAssist",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    json={"metadata": {"ideType": "ANTIGRAVITY"}},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    proj = data.get("cloudaicompanionProject") if isinstance(data, dict) else None
                    if isinstance(proj, str) and proj.strip():
                        self._oauth.project_id = proj.strip()
                        return self._oauth.project_id
                    if isinstance(proj, dict):
                        proj_id = _clean_optional_str(proj.get("id"))
                        if proj_id:
                            self._oauth.project_id = proj_id
                            return proj_id
            except (httpx.HTTPError, ValueError):
                pass

            fallback = _resolve_default_project_id()
            if fallback:
                self._oauth.project_id = fallback
            return self._oauth.project_id

    def fetch_available_models(self) -> list[ProviderModelInfo]:
        assert self._http_client is not None
        access_token = self._ensure_access_token()
        project_id = self._ensure_project_id(access_token)
        body: dict[str, Any] = {}
        if project_id:
            body["project"] = project_id
        resp = self._http_client.post(
            f"{self.base_url}/v1internal:fetchAvailableModels",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        if resp.status_code >= 400:
            raise AntigravityApiError(
                resp.status_code,
                f"Antigravity fetchAvailableModels returned HTTP {resp.status_code}.",
            )
        payload = resp.json()
        if not isinstance(payload, dict):
            return []
        return _parse_fetch_available_models(payload)

    def _complete_oauth(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> str | None:
        assert self._http_client is not None
        access_token = self._ensure_access_token()
        project_id = self._ensure_project_id(access_token)

        system_parts: list[dict[str, str]] = []
        contents: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            text = msg.get("content", "")
            if not text:
                continue
            if role == "system":
                system_parts.append({"text": text})
            else:
                gemini_role = "model" if role == "assistant" else "user"
                contents.append({"role": gemini_role, "parts": [{"text": text}]})

        if not contents:
            contents = [{"role": "user", "parts": [{"text": "ping"}]}]

        def _post(include_response_mime_type: bool) -> httpx.Response:
            generation_config: dict[str, Any] = {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            }
            if include_response_mime_type:
                generation_config["responseMimeType"] = "application/json"

            inner: dict[str, Any] = {
                "contents": contents,
                "generationConfig": generation_config,
            }
            if system_parts:
                inner["systemInstruction"] = {"parts": system_parts}

            envelope: dict[str, Any] = {
                "model": self.model,
                "request": inner,
            }
            if project_id:
                envelope["project"] = project_id

            try:
                return self._http_client.post(
                    f"{self.base_url}/v1internal:generateContent",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    json=envelope,
                )
            except httpx.TimeoutException as exc:
                raise AntigravityApiError(
                    408, "Antigravity request timed out.", retryable=True
                ) from exc
            except httpx.HTTPError as exc:
                raise AntigravityApiError(
                    502, "Antigravity network error.", retryable=True
                ) from exc

        resp = _post(include_response_mime_type=json_mode)
        if json_mode and _mentions_response_mime_type(resp.status_code, resp.text):
            resp = _post(include_response_mime_type=False)

        if resp.status_code >= 400:
            raise AntigravityApiError(
                resp.status_code,
                f"Antigravity API returned HTTP {resp.status_code}.",
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise AntigravityApiError(
                502, "Antigravity returned non-JSON payload.", retryable=False
            ) from exc

        if not isinstance(payload, dict):
            raise AntigravityApiError(
                502, "Antigravity returned unexpected payload shape.", retryable=False
            )
        return _extract_gemini_text(payload)

    def _complete_cli(
        self,
        *,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None,
        timeout_override: float | None = None,
    ) -> str | None:
        assert self._cli_bin is not None
        prompt = _format_cli_prompt(messages)
        cmd = [
            self._cli_bin,
            "--disable-slash-commands",
            "--output-format",
            "json",
            "--model",
            self.model,
        ]
        if json_schema is not None:
            cmd.extend(["--json-schema", json.dumps(json_schema, separators=(",", ":"))])
        cmd.extend(["--print", prompt])

        timeout_s = self._timeout_seconds(timeout_override)
        try:
            proc = self._cli_runner(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise AntigravityCliError("Antigravity CLI timed out.", retryable=True) from exc
        except OSError as exc:
            raise ProviderConfigError("Failed to execute Antigravity CLI ('agy').") from exc

        parsed = _parse_cli_json_output(proc.stdout or "")
        if proc.returncode != 0 or (parsed is not None and parsed.get("status") != "SUCCESS"):
            err_msg = ""
            if parsed and isinstance(parsed.get("error"), str):
                err_msg = parsed["error"]
            elif proc.stderr:
                err_msg = proc.stderr.strip()
            lowered = err_msg.lower()
            if "not logged into antigravity" in lowered:
                raise ProviderConfigError(
                    "Antigravity CLI is not signed in. Run 'agy' to sign in first."
                )
            if (
                "invalid model selection" in lowered
                or "is not recognized as a known model" in lowered
            ):
                raise ProviderConfigError(
                    f"Antigravity CLI rejected model '{self.model}'. "
                    "Choose a model shown by 'agy models'."
                )
            raise AntigravityCliError(
                err_msg or f"Antigravity CLI exited with status {proc.returncode}.",
                retryable=proc.returncode != 0,
            )

        if parsed is None:
            return None

        if json_schema is not None and isinstance(parsed.get("structured_output"), (dict, list)):
            return json.dumps(parsed["structured_output"])

        response = parsed.get("response")
        return response if isinstance(response, str) else None

    def _complete_bridge(
        self,
        *,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None,
        timeout_override: float | None = None,
    ) -> str | None:
        assert self._http_client is not None
        assert self._bridge_url is not None
        prompt = _format_cli_prompt(messages)
        timeout_s = self._timeout_seconds(timeout_override)
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "timeout_s": timeout_s,
        }
        if json_schema is not None:
            body["json_schema"] = json_schema

        connect_timeout = (
            self._timeout.connect
            if isinstance(self._timeout, httpx.Timeout) and self._timeout.connect is not None
            else 5.0
        )
        request_timeout = httpx.Timeout(timeout_s + 2.0, connect=connect_timeout)

        try:
            resp = self._http_client.post(
                f"{self._bridge_url}/v1/complete",
                headers=_bridge_headers(),
                json=body,
                timeout=request_timeout,
            )
        except httpx.TimeoutException as exc:
            raise AntigravityApiError(
                504, "Antigravity host bridge timed out.", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise AntigravityApiError(
                502, "Could not reach Antigravity host bridge.", retryable=True
            ) from exc

        detail = ""
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
            detail = payload["detail"]

        if resp.status_code == 400:
            lowered = detail.lower()
            if "not logged into antigravity" in lowered:
                raise ProviderConfigError(
                    "Antigravity CLI is not signed in. Run 'agy' to sign in first."
                )
            if (
                "invalid model selection" in lowered
                or "is not recognized as a known model" in lowered
            ):
                raise ProviderConfigError(
                    f"Antigravity rejected model '{self.model}'. "
                    "Select an available model from /api/v1/providers."
                )
            raise ProviderConfigError(detail or "Antigravity bridge rejected the request.")
        if resp.status_code in {401, 403}:
            raise ProviderConfigError("Antigravity host bridge rejected authentication.")
        if resp.status_code >= 400:
            raise AntigravityApiError(
                resp.status_code,
                detail or f"Antigravity bridge returned HTTP {resp.status_code}.",
                retryable=True,
            )

        if not isinstance(payload, dict):
            return None

        if json_schema is not None and isinstance(payload.get("structured_output"), (dict, list)):
            return json.dumps(payload["structured_output"])

        response = payload.get("response")
        return response if isinstance(response, str) else None

    def validate_credentials(self, *, timeout: float | None = None) -> None:
        if self.mode == "cli":
            cli_timeout = timeout if timeout is not None else CLI_VALIDATION_TIMEOUT_S
            reply = self._complete_cli(
                messages=[{"role": "user", "content": "ping"}],
                json_schema=None,
                timeout_override=cli_timeout,
            )
        elif self.mode == "bridge":
            cli_timeout = timeout if timeout is not None else CLI_VALIDATION_TIMEOUT_S
            reply = self._complete_bridge(
                messages=[{"role": "user", "content": "ping"}],
                json_schema=None,
                timeout_override=cli_timeout,
            )
        else:
            reply = self._complete_oauth(
                messages=[{"role": "user", "content": "ping"}],
                temperature=0.0,
                max_tokens=1,
                json_mode=False,
            )

        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("Antigravity returned an empty validation response.")

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 800,
        json_mode: bool = True,
        json_schema: dict[str, Any] | None = None,
    ) -> str | None:
        if self.mode == "cli":
            return self._complete_cli(messages=messages, json_schema=json_schema)
        if self.mode == "bridge":
            return self._complete_bridge(messages=messages, json_schema=json_schema)
        return self._complete_oauth(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )


def build_antigravity_provider(
    *,
    credential: str | None,
    base_url: str = ANTIGRAVITY_DEFAULT_BASE_URL,
    model: str = ANTIGRAVITY_DEFAULT_MODEL,
    timeout: httpx.Timeout | float = DEFAULT_CLI_TIMEOUT_S,
    http_client: httpx.Client | None = None,
    http_client_factory: Callable[..., httpx.Client] | None = None,
    cli_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> AntigravityProvider:
    validated_url = validate_outbound_url(base_url, policy=DEFAULT_HTTPS_POLICY)
    if http_client is not None and getattr(http_client, "follow_redirects", False):
        raise ProviderConfigError("HTTP client must disable redirects.")

    client_factory = http_client_factory or httpx.Client

    if not is_local_antigravity_sentinel(credential):
        assert credential is not None
        oauth = _parse_oauth_credential(credential)
        owns_client = http_client is None
        resolved_http = http_client or client_factory(
            follow_redirects=False,
            timeout=timeout,
        )
        return AntigravityProvider(
            mode="oauth",
            model=model,
            base_url=validated_url,
            oauth=oauth,
            http_client=resolved_http,
            owns_http_client=owns_client,
            timeout=timeout,
        )

    cli_bin = _resolve_cli_binary()
    if cli_bin is not None:
        return AntigravityProvider(
            mode="cli",
            model=model,
            base_url=validated_url,
            cli_bin=cli_bin,
            timeout=timeout,
            cli_runner=cli_runner,
        )

    bridge_url = _probe_bridge(client_factory)
    if bridge_url is not None:
        owns_client = http_client is None
        resolved_http = http_client or client_factory(
            follow_redirects=False,
            timeout=timeout,
        )
        return AntigravityProvider(
            mode="bridge",
            model=model,
            base_url=validated_url,
            bridge_url=bridge_url,
            http_client=resolved_http,
            owns_http_client=owns_client,
            timeout=timeout,
        )

    oauth_path = _resolve_local_oauth_path()
    if oauth_path is not None:
        try:
            raw_json = oauth_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProviderConfigError(
                "Could not read local Antigravity OAuth credentials."
            ) from exc
        oauth = _parse_oauth_credential(raw_json)
        owns_client = http_client is None
        resolved_http = http_client or client_factory(
            follow_redirects=False,
            timeout=timeout,
        )
        return AntigravityProvider(
            mode="oauth",
            model=model,
            base_url=validated_url,
            oauth=oauth,
            http_client=resolved_http,
            owns_http_client=owns_client,
            timeout=timeout,
        )

    raise ProviderConfigError(
        "Antigravity CLI ('agy'), host bridge, and local OAuth credentials were not found. "
        "Start the host bridge (scripts/antigravity_bridge.py), sign in with 'agy', "
        "mount OAuth credentials, or provide an Antigravity OAuth token/JSON."
    )


MODEL_DISCOVERY_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


def list_antigravity_models(
    *,
    credential: str | None = None,
    base_url: str = ANTIGRAVITY_DEFAULT_BASE_URL,
    http_client_factory: Callable[..., httpx.Client] | None = None,
    cli_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[list[ProviderModelInfo], str, bool]:
    client_factory = http_client_factory or httpx.Client
    runner = cli_runner or subprocess.run

    if not is_local_antigravity_sentinel(credential):
        try:
            with build_antigravity_provider(
                credential=credential,
                base_url=base_url,
                timeout=MODEL_DISCOVERY_TIMEOUT,
                http_client_factory=client_factory,
                cli_runner=runner,
            ) as provider:
                models = provider.fetch_available_models()
                if models:
                    return models, "oauth_live", True
        except Exception:
            pass
        return list(FALLBACK_ANTIGRAVITY_MODELS), "fallback", False

    try:
        cli_bin = _resolve_cli_binary()
    except ProviderConfigError:
        cli_bin = None

    if cli_bin is not None:
        try:
            proc = runner(
                [cli_bin, "models"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10.0,
            )
            if proc.returncode == 0:
                parsed = parse_agy_models_output(proc.stdout or "")
                if parsed:
                    return parsed, "cli_live", True
        except (OSError, subprocess.TimeoutExpired):
            pass

    bridge_url = _probe_bridge(client_factory)
    if bridge_url is not None:
        try:
            with client_factory(
                follow_redirects=False, timeout=MODEL_DISCOVERY_TIMEOUT
            ) as client:
                resp = client.get(f"{bridge_url}/v1/models", headers=_bridge_headers())
                if resp.status_code == 200:
                    payload = resp.json()
                    raw_models = payload.get("models") if isinstance(payload, dict) else None
                    if isinstance(raw_models, list):
                        bridge_models = [
                            ProviderModelInfo(id=item["id"], name=item.get("name") or item["id"])
                            for item in raw_models
                            if isinstance(item, dict)
                            and isinstance(item.get("id"), str)
                            and item["id"].strip()
                        ]
                        if bridge_models:
                            return bridge_models, "bridge_live", True
        except (httpx.HTTPError, ValueError):
            pass

    try:
        oauth_path = _resolve_local_oauth_path()
    except ProviderConfigError:
        oauth_path = None

    if oauth_path is not None:
        try:
            raw_json = oauth_path.read_text(encoding="utf-8")
            with build_antigravity_provider(
                credential=raw_json,
                base_url=base_url,
                timeout=MODEL_DISCOVERY_TIMEOUT,
                http_client_factory=client_factory,
                cli_runner=runner,
            ) as provider:
                models = provider.fetch_available_models()
                if models:
                    return models, "oauth_live", True
        except Exception:
            pass

    return list(FALLBACK_ANTIGRAVITY_MODELS), "fallback", False


def choose_antigravity_default_model(
    models: list[ProviderModelInfo],
    connected: bool,
) -> str:
    if not connected or not models:
        return ANTIGRAVITY_DEFAULT_MODEL
    ids = [m.id for m in models]
    if ANTIGRAVITY_DEFAULT_MODEL in ids:
        return ANTIGRAVITY_DEFAULT_MODEL
    for preferred in (
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-low",
    ):
        if preferred in ids:
            return preferred
    for model_id in ids:
        if model_id.startswith("gemini-"):
            return model_id
    return ids[0]


def pick_antigravity_default_model(
    *,
    credential: str | None = None,
    http_client_factory: Callable[..., httpx.Client] | None = None,
    cli_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    models, _source, connected = list_antigravity_models(
        credential=credential,
        http_client_factory=http_client_factory,
        cli_runner=cli_runner,
    )
    return choose_antigravity_default_model(models, connected)
