import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

import structlog

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "encrypted_key",
        "openai_api_key",
        "password",
        "jwt",
        "access_token",
        "encryption_key",
        "jwt_secret",
        "secret",
    }
)

_TOKEN_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"gAAAA[A-Za-z0-9_=-]{8,}"), "[REDACTED]"),
)

_secrets_var: ContextVar[tuple[str, ...]] = ContextVar("clipforge_secrets", default=())


def push_secret(secret: str) -> Token[tuple[str, ...]] | None:
    if not isinstance(secret, str) or len(secret) < 8:
        return None
    current = _secrets_var.get()
    if secret in current:
        return None
    return _secrets_var.set((*current, secret))


@contextmanager
def secret_scope(secret: str) -> Iterator[None]:
    token = push_secret(secret)
    try:
        yield
    finally:
        if token is not None:
            _secrets_var.reset(token)


def redact(text: str) -> str:
    result = text
    secrets = sorted(_secrets_var.get(), key=len, reverse=True)
    for secret in secrets:
        if secret and secret in result:
            result = result.replace(secret, "[REDACTED]")
    for pattern, replacement in _TOKEN_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        sanitized: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS:
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = _redact_value(v)
        return sanitized
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def redact_processor(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    return _redact_value(event_dict)


def configure_logging() -> None:
    logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stdout)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
