import os
import socket
from collections.abc import Generator
from pathlib import Path

from cryptography.fernet import Fernet

# Set test configuration BEFORE importing any app module.
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret-must-be-at-least-32-bytes-long"
os.environ["DATABASE_URL"] = "postgresql+psycopg://clipforge:clipforge@db:5432/clipforge_test"
os.environ["REDIS_URL"] = "redis://redis:6379/1"

import psycopg  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402


def _ensure_db_resolvable() -> None:
    try:
        socket.getaddrinfo("db", 5432)
    except socket.gaierror as exc:
        raise RuntimeError("run: docker compose exec -T api pytest") from exc


_ensure_db_resolvable()


@pytest.fixture(scope="session", autouse=True)
def setup_test_database() -> Generator[None, None, None]:
    maintenance_dsn = "postgresql://clipforge:clipforge@db:5432/clipforge"
    with psycopg.connect(maintenance_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = 'clipforge_test'")
            if cur.fetchone() is None:
                cur.execute("CREATE DATABASE clipforge_test")

    alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini))
    alembic_cfg.set_main_option("script_location", str(alembic_ini.parent / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
    command.upgrade(alembic_cfg, "head")
    yield


@pytest.fixture
def clean_db() -> Generator[None, None, None]:
    with SessionLocal() as session:
        session.execute(text("TRUNCATE TABLE clips, jobs, api_keys, users CASCADE"))
        session.commit()
    yield
    with SessionLocal() as session:
        session.execute(text("TRUNCATE TABLE clips, jobs, api_keys, users CASCADE"))
        session.commit()
