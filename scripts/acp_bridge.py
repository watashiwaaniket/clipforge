"""Loopback scoring bridge for the operator's signed-in Codex and Claude ACP agents."""

import hmac
import json
import logging
import os
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if not hmac.compare_digest(
            self.headers.get("Authorization", "").encode(),
            ("Bearer " + os.environ["ACP_BRIDGE_TOKEN"]).encode(),
        ):
            self.reply(401, {"error": "Invalid bridge token"})
            return
        routes = {f"/{agent}/v1/chat/completions": agent for agent in ("codex", "claude")}
        agent = routes.get(self.path)
        if not agent:
            self.reply(404, {"error": "Unknown route"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 2_000_000:
                raise ValueError
            body = json.loads(self.rfile.read(size))
            prompt = "\n\n".join(m["role"] + ": " + m["content"] for m in body["messages"])
            model = body["model"]
            if not isinstance(model, str) or not model:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            self.reply(400, {"error": "Invalid completion request"})
            return

        with tempfile.TemporaryDirectory() as workspace:
            command = [
                "acpx",
                "--cwd",
                workspace,
                "--deny-all",
                "--no-fs",
                "--no-terminal",
                "--allowed-tools",
                "",
                "--format",
                "quiet",
                "--timeout",
                "90",
            ]
            if model != "default":
                command.extend(["--model", model])
            adapter = "codex-acp" if agent == "codex" else "claude-agent-acp"
            command.extend(["--agent", adapter, "exec", "--file", "-"])
            try:
                result = subprocess.run(
                    command,
                    cwd=workspace,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=100,
                    check=True,
                    env={k: v for k, v in os.environ.items() if k != "ACP_BRIDGE_TOKEN"},
                )
            except (OSError, subprocess.SubprocessError) as exc:
                # Agent output may echo credentials or the transcript. Log only known
                # diagnostic markers and process metadata.
                reason = type(exc).__name__
                if isinstance(exc, subprocess.CalledProcessError):
                    stderr = (exc.stderr or "").lower()
                    marker = next(
                        (
                            label
                            for label in (
                                "unauthorized",
                                "not authenticated",
                                "not found",
                                "permission denied",
                                "rate limit",
                            )
                            if label in stderr
                        ),
                        "agent exited",
                    )
                    reason = f"{marker} (exit {exc.returncode})"
                elif isinstance(exc, subprocess.TimeoutExpired):
                    reason = f"timed out after {exc.timeout}s"
                elif isinstance(exc, OSError):
                    reason = f"{reason} (errno {exc.errno})"
                logging.error("ACP %s failed: %s", agent, reason)
                self.reply(502, {"error": "ACP agent failed. Check acpx installation and sign-in."})
                return
        self.reply(
            200,
            {
                "model": model,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": result.stdout.strip()},
                    }
                ],
            },
        )


if __name__ == "__main__":
    if len(os.getenv("ACP_BRIDGE_TOKEN", "")) < 32:
        raise SystemExit(
            "Set ACP_BRIDGE_TOKEN to a private random token of at least 32 characters."
        )
    ThreadingHTTPServer(("127.0.0.1", 11437), Handler).serve_forever()
