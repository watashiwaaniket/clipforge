import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user
from app.logging import secret_boundary, secret_scope
from app.models import User
from app.schemas import (
    AntigravityModelsProbeRequest,
    ProviderCatalogResponse,
    ProviderInfoResponse,
    ProviderModelResponse,
    ProviderModelsResponse,
)
from app.services.providers import (
    ANTIGRAVITY_PROVIDER,
    ProviderConfigError,
    is_local_antigravity_sentinel,
)
from app.services.providers.catalog import (
    ProviderCatalogEntry,
    get_provider_catalog_entry,
    list_provider_catalog,
)

router = APIRouter(prefix="/api/v1/providers", tags=["providers"])


def _to_provider_info(entry: ProviderCatalogEntry) -> ProviderInfoResponse:
    return ProviderInfoResponse(
        id=entry.id,
        name=entry.name,
        requires_api_key=entry.requires_api_key,
        locked_base_url=entry.locked_base_url,
        requires_custom_config=entry.requires_custom_config,
        default_base_url=entry.default_base_url,
        default_model=entry.default_model,
        connected=entry.connected,
        model_source=entry.model_source,
        models=[ProviderModelResponse(id=m.id, name=m.name) for m in entry.models],
    )


def _to_models_response(entry: ProviderCatalogEntry) -> ProviderModelsResponse:
    return ProviderModelsResponse(
        provider=entry.id,
        connected=entry.connected,
        model_source=entry.model_source,
        default_model=entry.default_model,
        models=[ProviderModelResponse(id=m.id, name=m.name) for m in entry.models],
    )


@router.get("", response_model=ProviderCatalogResponse)
def list_providers(
    _user: User = Depends(get_current_user),
) -> ProviderCatalogResponse:
    with secret_boundary():
        entries = list_provider_catalog(http_client_factory=httpx.Client)
    return ProviderCatalogResponse(providers=[_to_provider_info(entry) for entry in entries])


@router.get("/{provider}/models", response_model=ProviderModelsResponse)
def get_provider_models(
    provider: str,
    _user: User = Depends(get_current_user),
) -> ProviderModelsResponse:
    try:
        with secret_boundary():
            entry = get_provider_catalog_entry(provider, http_client_factory=httpx.Client)
    except ProviderConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return _to_models_response(entry)


@router.post("/antigravity/models", response_model=ProviderModelsResponse)
def probe_antigravity_models(
    body: AntigravityModelsProbeRequest,
    _user: User = Depends(get_current_user),
) -> ProviderModelsResponse:
    raw = body.api_key.get_secret_value().strip() if body.api_key is not None else ""
    is_local = is_local_antigravity_sentinel(raw)
    credential = "local" if is_local else raw
    scope_ctx = secret_boundary() if is_local else secret_scope(credential)
    with scope_ctx:
        entry = get_provider_catalog_entry(
            ANTIGRAVITY_PROVIDER,
            credential=credential,
            http_client_factory=httpx.Client,
        )
    return _to_models_response(entry)
