#!/usr/bin/env python3
"""Lightweight host-side HTTP bridge to the local Antigravity (`agy`) CLI.

Allows ClipForge containers (`api` / `worker`) to discover available Antigravity
models and execute prompt completions using the host's signed-in `agy` session
without embedding macOS binaries or Keychain credentials inside Docker.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11436
MODELS_CACHE_TTL_S = 60.0
MAX_REQUEST_BYTES = 2 * 1024 * 1024
_MODEL_LINE_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._:-]+)\s{2,}(.+?)\s*$")


def resolve_agy_binary(explicit: str = "") -> str | None:
    candidate = explicit.strip() or os.environ.get("ANTIGRAVITY_CLI_BIN", "").strip()
    if candidate:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        found = shutil.which(candidate)
        return found
    found = shutil.which("agy")
    if found:
        return found
    home_bin = Path.home() / ".local" / "bin" / "agy"
    if home_bin.is_file() and os.access(home_bin, os.X_OK):
        return str(home_bin)
    return None


_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]+$")


def parse_agy_models_output(raw: str) -> list[dict[str, str]]:
    models: list[dict[str, str]] = []
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
        models.append({"id": model_id, "name": display_name, "provider": "antigravity"})
    return models


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


class BridgeState:
    def __init__(self, *, cli_bin: str | None, token: str = "") -> None:
        self.cli_bin = cli_bin
        self.token = token.strip()
        self._models_lock = threading.Lock()
        self._cached_models: list[dict[str, str]] | None = None
        self._cached_at: float = 0.0

    def get_models(self, *, refresh: bool = False) -> list[dict[str, str]]:
        if not self.cli_bin:
            raise FileNotFoundError("Antigravity CLI ('agy') not found on host.")

        with self._models_lock:
            now = time.monotonic()
            if (
                not refresh
                and self._cached_models is not None
                and (now - self._cached_at) < MODELS_CACHE_TTL_S
            ):
                return list(self._cached_models)

            proc = subprocess.run(
                [self.cli_bin, "models"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15.0,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip() or "Failed to list agy models."
                raise RuntimeError(detail)

            models = parse_agy_models_output(proc.stdout or "")
            self._cached_models = models
            self._cached_at = time.monotonic()
            return list(models)


def make_handler(state: BridgeState) -> type[BaseHTTPRequestHandler]:
    class AntigravityBridgeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _check_auth(self) -> bool:
            if not state.token:
                return True
            header = self.headers.get("Authorization", "")
            scheme, sep, provided = header.partition(" ")
            if sep != " " or scheme.lower() != "bearer":
                self._send_json(HTTPStatus.UNAUTHORIZED, {"detail": "Unauthorized."})
                return False
            if not hmac.compare_digest(provided.strip(), state.token):
                self._send_json(HTTPStatus.UNAUTHORIZED, {"detail": "Unauthorized."})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": bool(state.cli_bin),
                        "cli_found": bool(state.cli_bin),
                        "auth_required": bool(state.token),
                    },
                )
                return

            if parsed.path == "/v1/models":
                if not self._check_auth():
                    return
                query = parse_qs(parsed.query)
                refresh = query.get("refresh", ["0"])[0] in {"1", "true", "yes"}
                try:
                    models = state.get_models(refresh=refresh)
                except FileNotFoundError as exc:
                    self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"detail": str(exc)})
                    return
                except subprocess.TimeoutExpired:
                    self._send_json(HTTPStatus.GATEWAY_TIMEOUT, {"detail": "agy models timed out."})
                    return
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"detail": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"models": models})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Not found."})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path != "/v1/complete":
                self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Not found."})
                return
            if not self._check_auth():
                return
            if not state.cli_bin:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"detail": "Antigravity CLI ('agy') not found on host."},
                )
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"detail": "Invalid Content-Length."})
                return
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                self._send_json(HTTPStatus.BAD_REQUEST, {"detail": "Invalid request size."})
                return

            raw_body = self.rfile.read(content_length)
            try:
                req = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"detail": "Invalid JSON body."})
                return

            if not isinstance(req, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"detail": "JSON body must be an object."})
                return

            model = req.get("model")
            prompt = req.get("prompt")
            json_schema = req.get("json_schema")
            raw_timeout = req.get("timeout_s", 30.0)
            if not isinstance(model, str) or not model.strip():
                self._send_json(HTTPStatus.BAD_REQUEST, {"detail": "model is required."})
                return
            if not isinstance(prompt, str) or not prompt.strip():
                self._send_json(HTTPStatus.BAD_REQUEST, {"detail": "prompt is required."})
                return
            if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
                timeout_s = 30.0
            else:
                timeout_s = max(1.0, min(120.0, float(raw_timeout)))

            cmd = [
                state.cli_bin,
                "--disable-slash-commands",
                "--output-format",
                "json",
                "--model",
                model.strip(),
            ]
            if isinstance(json_schema, dict):
                cmd.extend(["--json-schema", json.dumps(json_schema, separators=(",", ":"))])
            cmd.extend(["--print", prompt])

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_s,
                )
            except subprocess.TimeoutExpired:
                self._send_json(
                    HTTPStatus.GATEWAY_TIMEOUT,
                    {"detail": "Antigravity CLI timed out."},
                )
                return
            except OSError as exc:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"detail": f"Failed to execute agy: {exc}"},
                )
                return

            cli_payload = _parse_cli_json_output(proc.stdout or "")
            if proc.returncode != 0 or (
                cli_payload is not None and cli_payload.get("status") != "SUCCESS"
            ):
                err_msg = ""
                if cli_payload and isinstance(cli_payload.get("error"), str):
                    err_msg = cli_payload["error"]
                elif proc.stderr:
                    err_msg = proc.stderr.strip()
                lowered = err_msg.lower()
                if (
                    "not logged into antigravity" in lowered
                    or "invalid model selection" in lowered
                    or "is not recognized as a known model" in lowered
                ):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"detail": err_msg or "Antigravity CLI rejected the request."},
                    )
                    return
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"detail": err_msg or f"agy exited with status {proc.returncode}."},
                )
                return

            if cli_payload is None:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"detail": "Antigravity CLI returned empty or non-JSON output."},
                )
                return

            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "SUCCESS",
                    "response": cli_payload.get("response"),
                    "structured_output": cli_payload.get("structured_output"),
                },
            )

    return AntigravityBridgeHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="ClipForge host bridge for local Antigravity CLI.")
    parser.add_argument("--host", default=os.environ.get("ANTIGRAVITY_BRIDGE_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ANTIGRAVITY_BRIDGE_PORT", str(DEFAULT_PORT))),
    )
    parser.add_argument("--cli-bin", default="")
    parser.add_argument("--token", default=os.environ.get("ANTIGRAVITY_BRIDGE_TOKEN", ""))
    args = parser.parse_args()

    cli_bin = resolve_agy_binary(args.cli_bin)
    state = BridgeState(cli_bin=cli_bin, token=args.token)
    class _BridgeServer(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = _BridgeServer((args.host, args.port), make_handler(state))
    print(f"Antigravity bridge listening on http://{args.host}:{args.port} (cli={cli_bin})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
