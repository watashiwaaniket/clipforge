from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ApiKey, User


def test_downgrade_removes_only_acp_credentials(clean_db) -> None:
    config = Config("alembic.ini")
    with SessionLocal.begin() as session:
        for provider in ("codex", "claude", "openai"):
            user = User(email=f"{provider}@example.com", password_hash="unused")
            user.api_key = ApiKey(
                provider=provider, encrypted_key=b"fixture", base_url="", model=""
            )
            session.add(user)

    try:
        command.downgrade(config, "-1")
        with SessionLocal() as session:
            assert session.scalars(select(ApiKey.provider)).all() == ["openai"]
            assert len(session.scalars(select(User.id)).all()) == 3
    finally:
        command.upgrade(config, "head")
