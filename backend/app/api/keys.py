from uuid import uuid4

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from openai.types.chat import ChatCompletion
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db import get_session
from app.logging import secret_boundary, secret_scope
from app.models import ApiKey, User
from app.schemas import (
    ApiKeyMetadataResponse,
    ApiKeyUpsertRequest,
    ApiKeyUpsertResponse,
)
from app.security import encrypt_api_key
from app.services.providers import (
    ANTIGRAVITY_PROVIDER,
    ProviderConfigError,
    ResolvedProviderConfig,
    build_antigravity_provider,
    build_openai_client,
    is_local_antigravity_sentinel,
    pick_antigravity_default_model,
    resolve_provider_config,
    strip_url_userinfo,
)

router = APIRouter(prefix="/api/v1/keys", tags=["keys"])
log = structlog.get_logger()

VALIDATION_TIMEOUT = httpx.Timeout(8.0, connect=5.0)


def _validate_provider_key(
    *,
    provider: str,
    base_url: str,
    model: str,
    plaintext_key: str,
) -> None:
    if provider == ANTIGRAVITY_PROVIDER:
        antigravity_provider = build_antigravity_provider(
            credential=plaintext_key,
            base_url=base_url,
            model=model,
            timeout=VALIDATION_TIMEOUT,
            http_client_factory=httpx.Client,
        )
        with antigravity_provider:
            antigravity_provider.validate_credentials()
        return

    http_client, client = build_openai_client(
        api_key=plaintext_key,
        base_url=base_url,
        provider=provider,
        timeout=VALIDATION_TIMEOUT,
        http_client_factory=httpx.Client,
    )
    with http_client:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        if not isinstance(response, ChatCompletion) or not response.choices:
            raise ValueError("Provider returned an unexpected completion payload.")


@router.post("", response_model=ApiKeyUpsertResponse)
def upsert_api_key(
    body: ApiKeyUpsertRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ApiKeyUpsertResponse:
    raw_key = body.api_key.get_secret_value().strip() if body.api_key is not None else ""
    if body.provider != ANTIGRAVITY_PROVIDER and not raw_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="API key cannot be empty.",
        )

    is_local_antigravity = body.provider == ANTIGRAVITY_PROVIDER and is_local_antigravity_sentinel(
        raw_key
    )
    plaintext_key = "local" if is_local_antigravity else raw_key
    scope_ctx = secret_boundary() if is_local_antigravity else secret_scope(plaintext_key)

    with scope_ctx:
        try:
            resolved = resolve_provider_config(body.provider, body.base_url, body.model)
        except ProviderConfigError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        if body.provider == ANTIGRAVITY_PROVIDER and (
            body.model is None or not body.model.strip()
        ):
            resolved = ResolvedProviderConfig(
                provider=resolved.provider,
                base_url=resolved.base_url,
                model=pick_antigravity_default_model(
                    credential=plaintext_key,
                    http_client_factory=httpx.Client,
                ),
            )

        try:
            _validate_provider_key(
                provider=resolved.provider,
                base_url=resolved.base_url,
                model=resolved.model,
                plaintext_key=plaintext_key,
            )
        except ProviderConfigError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            log.warning(
                "key_validation_failed",
                user_id=str(user.id),
                provider=resolved.provider,
                exc_info=exc,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That key was rejected by the provider.",
            ) from exc

        encrypted = encrypt_api_key(plaintext_key)
        del plaintext_key

        stmt = (
            pg_insert(ApiKey)
            .values(
                id=uuid4(),
                user_id=user.id,
                provider=resolved.provider,
                encrypted_key=encrypted,
                base_url=resolved.base_url,
                model=resolved.model,
            )
            .on_conflict_do_update(
                index_elements=[ApiKey.user_id],
                set_={
                    "provider": resolved.provider,
                    "encrypted_key": encrypted,
                    "base_url": resolved.base_url,
                    "model": resolved.model,
                    "updated_at": func.now(),
                },
            )
        )
        session.execute(stmt)
        session.flush()

        return ApiKeyUpsertResponse(
            configured=True,
            provider=resolved.provider,
            model=resolved.model,
        )


@router.get(
    "",
    response_model=ApiKeyMetadataResponse,
    response_model_exclude_none=True,
)
def get_api_key_metadata(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ApiKeyMetadataResponse:
    row = session.scalar(select(ApiKey).where(ApiKey.user_id == user.id))
    if row is None:
        return ApiKeyMetadataResponse(configured=False)
    return ApiKeyMetadataResponse(
        configured=True,
        provider=row.provider,
        model=row.model,
        base_url=strip_url_userinfo(row.base_url),
    )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete_api_key(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    session.execute(delete(ApiKey).where(ApiKey.user_id == user.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
