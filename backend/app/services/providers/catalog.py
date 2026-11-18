from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.services.providers.antigravity import (
    ANTIGRAVITY_DEFAULT_BASE_URL,
    ANTIGRAVITY_DEFAULT_MODEL,
    ANTIGRAVITY_PROVIDER,
    list_antigravity_models,
)
from app.services.providers.base import ProviderModelInfo
from app.services.providers.errors import ProviderConfigError
from app.services.providers.presets import PROVIDER_PRESETS

_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "openai": "OpenAI",
    "groq": "Groq",
    "together": "Together",
    "ollama": "Ollama",
    "compatible": "OpenAI-Compatible",
    "antigravity": "Antigravity",
}

_STATIC_PROVIDER_MODELS: dict[str, tuple[ProviderModelInfo, ...]] = {
    "openai": (
        ProviderModelInfo(id="gpt-4o-mini", name="GPT-4o mini"),
        ProviderModelInfo(id="gpt-4o", name="GPT-4o"),
        ProviderModelInfo(id="gpt-4.1-mini", name="GPT-4.1 mini"),
        ProviderModelInfo(id="gpt-4.1", name="GPT-4.1"),
    ),
    "groq": (
        ProviderModelInfo(id="llama-3.3-70b-versatile", name="Llama 3.3 70B Versatile"),
        ProviderModelInfo(id="llama-3.1-8b-instant", name="Llama 3.1 8B Instant"),
        ProviderModelInfo(id="mixtral-8x7b-32768", name="Mixtral 8x7B"),
    ),
    "together": (
        ProviderModelInfo(
            id="meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            name="Llama 3.1 70B Instruct Turbo",
        ),
        ProviderModelInfo(
            id="meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
            name="Llama 3.1 8B Instruct Turbo",
        ),
        ProviderModelInfo(
            id="Qwen/Qwen2.5-72B-Instruct-Turbo",
            name="Qwen 2.5 72B Instruct Turbo",
        ),
    ),
    "ollama": (
        ProviderModelInfo(id="llama3.1", name="Llama 3.1"),
        ProviderModelInfo(id="llama3.2", name="Llama 3.2"),
        ProviderModelInfo(id="qwen2.5", name="Qwen 2.5"),
    ),
    "compatible": (),
}

_PROVIDER_ORDER: tuple[str, ...] = (
    "openai",
    "groq",
    "together",
    "ollama",
    "compatible",
    "antigravity",
)


@dataclass(frozen=True)
class ProviderCatalogEntry:
    id: str
    name: str
    requires_api_key: bool
    locked_base_url: bool
    requires_custom_config: bool
    default_base_url: str | None
    default_model: str | None
    connected: bool
    model_source: str
    models: list[ProviderModelInfo]


def _probe_ollama_models(
    client_factory: Callable[..., httpx.Client],
) -> tuple[list[ProviderModelInfo], str, bool]:
    preset = PROVIDER_PRESETS["ollama"]
    assert preset.default_base_url is not None
    tags_url = preset.default_base_url.removesuffix("/v1") + "/api/tags"
    try:
        with client_factory(
            follow_redirects=False, timeout=httpx.Timeout(1.0, connect=0.5)
        ) as client:
            resp = client.get(tags_url)
            if resp.status_code == 200:
                payload = resp.json()
                raw_models = payload.get("models") if isinstance(payload, dict) else None
                if isinstance(raw_models, list):
                    found: list[ProviderModelInfo] = []
                    seen: set[str] = set()
                    for item in raw_models:
                        if not isinstance(item, dict):
                            continue
                        name = item.get("name")
                        if isinstance(name, str) and name.strip() and name.strip() not in seen:
                            clean = name.strip()
                            seen.add(clean)
                            found.append(ProviderModelInfo(id=clean, name=clean))
                    if found:
                        return found, "ollama_live", True
    except (httpx.HTTPError, ValueError):
        pass
    return list(_STATIC_PROVIDER_MODELS["ollama"]), "static", False


def _choose_antigravity_default(models: list[ProviderModelInfo], connected: bool) -> str:
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


def get_provider_catalog_entry(
    provider: str,
    *,
    credential: str | None = None,
    http_client_factory: Callable[..., httpx.Client] | None = None,
) -> ProviderCatalogEntry:
    factory = http_client_factory or httpx.Client

    if provider == ANTIGRAVITY_PROVIDER:
        models, source, connected = list_antigravity_models(
            credential=credential,
            http_client_factory=factory,
        )
        default_model = _choose_antigravity_default(models, connected)
        return ProviderCatalogEntry(
            id=ANTIGRAVITY_PROVIDER,
            name=_PROVIDER_DISPLAY_NAMES[ANTIGRAVITY_PROVIDER],
            requires_api_key=False,
            locked_base_url=True,
            requires_custom_config=False,
            default_base_url=ANTIGRAVITY_DEFAULT_BASE_URL,
            default_model=default_model,
            connected=connected,
            model_source=source,
            models=models,
        )

    preset = PROVIDER_PRESETS.get(provider)
    if preset is None:
        raise ProviderConfigError("Unsupported provider.")

    if provider == "ollama":
        models, source, connected = _probe_ollama_models(factory)
        default_model = models[0].id if connected and models else preset.default_model
    else:
        models = list(_STATIC_PROVIDER_MODELS.get(provider, ()))
        source = "static"
        connected = False
        default_model = preset.default_model

    return ProviderCatalogEntry(
        id=provider,
        name=_PROVIDER_DISPLAY_NAMES.get(provider, provider.title()),
        requires_api_key=True,
        locked_base_url=preset.locked_base_url,
        requires_custom_config=preset.requires_custom_config,
        default_base_url=preset.default_base_url,
        default_model=default_model,
        connected=connected,
        model_source=source,
        models=models,
    )


def list_provider_catalog(
    *,
    http_client_factory: Callable[..., httpx.Client] | None = None,
) -> list[ProviderCatalogEntry]:
    return [
        get_provider_catalog_entry(provider_id, http_client_factory=http_client_factory)
        for provider_id in _PROVIDER_ORDER
    ]
