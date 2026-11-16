import base64
import hashlib
import hmac
import json
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import ApiKey, Job, User
from app.security import encrypt_api_key, hash_password


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_token(user_id: UUID) -> str:
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "HS256"}, separators=(",", ":")).encode("utf-8"))
    payload = _b64url(
        json.dumps(
            {
                "sub": str(user_id),
                "iss": "clipforge",
                "aud": "clipforge-api",
                "iat": now,
                "exp": now + 3600,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    sig = hmac.new(
        settings.jwt_secret.encode("utf-8"),
        f"{header}.{payload}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def _create_user_with_key(email: str) -> tuple[dict[str, str], UUID]:
    with SessionLocal() as session:
        user = User(email=email, password_hash=hash_password("correct-horse-battery"))
        session.add(user)
        session.flush()
        session.add(
            ApiKey(
                user_id=user.id,
                provider="openai",
                encrypted_key=encrypt_api_key("sk-test-" + "0123456789abcdef"),
                base_url="https://api.openai.com/v1",
                model="gpt-4o-mini",
            )
        )
        session.commit()
        user_id = user.id
    return {"Authorization": f"Bearer {_sign_token(user_id)}"}, user_id


@pytest.fixture
def client(clean_db: None) -> Generator[TestClient, None, None]:
    with TestClient(app) as tc:
        yield tc


def test_post_jobs_enqueues_uuid_without_running_ytdlp(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, user_id = _create_user_with_key("jobs@example.com")
    monkeypatch.setattr("app.api.jobs.has_enough_free_disk", lambda: True)

    def forbid_subprocess(*args, **kwargs):
        raise AssertionError("subprocess.run must never be called on POST /api/v1/jobs")

    monkeypatch.setattr("app.services.downloader.subprocess.run", forbid_subprocess)

    delayed_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_delay(*args, **kwargs):
        delayed_calls.append((args, kwargs))

    monkeypatch.setattr("app.api.jobs.process_job.delay", fake_delay)

    res = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert res.status_code == 202
    job_id = UUID(res.json()["job_id"])

    assert delayed_calls == [((str(job_id),), {})]

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.user_id == user_id
        assert job.platform == "youtube"
        assert job.status == "queued"
        assert job.enqueued_at is not None


def test_post_jobs_validation_caps_and_failures(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.api.jobs.has_enough_free_disk", lambda: True)
    monkeypatch.setattr("app.api.jobs.process_job.delay", lambda *_args: None)

    # Missing key -> 400
    with SessionLocal() as session:
        no_key_user = User(email="nokey@example.com", password_hash=hash_password("correct-horse"))
        session.add(no_key_user)
        session.commit()
        no_key_id = no_key_user.id
    no_key_headers = {"Authorization": f"Bearer {_sign_token(no_key_id)}"}

    missing_key = client.post(
        "/api/v1/jobs",
        headers=no_key_headers,
        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"] == "Add an API key before generating clips."

    headers, user_id = _create_user_with_key("limits@example.com")

    # Unsupported URL -> 400 and no row inserted
    bad_url = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"url": "https://www.youtube.com/embed/dQw4w9WgXcQ"},
    )
    assert bad_url.status_code == 400
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0

    # Body over 2 KB -> 422
    oversized = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "pad": "x" * 2100},
    )
    assert oversized.status_code == 422

    # 3 concurrent jobs succeed; 4th returns 429
    for idx in range(3):
        ok = client.post(
            "/api/v1/jobs",
            headers=headers,
            json={"url": f"https://www.twitch.tv/videos/{1000 + idx}"},
        )
        assert ok.status_code == 202

    fourth = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"url": "https://www.twitch.tv/videos/9999"},
    )
    assert fourth.status_code == 429
    assert "3 jobs generating" in fourth.json()["detail"]

    # Mark existing jobs completed, fill up to 10/day, 11th returns 429
    with SessionLocal() as session:
        for row in session.scalars(select(Job).where(Job.user_id == user_id)).all():
            row.status = "completed"
        for idx in range(7):
            session.add(
                Job(
                    user_id=user_id,
                    source_url=f"https://www.twitch.tv/videos/{2000 + idx}",
                    platform="twitch",
                    status="completed",
                    progress_message="Ready · 1 clips",
                )
            )
        session.commit()

    eleventh = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"url": "https://www.twitch.tv/videos/8888"},
    )
    assert eleventh.status_code == 429
    assert "Daily limit reached (10 jobs)" in eleventh.json()["detail"]


