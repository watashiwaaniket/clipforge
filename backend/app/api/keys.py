import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from openai import OpenAI
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db import get_session
from app.logging import secret_scope
from app.models import ApiKey, User
from app.schemas import (
    ApiKeyMetadataResponse,
    ApiKeyUpsertRequest,
    ApiKeyUpsertResponse,
)
from app.security import encrypt_api_key
from app.services.providers import (
    ProviderConfigError,
    check_outbound_url,
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
    check_outbound_url(base_url, provider=provider)
    with httpx.Client(follow_redirects=False, timeout=VALIDATION_TIMEOUT) as http_client:
        client = OpenAI(
            api_key=plaintext_key,
            base_url=base_url,
            http_client=http_client,
            max_retries=0,
        )
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )


@router.post("", response_model=ApiKeyUpsertResponse)
def upsert_api_key(
    body: ApiKeyUpsertRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ApiKeyUpsertResponse:
    plaintext_key = body.api_key.get_secret_value().strip()
    if not plaintext_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="API key cannot be empty.",
        )

    with secret_scope(plaintext_key):
        try:
            resolved = resolve_provider_config(body.provider, body.base_url, body.model)
        except ProviderConfigError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

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

        session.execute(delete(ApiKey).where(ApiKey.user_id == user.id))
        session.add(
            ApiKey(
                user_id=user.id,
                provider=resolved.provider,
                encrypted_key=encrypted,
                base_url=resolved.base_url,
                model=resolved.model,
            )
        )
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
