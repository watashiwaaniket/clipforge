import json
import subprocess
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
import redis

from app.config import settings
from app.db import SessionLocal
from app.errors import InvalidTransition
from app.models import Job, User
from app.security import hash_password
from app.services.jobs import transition_job
from app.workers.pipeline import begin_attempt, process_job


@pytest.fixture(autouse=True)
def flush_test_redis() -> Generator[None, None, None]:
    client = redis.Redis.from_url(settings.redis_url)
    try:
        client.flushdb()
        yield
        client.flushdb()
    finally:
        client.close()


def _seed_queued_job() -> str:
    with SessionLocal() as session:
        user = User(
            email=f"pipe-{uuid4().hex[:8]}@example.com",
            password_hash=hash_password("correct-horse-battery"),
        )
        session.add(user)
        session.flush()
        job = Job(
            user_id=user.id,
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            platform="youtube",
            status="queued",
            progress_message="Queued",
        )
        session.add(job)
        session.commit()
        return str(job.id)


def test_process_job_stops_after_download(
    clean_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    job_id = _seed_queued_job()

    def fake_run(argv, **kwargs):
        if "--dump-single-json" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "duration": 180.0,
                        "title": "Pipeline Test VOD",
                        "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
                        "extractor_key": "Youtube",
                    }
                ),
                stderr="",
            )
        if argv[0] == "yt-dlp":
            out_idx = argv.index("--output") + 1
            out_template = argv[out_idx]
            mp4_path = Path(out_template.replace("%(ext)s", "mp4"))
            mp4_path.parent.mkdir(parents=True, exist_ok=True)
            mp4_path.write_bytes(b"mp4-content")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "ffprobe":
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"format": {"duration": "180.0"}}), stderr=""
            )
        raise AssertionError(f"Unexpected command: {argv}")

    monkeypatch.setattr("app.services.downloader.subprocess.run", fake_run)

    process_job(job_id)

    expected_mp4 = tmp_path / "jobs" / job_id / "vod.mp4"
    assert expected_mp4.exists()

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error == "Download finished. Later stages are not included in this build yet."
        assert job.title == "Pipeline Test VOD"
        assert job.duration_s == 180.0
        assert job.thumbnail_url == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
        assert job.attempt == 1
        assert job.started_at is not None
        assert job.finished_at is not None

    r = redis.Redis.from_url(settings.redis_url)
    try:
        assert r.get(f"joblock:{job_id}") is None
    finally:
        r.close()


def test_begin_attempt_lock_eviction_requeue_and_attempt_cap(
    clean_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _seed_queued_job()
    r = redis.Redis.from_url(settings.redis_url)
    try:
        # Active remote/other-host lock defers without incrementing attempt
        r.set(f"joblock:{job_id}", "other-host:999999:abc", ex=60)
        requeues: list[tuple[list[str], int]] = []
        monkeypatch.setattr(
            "app.workers.pipeline.process_job.apply_async",
            lambda args, countdown: requeues.append((list(args), countdown)),
        )
        decision, token = begin_attempt(job_id)
        assert (decision, token) == ("skip", None)
        assert requeues == [([job_id], 15)]

        with SessionLocal() as session:
            assert session.get(Job, job_id).attempt == 0

        # Dead local PID lock is evicted and attempt advances
        import socket

        r.set(f"joblock:{job_id}", f"{socket.gethostname()}:99999999:dead", ex=60)
        decision, token = begin_attempt(job_id)
        assert decision == "run"
        assert token is not None
        with SessionLocal() as session:
            assert session.get(Job, job_id).attempt == 1

        # Simulate attempt > 3 cap
        r.delete(f"joblock:{job_id}")
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            assert job is not None
            job.attempt = 3
            session.commit()

        decision, token = begin_attempt(job_id)
        assert decision == "stop"
        assert token is not None
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            assert job is not None
            assert job.status == "failed"
            assert job.error == "The worker restarted while this job was running. Please try again."

        # Terminal job skips cleanly
        decision, token = begin_attempt(job_id)
        assert (decision, token) == ("skip", None)
    finally:
        r.close()


def test_transition_job_enforces_forward_only(clean_db: None) -> None:
    job_id = _seed_queued_job()
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None

        with pytest.raises(InvalidTransition):
            transition_job(session, job, "failed", "bad")

        transition_job(session, job, "downloading", "Downloading the VOD")
        assert job.started_at is not None

        with pytest.raises(InvalidTransition):
            transition_job(session, job, "downloading", "again")
        with pytest.raises(InvalidTransition):
            transition_job(session, job, "queued", "backwards")


def test_run_pipeline_resumes_valid_vod_and_redownloads_corrupt_vod(
    clean_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    job_id = _seed_queued_job()
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.status = "downloading"
        job.duration_s = 120.0
        session.commit()

    vod_file = tmp_path / "jobs" / job_id / "vod.mp4"
    vod_file.parent.mkdir(parents=True, exist_ok=True)
    vod_file.write_bytes(b"valid-vod")

    # 1. Matching ffprobe duration skips probe_and_download
    monkeypatch.setattr("app.workers.pipeline.probe_media_duration", lambda _p: 121.5)
    monkeypatch.setattr(
        "app.workers.pipeline.probe_and_download",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("should not download")),
    )
    process_job(job_id)
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error == "Download finished. Later stages are not included in this build yet."

    # 2. Reset to downloading with unreadable vod.mp4 -> triggers download
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.status = "downloading"
        job.error = None
        job.finished_at = None
        job.attempt = 0
        session.commit()

    vod_file.write_bytes(b"corrupt")
    monkeypatch.setattr("app.workers.pipeline.probe_media_duration", lambda _p: None)
    downloaded_urls: list[str] = []

    def fake_probe_and_download(url: str, job_dir: Path, **_kwargs):
        from app.services.downloader import DownloadResult

        downloaded_urls.append(url)
        out = job_dir / "vod.mp4"
        out.write_bytes(b"re-downloaded")
        return DownloadResult(
            path=out,
            platform="youtube",
            title="Resumed VOD",
            duration_s=120.0,
            thumbnail_url=None,
        )

    monkeypatch.setattr("app.workers.pipeline.probe_and_download", fake_probe_and_download)
    process_job(job_id)
    assert len(downloaded_urls) == 1
    assert vod_file.read_bytes() == b"re-downloaded"


def test_process_job_at_attempt_cap_fails_without_downloading_and_releases_lock(
    clean_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _seed_queued_job()
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.attempt = 3
        session.commit()

    monkeypatch.setattr(
        "app.workers.pipeline.probe_and_download",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("should not download")),
    )
    process_job(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.attempt == 4
        assert job.status == "failed"
        assert job.error == "The worker restarted while this job was running. Please try again."

    r = redis.Redis.from_url(settings.redis_url)
    try:
        assert r.get(f"joblock:{job_id}") is None
    finally:
        r.close()


def test_begin_attempt_releases_lock_on_unexpected_exception(
    clean_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _seed_queued_job()
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.attempt = 3
        session.commit()

    monkeypatch.setattr(
        "app.workers.pipeline.fail_job",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    with pytest.raises(RuntimeError, match="db down"):
        begin_attempt(job_id)

    r = redis.Redis.from_url(settings.redis_url)
    try:
        assert r.get(f"joblock:{job_id}") is None
    finally:
        r.close()

