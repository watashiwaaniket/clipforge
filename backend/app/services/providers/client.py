from collections.abc import Callable

import httpx
from openai import OpenAI

from app.services.providers.presets import check_outbound_url


def build_openai_client(
    *,
    api_key: str,
    base_url: str,
    provider: str = "compatible",
    timeout: httpx.Timeout,
    http_client: httpx.Client | None = None,
    http_client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> tuple[httpx.Client, OpenAI]:
    validated_url = check_outbound_url(base_url, provider=provider)
    resolved_http_client = http_client or http_client_factory(
        follow_redirects=False, timeout=timeout
    )
    client = OpenAI(
        api_key=api_key,
        base_url=validated_url,
        http_client=resolved_http_client,
        max_retries=0,
    )
    return resolved_http_client, client
