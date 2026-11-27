import base64
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import redis

import app.workers.pipeline as pipeline_module
from app.config import settings
from app.db import SessionLocal
from app.models import Job, User
from app.security import hash_password
from app.services.storage import (
    audio_path,
    chunks_dir,
    cleanup_raw,
    clips_dir,
    ensure_job_dirs,
    resolve_under_storage,
    transcript_path,
    vod_path,
    windows_path,
    work_dir,
)
from app.workers.celery_app import celery_app
from app.workers.pipeline import (
    _broker_job_ids,
    reap_stale_jobs,
    refresh_job_lock,
    release_job_lock,
    start_lock_refresher,
)


@pytest.fixture(autouse=True)
def flush_test_redis() -> Generator[None, None, None]:
    client = redis.Redis.from_url(settings.redis_url)
    try:
        client.flushdb()
        yield
        client.flushdb()
    finally:
        client.close()


def _seed_job(
    *,
    status: str = "queued",
    attempt: int = 0,
    created_at: datetime | None = None,
    enqueued_at: datetime | None = None,
    started_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> UUID:
    now = datetime.now(UTC)
    with SessionLocal() as session:
        user = User(
            email=f"cleanup-{uuid4().hex[:8]}@example.com",
            password_hash=hash_password("correct-horse-battery"),
        )
        session.add(user)
        session.flush()
        job = Job(
            user_id=user.id,
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            platform="youtube",
            status=status,
            progress_message="Queued" if status == "queued" else "Working",
            attempt=attempt,
            created_at=created_at or now,
            enqueued_at=enqueued_at,
            started_at=started_at,
            updated_at=updated_at or now,
        )
        session.add(job)
        session.commit()
        return job.id


def _make_celery_envelope(job_id: str, *, base64_body: bool = True) -> str:
    body_obj = [[job_id], {}, {"callbacks": None, "errbacks": None, "chain": None, "chord": None}]
    body_str = json.dumps(body_obj)
    if base64_body:
        body_payload = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
        encoding = "base64"
    else:
        body_payload = body_str
        encoding = "utf-8"
    return json.dumps(
        {
            "body": body_payload,
            "content-encoding": "utf-8",
            "content-type": "application/json",
            "headers": {"task": "process_job", "id": str(uuid4())},
            "properties": {"body_encoding": encoding, "delivery_tag": str(uuid4())},
        }
    )


def test_celery_hardening_settings() -> None:
    assert celery_app.conf.worker_max_tasks_per_child == 1
    assert celery_app.conf.worker_max_memory_per_child == 2_500_000
    assert celery_app.conf.task_routes["reap_stale_jobs"] == {"queue": "reaper"}
    assert celery_app.conf.beat_schedule["reap-stale-jobs"]["task"] == "reap_stale_jobs"
    assert celery_app.conf.beat_schedule["reap-stale-jobs"]["schedule"] == 60.0


def test_cleanup_raw_deletes_raw_artifacts_keeps_clips_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    job_id = uuid4()
    ensure_job_dirs(job_id)

    vod = vod_path(job_id)
    vod.write_bytes(b"raw-vod")
    (vod.parent / "vod.mp4.part").write_bytes(b"partial-vod")
    wav = audio_path(job_id)
    wav.write_bytes(b"wav-bytes")
    c_dir = chunks_dir(job_id)
    c_dir.mkdir(parents=True, exist_ok=True)
    (c_dir / "0.wav").write_bytes(b"chunk-0")
    t_file = transcript_path(job_id)
    t_file.write_text("[]", encoding="utf-8")
    w_file = windows_path(job_id)
    w_file.write_text("[]", encoding="utf-8")
    wk = work_dir(job_id)
    (wk / "clip_raw.mp4").write_bytes(b"raw-clip")
    (wk / "clip.ass").write_text("[Script Info]", encoding="utf-8")

    clip_file = clips_dir(job_id) / f"{uuid4()}.mp4"
    clip_file.write_bytes(b"final-clip")

    cleanup_raw(job_id)
    assert not vod.exists()
    assert not (vod.parent / "vod.mp4.part").exists()
    assert not wav.exists()
    assert not c_dir.exists()
    assert not t_file.exists()
    assert not w_file.exists()
    assert not wk.exists()
    assert clip_file.exists()
    assert clip_file.read_bytes() == b"final-clip"

    # Calling a second time succeeds with no exception.
    cleanup_raw(job_id)
    assert clip_file.exists()


def test_storage_escape_raises_and_deletes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    storage_root = tmp_path / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep-me", encoding="utf-8")

    with pytest.raises(ValueError):
        resolve_under_storage("../outside.txt")
    with pytest.raises(ValueError):
        cleanup_raw("../outside.txt")
    assert outside.read_text(encoding="utf-8") == "keep-me"


def test_reap_stale_jobs_timeout_pass_respects_lock_and_started_at(
    clean_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(pipeline_module, "REAPER_RECHECK_SLEEP_S", 0.0)
    now = datetime.now(UTC)

    # 1. Started 46 min ago, lock absent -> timed out and vod.mp4 deleted.
    stale_id = _seed_job(
        status="scoring",
        attempt=1,
        started_at=now - timedelta(minutes=46),
        updated_at=now - timedelta(minutes=46),
    )
    ensure_job_dirs(stale_id)
    stale_vod = vod_path(stale_id)
    stale_vod.write_bytes(b"stale-vod")

    # 2. Started 46 min ago, lock held -> left alone and vod.mp4 preserved.
    locked_id = _seed_job(
        status="clipping",
        attempt=1,
        started_at=now - timedelta(minutes=46),
        updated_at=now - timedelta(minutes=46),
    )
    ensure_job_dirs(locked_id)
    locked_vod = vod_path(locked_id)
    locked_vod.write_bytes(b"locked-vod")

    # 3. updated_at 60 min ago, started_at 1 min ago -> not reaped.
    recent_id = _seed_job(
        status="downloading",
        attempt=1,
        started_at=now - timedelta(minutes=1),
        updated_at=now - timedelta(minutes=60),
    )

    r = redis.Redis.from_url(settings.redis_url)
    try:
        r.set(f"joblock:{locked_id}", "other-host:123:abc", ex=60)
        reap_stale_jobs()
    finally:
        r.close()

    with SessionLocal() as session:
        stale_job = session.get(Job, stale_id)
        assert stale_job is not None
        assert stale_job.status == "failed"
        assert stale_job.error == pipeline_module.TIMEOUT_MESSAGE
        assert not stale_vod.exists()

        locked_job = session.get(Job, locked_id)
        assert locked_job is not None
        assert locked_job.status == "clipping"
        assert locked_job.error is None
        assert locked_vod.exists()

        recent_job = session.get(Job, recent_id)
        assert recent_job is not None
        assert recent_job.status == "downloading"
        assert recent_job.error is None


def test_reap_stale_jobs_lost_enqueue_and_broker_presence(
    clean_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline_module, "REAPER_RECHECK_SLEEP_S", 0.0)
    now = datetime.now(UTC)

    # Absent from broker, enqueued_at NULL, created 4 min ago -> enqueue_failed
    lost_id = _seed_job(
        status="queued",
        attempt=0,
        created_at=now - timedelta(minutes=4),
        enqueued_at=None,
    )
    # Present in "celery" list, enqueued_at NULL, created 4 min ago -> kept queued
    in_celery_id = _seed_job(
        status="queued",
        attempt=0,
        created_at=now - timedelta(minutes=4),
        enqueued_at=None,
    )
    # Present in "unacked" hash, enqueued_at NULL, created 4 min ago -> kept queued
    in_unacked_id = _seed_job(
        status="queued",
        attempt=0,
        created_at=now - timedelta(minutes=4),
        enqueued_at=None,
    )
    # Enqueued 90 min ago, still waiting in "celery" -> kept queued
    long_wait_id = _seed_job(
        status="queued",
        attempt=0,
        created_at=now - timedelta(minutes=91),
        enqueued_at=now - timedelta(minutes=90),
    )

    r = redis.Redis.from_url(settings.redis_url)
    try:
        r.rpush("celery", _make_celery_envelope(str(in_celery_id)))
        r.rpush("celery", _make_celery_envelope(str(long_wait_id), base64_body=False))
        r.hset(
            "unacked",
            "tag-1",
            json.dumps([json.loads(_make_celery_envelope(str(in_unacked_id))), "", "celery"]),
        )
        assert _broker_job_ids(r) == {str(in_celery_id), str(in_unacked_id), str(long_wait_id)}

        reap_stale_jobs()
    finally:
        r.close()

    with SessionLocal() as session:
        lost_job = session.get(Job, lost_id)
        assert lost_job is not None
        assert lost_job.status == "failed"
        assert lost_job.error == pipeline_module.ENQUEUE_FAILED_MESSAGE

        for kept_id in (in_celery_id, in_unacked_id, long_wait_id):
            row = session.get(Job, kept_id)
            assert row is not None
            assert row.status == "queued"
            assert row.error is None


def test_reap_stale_jobs_redrives_once_then_fails_lost_queued(
    clean_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline_module, "REAPER_RECHECK_SLEEP_S", 0.0)
    now = datetime.now(UTC)

    job_id = _seed_job(
        status="queued",
        attempt=0,
        created_at=now - timedelta(minutes=11),
        enqueued_at=now - timedelta(minutes=10),
    )

    delayed: list[str] = []
    monkeypatch.setattr(
        "app.workers.pipeline.process_job.delay",
        lambda jid: delayed.append(jid),
    )

    # First miss -> redrive once and set reaper:redriven:{id}
    reap_stale_jobs()
    assert delayed == [str(job_id)]

    r = redis.Redis.from_url(settings.redis_url)
    try:
        assert r.get(f"reaper:redriven:{job_id}") == b"1"
    finally:
        r.close()

    with SessionLocal() as session:
        row = session.get(Job, job_id)
        assert row is not None
        assert row.status == "queued"
        # Simulate another 4 minutes passing without the job hitting Redis
        row.enqueued_at = datetime.now(UTC) - timedelta(minutes=4)
        session.commit()

    # Second miss -> mark failed enqueue_failed
    reap_stale_jobs()
    assert delayed == [str(job_id)]

    with SessionLocal() as session:
        row = session.get(Job, job_id)
        assert row is not None
        assert row.status == "failed"
        assert row.error == pipeline_module.ENQUEUE_FAILED_MESSAGE


def test_reap_stale_jobs_orphan_media_pass(
    clean_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(pipeline_module, "REAPER_RECHECK_SLEEP_S", 0.0)

    monkeypatch.setattr(pipeline_module, "ORPHAN_BATCH_SIZE", 1)
    job_id, later_id = sorted(_seed_job(status="completed", attempt=1) for _ in range(2))
    ensure_job_dirs(later_id)
    later_vod = vod_path(later_id)
    later_vod.write_bytes(b"leftover-vod")
    ensure_job_dirs(job_id)
    vod = vod_path(job_id)
    vod.write_bytes(b"leftover-vod")
    clip = clips_dir(job_id) / f"{uuid4()}.mp4"
    clip.write_bytes(b"final-clip")

    reap_stale_jobs()

    assert not vod.exists()
    assert later_vod.exists()
    reap_stale_jobs()
    assert not later_vod.exists()
    assert clip.exists()
    with SessionLocal() as session:
        row = session.get(Job, job_id)
        assert row is not None
        assert row.status == "completed"


def test_lock_refresher_uses_cas_expire_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = str(uuid4())
    lock_token = "host:1:inst"
    r = redis.Redis.from_url(settings.redis_url)
    try:
        # Missing lock cannot be recreated by refresh_job_lock (CAS EXPIRE).
        assert refresh_job_lock(job_id, lock_token) is False
        assert r.get(f"joblock:{job_id}") is None

        # Wrong token cannot refresh or delete another holder's lock.
        r.set(f"joblock:{job_id}", "other-host:2:other", ex=60)
        assert refresh_job_lock(job_id, lock_token) is False
        release_job_lock(job_id, lock_token)
        assert r.get(f"joblock:{job_id}") == b"other-host:2:other"

        # Matching token refreshes and stops/joins cleanly.
        r.set(f"joblock:{job_id}", lock_token, ex=30)
        monkeypatch.setattr(pipeline_module, "JOB_LOCK_REFRESH_INTERVAL_S", 0.01)
        stop_event, thread = start_lock_refresher(job_id, lock_token)
        stop_event.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        release_job_lock(job_id, lock_token)
        assert r.get(f"joblock:{job_id}") is None
    finally:
        r.close()


def test_readme_documents_contract_and_no_canary_in_repo() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    readme = repo_root / "README.md"
    if readme.exists():
        text = readme.read_text(encoding="utf-8")
        assert "/api/v1/health" in text
        assert "MAX_VOD_MINUTES" in text
        assert "smoke.sh" in text

    canary = "sk-canary-" + "DO-NOT-LEAK-" + "0123456789abcdef"
    backend_root = Path(__file__).resolve().parents[1]
    for path in backend_root.rglob("*.py"):
        content = path.read_text(encoding="utf-8", errors="ignore")
        assert canary not in content, f"Contiguous canary found in {path}"