def test_post_jobs_enqueue_error_marks_row_failed_and_returns_503(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, user_id = _create_user_with_key("brokerdown@example.com")
    monkeypatch.setattr("app.api.jobs.has_enough_free_disk", lambda: True)

    def fail_delay(_job_id: str):
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.api.jobs.process_job.delay", fail_delay)

    res = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"url": "https://www.twitch.tv/videos/123456"},
    )
    assert res.status_code == 503
    assert res.json()["detail"] == "Could not start the job. Try again in a moment."

    with SessionLocal() as session:
        row = session.scalar(select(Job).where(Job.user_id == user_id))
        assert row is not None
        assert row.status == "failed"
        assert row.error == "Could not start the job. Try again in a moment."
        assert row.enqueued_at is None


def test_parallel_job_posts_at_cap_insert_only_one_row(
    clean_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, user_id = _create_user_with_key("race@example.com")
    monkeypatch.setattr("app.api.jobs.has_enough_free_disk", lambda: True)
    monkeypatch.setattr("app.api.jobs.process_job.delay", lambda *_args: None)

    with SessionLocal() as session:
        for idx in range(2):
            session.add(
                Job(
                    user_id=user_id,
                    source_url=f"https://www.twitch.tv/videos/{500 + idx}",
                    platform="twitch",
                    status="queued",
                    progress_message="Queued",
                )
            )
        session.commit()

    def submit_one(idx: int) -> int:
        with TestClient(app) as tc:
            resp = tc.post(
                "/api/v1/jobs",
                headers=headers,
                json={"url": f"https://www.twitch.tv/videos/{600 + idx}"},
            )
            return resp.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit_one, [1, 2]))

    assert sorted(statuses) == [202, 429]
    with SessionLocal() as session:
        total = session.scalar(select(func.count()).select_from(Job).where(Job.user_id == user_id))
        assert total == 3


def test_list_and_detail_omit_file_path_and_isolate_users(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers_a, user_a = _create_user_with_key("usera@example.com")
    headers_b, _user_b = _create_user_with_key("userb@example.com")
    monkeypatch.setattr("app.api.jobs.has_enough_free_disk", lambda: True)
    monkeypatch.setattr("app.api.jobs.process_job.delay", lambda *_args: None)

    create_res = client.post(
        "/api/v1/jobs",
        headers=headers_a,
        json={"url": "https://www.twitch.tv/videos/123456"},
    )
    assert create_res.status_code == 202
    job_id = create_res.json()["job_id"]

    list_res = client.get("/api/v1/jobs", headers=headers_a)
    assert list_res.status_code == 200
    items = list_res.json()
    assert len(items) == 1
    assert set(items[0].keys()) == {
        "id",
        "source_url",
        "status",
        "progress_message",
        "error",
        "created_at",
    }
    assert "file_path" not in list_res.text

    detail_res = client.get(f"/api/v1/jobs/{job_id}", headers=headers_a)
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert detail["id"] == job_id
    assert detail["platform"] == "twitch"
    assert detail["clips"] == []
    assert "file_path" not in detail_res.text

    # Other user sees 404, not 403
    other_detail = client.get(f"/api/v1/jobs/{job_id}", headers=headers_b)
    assert other_detail.status_code == 404
    assert other_detail.json()["detail"] == "Not found."
    assert client.get("/api/v1/jobs", headers=headers_b).json() == []
