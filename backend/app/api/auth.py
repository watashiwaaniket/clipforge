import redis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.models import User
from app.schemas import AuthRequest, UserResponse
from app.security import hash_password, validate_email, verify_dummy_password, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

AUTH_RATE_LIMIT_MAX = 10
AUTH_RATE_LIMIT_WINDOW_S = 60

_RATE_LIMIT_LUA = """
local count = redis.call("INCR", KEYS[1])
if count == 1 then
    redis.call("EXPIRE", KEYS[1], tonumber(ARGV[1]))
end
return count
"""


def resolve_client_ip(request: Request) -> str:
    peer = request.client.host if request.client and request.client.host else "unknown"
    trust_count = settings.trust_proxy_count
    if trust_count <= 0:
        return peer
    forwarded_values = request.headers.getlist("x-forwarded-for")
    if not forwarded_values:
        return peer
    hops = [
        part.strip()
        for header_value in forwarded_values
        for part in header_value.split(",")
        if part.strip()
    ]
    if len(hops) < trust_count:
        return peer
    return hops[-trust_count]


def _enforce_auth_rate_limit(request: Request) -> None:
    ip = resolve_client_ip(request)
    key = f"rl:auth:{ip}"
    client = redis.Redis.from_url(settings.redis_url)
    try:
        count = int(client.eval(_RATE_LIMIT_LUA, 1, key, AUTH_RATE_LIMIT_WINDOW_S))
    finally:
        client.close()
    if count > AUTH_RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many sign-in attempts. Wait a minute.",
        )


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
def register(
    body: AuthRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> UserResponse:
    _enforce_auth_rate_limit(request)

    try:
        email = validate_email(body.email)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email address.",
        ) from exc

    if len(body.password) < 10:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 10 characters.",
        )

    existing = session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email or password.",
        )

    user = User(email=email, password_hash=hash_password(body.password))
    session.add(user)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email or password.",
        ) from exc

    return UserResponse(id=user.id, email=user.email)


@router.post("/login", response_model=UserResponse)
def login(
    body: AuthRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> UserResponse:
    _enforce_auth_rate_limit(request)

    try:
        email = validate_email(body.email)
    except ValueError as exc:
        verify_dummy_password(body.password)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email or password.",
        ) from exc

    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        verify_dummy_password(body.password)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email or password.",
        )

    if not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email or password.",
        )

    return UserResponse(id=user.id, email=user.email)
