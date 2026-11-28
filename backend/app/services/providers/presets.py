from dataclasses import dataclass

from app.services.providers.base import ResolvedProviderConfig
from app.services.providers.errors import ProviderConfigError
from app.services.providers.url_policy import (
    DEFAULT_HTTPS_POLICY,
    OLLAMA_LOCAL_HTTP_POLICY,
    OutboundUrlPolicy,
    validate_outbound_url,
)


@dataclass(frozen=True)
class ProviderPreset:
    default_base_url: str | None
    default_model: str | None
    locked_base_url: bool
    requires_custom_config: bool = False
    url_policy: OutboundUrlPolicy = DEFAULT_HTTPS_POLICY


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    **{
        agent: ProviderPreset(
            default_base_url=f"http://host.docker.internal:11437/{agent}/v1",
            default_model="default",
            locked_base_url=True,
            url_policy=OLLAMA_LOCAL_HTTP_POLICY,
        )
        for agent in ("codex", "claude")
    },
    "openai": ProviderPreset(
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        locked_base_url=True,
    ),
    "groq": ProviderPreset(
        default_base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        locked_base_url=True,
    ),
    "together": ProviderPreset(
        default_base_url="https://api.together.xyz/v1",
        default_model="meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        locked_base_url=True,
    ),
    "ollama": ProviderPreset(
        default_base_url="http://host.docker.internal:11434/v1",
        default_model="llama3.1",
        locked_base_url=False,
        url_policy=OLLAMA_LOCAL_HTTP_POLICY,
    ),
    "compatible": ProviderPreset(
        default_base_url=None,
        default_model=None,
        locked_base_url=False,
        requires_custom_config=True,
    ),
}


def get_provider_preset(provider: str) -> ProviderPreset | None:
    return PROVIDER_PRESETS.get(provider)


def check_outbound_url(base_url: str, *, provider: str = "compatible") -> str:
    preset = get_provider_preset(provider)
    policy = preset.url_policy if preset is not None else DEFAULT_HTTPS_POLICY
    return validate_outbound_url(base_url, policy=policy)


def resolve_provider_config(
    provider: str,
    base_url: str | None,
    model: str | None,
) -> ResolvedProviderConfig:
    if provider == "antigravity":
        from app.services.providers.antigravity import resolve_antigravity_config

        return resolve_antigravity_config(base_url, model)

    preset = get_provider_preset(provider)
    if preset is None:
        raise ProviderConfigError("Unsupported provider.")

    clean_base_url = base_url.strip() if base_url is not None else None
    if clean_base_url == "":
        clean_base_url = None

    clean_model = model.strip() if model is not None else None
    if clean_model == "":
        clean_model = None

    if preset.locked_base_url and clean_base_url is not None:
        raise ProviderConfigError("That provider uses a fixed base URL.")

    if preset.requires_custom_config and (clean_base_url is None or clean_model is None):
        raise ProviderConfigError(f'Provider "{provider}" needs a base URL and a model.')

    resolved_base_url = clean_base_url if clean_base_url is not None else preset.default_base_url
    resolved_model = clean_model if clean_model is not None else preset.default_model

    if not resolved_base_url or not resolved_model:
        raise ProviderConfigError("Provider base URL and model are required.")

    validated_url = validate_outbound_url(resolved_base_url, policy=preset.url_policy)
    return ResolvedProviderConfig(
        provider=provider,
        base_url=validated_url,
        model=resolved_model,
    )
