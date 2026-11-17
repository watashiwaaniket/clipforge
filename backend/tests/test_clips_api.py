import base64
import hashlib
import hmac
import json
import time
from collections.abc import Generator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.clips import download_slug
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import Clip, Job, User
from app.security import hash_password

CLIP_BYTES = b"0123456789-fake-mp4-bytes"


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


def _create_user(email: str) -> tuple[dict[str, str], UUID]:
    with SessionLocal() as session:
        user = User(email=email, password_hash=hash_password("correct-horse-battery"))
        session.add(user)
        session.commit()
        user_id = user.id
    return {"Authorization": f"Bearer {_sign_token(user_id)}"}, user_id


def _create_job(user_id: UUID) -> UUID:
    with SessionLocal() as session:
        job = Job(
            user_id=user_id,
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            platform="youtube",
            status="completed",
            progress_message="Ready · 1 clips",
        )
        session.add(job)
        session.commit()
        return job.id


def _create_clip(
    storage: Path,
    job_id: UUID,
    *,
    title: str = "Heist goes wrong",
    status: str = "ready",
    score: int = 90,
    start_s: float = 10.0,
    write_file: bool = True,
    file_path: str | None = None,
) -> UUID:
    clip_id = uuid4()
    rel = f"jobs/{job_id}/clips/{clip_id}.mp4"
    if write_file and status == "ready":
        target = storage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(CLIP_BYTES)
    with SessionLocal() as session:
        session.add(
            Clip(
                id=clip_id,
                job_id=job_id,
                start_s=start_s,
                end_s=start_s + 30.0,
                score=score,
                title=title,
                hook="Hook.",
                score_source="llm",
                file_path=(file_path if file_path is not None else rel)
                if status == "ready"
                else None,
                caption_status="burned" if status == "ready" else "failed",
                status=status,
                error=None if status == "ready" else "This clip could not be exported.",
            )
        )
        session.commit()
    return clip_id


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(clean_db: None) -> Generator[TestClient, None, None]:
    with TestClient(app) as tc:
        yield tc


def test_owner_streams_clip_with_range(client: TestClient, storage: Path) -> None:
    headers, user_id = _create_user("owner@example.com")
    clip_id = _create_clip(storage, _create_job(user_id))

    res = client.get(f"/api/v1/clips/{clip_id}/video", headers=headers)
    assert res.status_code == 200
    assert res.content == CLIP_BYTES
    assert res.headers["content-type"] == "video/mp4"
    assert res.headers["content-disposition"] == 'inline; filename="clip.mp4"'
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["cache-control"] == "private, max-age=3600"
    assert res.headers["accept-ranges"] == "bytes"

    partial = client.get(
        f"/api/v1/clips/{clip_id}/video", headers={**headers, "Range": "bytes=0-3"}
    )
    assert partial.status_code == 206
    assert partial.content == CLIP_BYTES[:4]
    assert partial.headers["content-range"] == f"bytes 0-3/{len(CLIP_BYTES)}"
    assert partial.headers["content-length"] == "4"


def test_other_user_and_anonymous_cannot_stream(client: TestClient, storage: Path) -> None:
    _headers_a, user_a = _create_user("a@example.com")
    headers_b, _user_b = _create_user("b@example.com")
    clip_id = _create_clip(storage, _create_job(user_a))

    other = client.get(f"/api/v1/clips/{clip_id}/video", headers=headers_b)
    assert other.status_code == 404
    assert other.json()["detail"] == "Not found."
    assert client.get(f"/api/v1/clips/{clip_id}/video").status_code == 401
    assert client.get(f"/api/v1/clips/{uuid4()}/video", headers=headers_b).status_code == 404


def test_download_sets_attachment_with_safe_slug(client: TestClient, storage: Path) -> None:
    headers, user_id = _create_user("dl@example.com")
    job_id = _create_job(user_id)
    clip_id = _create_clip(storage, job_id, title="Heist goes wrong!")
    nasty_id = _create_clip(storage, job_id, title='evil"\r\nSet-Cookie: x=1')

    res = client.get(f"/api/v1/clips/{clip_id}/video?download=1", headers=headers)
    assert res.status_code == 200
    assert res.headers["content-disposition"] == (
        'attachment; filename="clipforge-Heist-goes-wrong.mp4"'
    )

    nasty = client.get(f"/api/v1/clips/{nasty_id}/video?download=1", headers=headers)
    assert nasty.status_code == 200
    disposition = nasty.headers["content-disposition"]
    assert disposition == 'attachment; filename="clipforge-evil-Set-Cookie-x-1.mp4"'
    assert "set-cookie" not in nasty.headers


def test_download_slug_rules() -> None:
    assert download_slug(None) == "clip"
    assert download_slug("") == "clip"
    assert download_slug("¡¡¡") == "clip"
    assert download_slug("GTA 6 — wanted level 5") == "GTA-6-wanted-level-5"
    assert len(download_slug("x" * 200)) == 60


def test_failed_missing_and_poisoned_clips_are_404(client: TestClient, storage: Path) -> None:
    headers, user_id = _create_user("edge@example.com")
    job_id = _create_job(user_id)
    failed_id = _create_clip(storage, job_id, status="failed")
    missing_id = _create_clip(storage, job_id, write_file=False)

    vod = storage / "jobs" / str(job_id) / "vod.mp4"
    vod.parent.mkdir(parents=True, exist_ok=True)
    vod.write_bytes(b"raw-vod")
    poisoned_id = _create_clip(storage, job_id, file_path=f"jobs/{job_id}/vod.mp4")
    escape_id = _create_clip(storage, job_id, file_path="../../etc/passwd")

    for clip_id in (failed_id, missing_id, poisoned_id, escape_id):
        res = client.get(f"/api/v1/clips/{clip_id}/video", headers=headers)
        assert res.status_code == 404, clip_id


def test_detail_returns_clip_objects_without_file_path(client: TestClient, storage: Path) -> None:
    headers, user_id = _create_user("detail@example.com")
    job_id = _create_job(user_id)
    low = _create_clip(storage, job_id, score=60, start_s=5.0)
    high_late = _create_clip(storage, job_id, score=95, start_s=50.0)
    high_early = _create_clip(storage, job_id, score=95, start_s=20.0)
    failed = _create_clip(storage, job_id, status="failed", score=80)

    res = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert res.status_code == 200
    assert "file_path" not in res.text
    clips = res.json()["clips"]
    assert [c["id"] for c in clips] == [str(high_early), str(high_late), str(failed), str(low)]
    assert set(clips[0].keys()) == {
        "id",
        "start_s",
        "end_s",
        "score",
        "title",
        "hook",
        "score_source",
        "caption_status",
        "status",
        "error",
        "created_at",
    }
    failed_row = next(c for c in clips if c["id"] == str(failed))
    assert failed_row["status"] == "failed"
    assert failed_row["caption_status"] == "failed"
    assert failed_row["error"] == "This clip could not be exported."
