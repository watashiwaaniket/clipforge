import json
import subprocess
from collections.abc import Generator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import redis
from celery.exceptions import SoftTimeLimitExceeded

import app.workers.pipeline as pipeline_module
from app.config import settings
from app.db import SessionLocal
from app.errors import InvalidStoredKey, InvalidTransition, TranscriptionFailed
from app.logging import configure_logging
from app.models import ApiKey, Job, User
from app.security import encrypt_api_key, hash_password
from app.services.downloader import DownloadResult
from app.services.jobs import transition_job
from app.services.scorer import ScoredWindow, load_windows
from app.services.transcribe import Word
from app.workers.celery_app import celery_app
from app.workers.pipeline import _select_whisper_key, begin_attempt, process_job


def _sk_canary() -> str:
    return "sk-canary-" + "DO-NOT-LEAK-" + "0123456789abcdef"


def _gsk_canary() -> str:
    return "gsk_" + "canary-" + "DO-NOT-LEAK-" + "abcdef0123456789"


@pytest.fixture(autouse=True)
def flush_test_redis() -> Generator[None, None, None]:
    client = redis.Redis.from_url(settings.redis_url)
    try:
        client.flushdb()
        yield
        client.flushdb()
    finally:
        client.close()


def _seed_queued_job(
    *,
    provider: str | None = None,
    base_url: str | None = None,
    api_key_plaintext: str | None = None,
    raw_encrypted_key: bytes | None = None,
) -> tuple[UUID, str]:
    with SessionLocal() as session:
        user = User(
            email=f"pipe-{uuid4().hex[:8]}@example.com",
            password_hash=hash_password("correct-horse-battery"),
        )
        session.add(user)
        session.flush()
        if provider is not None:
            encrypted = (
                raw_encrypted_key
                if raw_encrypted_key is not None
                else encrypt_api_key(api_key_plaintext or "sk-default-key")
            )
            session.add(
                ApiKey(
                    user_id=user.id,
                    provider=provider,
                    encrypted_key=encrypted,
                    base_url=base_url or "https://api.openai.com/v1",
                    model="gpt-4o-mini",
                )
            )
        job = Job(
            user_id=user.id,
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            platform="youtube",
            status="queued",
            progress_message="Queued",
        )
        session.add(job)
        session.commit()
        return user.id, str(job.id)


def test_celery_time_limits_are_configured() -> None:
    assert celery_app.conf.task_soft_time_limit == 2700
    assert celery_app.conf.task_time_limit == 2760
    assert "openai" not in pipeline_module.__dict__


def test_process_job_completes_end_to_end(
    clean_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "openai_api_key", "")
    _user_id, job_id = _seed_queued_job(
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key_plaintext="sk-user-openai-key",
    )

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
            payload = (
                {"streams": [{"width": 1920, "height": 1080}]}
                if "stream=width,height" in argv
                else {"format": {"duration": "180.0"}}
            )
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
        if argv[0] == "ffmpeg":
            cwd = kwargs.get("cwd")
            out_path = Path(str(cwd)) / argv[-1] if cwd else Path(argv[-1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"rendered-mp4")
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"Unexpected command: {argv}")

    monkeypatch.setattr("app.services.downloader.subprocess.run", fake_run)
    monkeypatch.setattr("app.services.video.subprocess.run", fake_run)

    observed_statuses: list[str] = []
    original_transition = pipeline_module.transition_job

    def spy_transition(session, job, new_status, message):
        observed_statuses.append(new_status)
        return original_transition(session, job, new_status, message)

    monkeypatch.setattr("app.workers.pipeline.transition_job", spy_transition)

    class FakeTranscriber:
        def __init__(self, on_chunk) -> None:
            self._on_chunk = on_chunk
            self.last_chunk_count = 1

        def transcribe(self, audio_path: Path) -> list[Word]:
            assert audio_path == tmp_path / "jobs" / job_id / "vod.mp4"
            if self._on_chunk is not None:
                self._on_chunk(1, 1)
            return [Word(word="hello", start=1.2, end=1.6)]

    monkeypatch.setattr(
        "app.workers.pipeline.build_transcriber",
        lambda _key, *, language, on_chunk=None: FakeTranscriber(on_chunk),
    )

    def fake_run_scoring(
        words: list[Word],
        *,
        duration_s: float,
        api_key: str,
        base_url: str,
        model: str,
        provider: str,
        max_clips: int,
        job_id: str,
        on_chunk=None,
    ) -> list[ScoredWindow]:
        assert api_key == "sk-user-openai-key"
        assert base_url == "https://api.openai.com/v1"
        assert model == "gpt-4o-mini"
        assert provider == "openai"
        assert duration_s == 180.0
        assert max_clips == settings.max_clips_per_job
        if on_chunk is not None:
            on_chunk(1, 1)
        return [
            ScoredWindow(
                start_s=0.0,
                end_s=9.6,
                score=91,
                title="Heist chaos",
                hook="Immediate action.",
                score_source="llm",
                word_start=0,
                word_end=0,
            )
        ]

    monkeypatch.setattr("app.workers.pipeline.run_scoring", fake_run_scoring)

    saved_windows_snapshot: list[ScoredWindow] = []
    original_run_clipping = pipeline_module.run_clipping

    def spy_run_clipping(uid, lock_token, *, vod, windows, words, duration_s):
        w_path = tmp_path / "jobs" / str(uid) / "windows.json"
        assert w_path.exists()
        saved_windows_snapshot.extend(load_windows(w_path) or [])
        return original_run_clipping(
            uid,
            lock_token,
            vod=vod,
            windows=windows,
            words=words,
            duration_s=duration_s,
        )

    monkeypatch.setattr("app.workers.pipeline.run_clipping", spy_run_clipping)

    process_job(job_id)

    expected_mp4 = tmp_path / "jobs" / job_id / "vod.mp4"
    expected_transcript = tmp_path / "jobs" / job_id / "transcript.json"
    expected_windows = tmp_path / "jobs" / job_id / "windows.json"
    expected_wav = tmp_path / "jobs" / job_id / "audio.wav"
    expected_work = tmp_path / "jobs" / job_id / "work"
    assert not expected_mp4.exists()
    assert not expected_transcript.exists()
    assert not expected_windows.exists()
    assert not expected_wav.exists()
    assert not expected_work.exists()
    assert saved_windows_snapshot == [
        ScoredWindow(
            start_s=0.0,
            end_s=9.6,
            score=91,
            title="Heist chaos",
            hook="Immediate action.",
            score_source="llm",
            word_start=0,
            word_end=0,
        )
    ]
    assert observed_statuses == ["downloading", "transcribing", "scoring", "clipping", "completed"]

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.error is None
        assert job.progress_message == "Ready · 1 clips"
        assert job.title == "Pipeline Test VOD"
        assert job.duration_s == 180.0
        assert job.thumbnail_url == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
        assert job.attempt == 1
        assert job.started_at is not None
        assert job.finished_at is not None
        clips = list(job.clips)
        assert len(clips) == 1
        assert clips[0].status == "ready"
        assert clips[0].caption_status == "burned"
        assert clips[0].file_path == f"jobs/{job_id}/clips/{clips[0].id}.mp4"
        assert (tmp_path / clips[0].file_path).read_bytes() == b"rendered-mp4"

    r = redis.Redis.from_url(settings.redis_url)
    try:
        assert r.get(f"joblock:{job_id}") is None
    finally:
        r.close()


def test_select_whisper_key_precedence(clean_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. User OpenAI key beats operator fallback even when both are set.
    user_key = "sk-user-primary-123456"
    uid_openai, _ = _seed_queued_job(
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key_plaintext=user_key,
    )
    monkeypatch.setattr(settings, "openai_api_key", "sk-operator-fallback-9999")
    assert _select_whisper_key(uid_openai) == user_key

    # 2. Non-OpenAI user key uses operator fallback when configured.
    uid_groq, job_groq_id = _seed_queued_job(
        provider="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_plaintext="gsk_user_groq_key",
    )
    assert _select_whisper_key(uid_groq) == "sk-operator-fallback-9999"

    # 3. Groq user key with empty operator key fails before constructing client.
    monkeypatch.setattr(settings, "openai_api_key", "")
    factory_called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal factory_called
        factory_called = True
        raise AssertionError("Transcriber should not be constructed")

    monkeypatch.setattr("app.workers.pipeline.build_transcriber", fail_if_called)
    monkeypatch.setattr(
        "app.workers.pipeline.probe_and_download",
        lambda _url, job_dir, **_kw: (
            (job_dir / "vod.mp4").write_bytes(b"mp4"),
            DownloadResult(
                path=job_dir / "vod.mp4",
                platform="youtube",
                title="Groq key fail",
                duration_s=60.0,
                thumbnail_url=None,
            ),
        )[-1],
    )
    process_job(job_groq_id)
    assert factory_called is False
    with SessionLocal() as session:
        job = session.get(Job, job_groq_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error == (
            "Transcription needs an OpenAI Whisper key on the server, or an OpenAI key saved in "
            "your account. Other providers can score clips but cannot transcribe in this build."
        )

    # 4. Corrupt OpenAI ciphertext raises InvalidStoredKey instead of falling back.
    monkeypatch.setattr(settings, "openai_api_key", "sk-operator-fallback-9999")
    uid_bad, _ = _seed_queued_job(
        provider="openai",
        base_url="https://api.openai.com/v1",
        raw_encrypted_key=b"not-a-valid-fernet-token",
    )
    with pytest.raises(InvalidStoredKey):
        _select_whisper_key(uid_bad)


@pytest.mark.parametrize("canary_fn", [_sk_canary, _gsk_canary])
def test_process_job_redacts_whisper_canary_from_logs_and_error(
    clean_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    canary_fn,
) -> None:
    import logging

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    canary = canary_fn()
    monkeypatch.setattr(settings, "openai_api_key", canary)
    _uid, job_id = _seed_queued_job(
        provider="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_plaintext="gsk_other_key_1234",
    )

    monkeypatch.setattr(
        "app.workers.pipeline.probe_and_download",
        lambda _url, job_dir, **_kw: (
            (job_dir / "vod.mp4").write_bytes(b"mp4"),
            DownloadResult(
                path=job_dir / "vod.mp4",
                platform="youtube",
                title="Canary VOD",
                duration_s=60.0,
                thumbnail_url=None,
            ),
        )[-1],
    )

    received_keys: list[str] = []

    class ExplodingTranscriber:
        def transcribe(self, _audio_path: Path) -> list[Word]:
            raise TranscriptionFailed(f"failed calling whisper with {canary}")

    def spy_build(api_key: str, *, language: str, on_chunk=None):
        received_keys.append(api_key)
        return ExplodingTranscriber()

    monkeypatch.setattr("app.workers.pipeline.build_transcriber", spy_build)

    configure_logging()
    with caplog.at_level(logging.INFO):
        process_job(job_id)

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert received_keys == [canary]
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error is not None
        assert canary not in job.error
        assert "[REDACTED]" in job.error
    assert canary not in joined


def test_begin_attempt_lock_eviction_requeue_and_attempt_cap(
    clean_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _uid, job_id = _seed_queued_job()
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
    _uid, job_id = _seed_queued_job()
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


def test_run_pipeline_resumes_valid_vod_transcript_and_windows(
    clean_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    _uid, job_id = _seed_queued_job(
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key_plaintext="sk-resume-key",
    )
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.status = "scoring"
        job.duration_s = 120.0
        session.commit()

    vod_file = tmp_path / "jobs" / job_id / "vod.mp4"
    vod_file.parent.mkdir(parents=True, exist_ok=True)
    vod_file.write_bytes(b"valid-vod")
    transcript_file = tmp_path / "jobs" / job_id / "transcript.json"
    transcript_file.write_text(
        json.dumps([{"word": "resumed", "start": 0.5, "end": 0.9}]),
        encoding="utf-8",
    )
    windows_file = tmp_path / "jobs" / job_id / "windows.json"
    windows_file.write_text("[]", encoding="utf-8")

    monkeypatch.setattr("app.workers.pipeline.probe_media_duration", lambda _p: 121.5)
    monkeypatch.setattr(
        "app.workers.pipeline.probe_and_download",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("should not download")),
    )
    monkeypatch.setattr(
        "app.workers.pipeline.build_transcriber",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("should not transcribe")),
    )
    monkeypatch.setattr(
        "app.workers.pipeline.run_scoring",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("should not score")),
    )
    process_job(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.error is None
        assert job.progress_message == "No clip-worthy moments in this VOD."


def test_scoring_missing_api_key_row_fails_without_operator_fallback(
    clean_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "openai_api_key", "sk-operator-should-not-score")
    _uid, job_id = _seed_queued_job()
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.duration_s = 60.0
        session.commit()

    vod_file = tmp_path / "jobs" / job_id / "vod.mp4"
    vod_file.parent.mkdir(parents=True, exist_ok=True)
    vod_file.write_bytes(b"valid-vod")
    transcript_file = tmp_path / "jobs" / job_id / "transcript.json"
    transcript_file.write_text(
        json.dumps([{"word": "resumed", "start": 0.5, "end": 0.9}]),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.workers.pipeline.probe_media_duration", lambda _p: 60.0)
    monkeypatch.setattr(
        "app.workers.pipeline.run_scoring",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("should not score")),
    )

    process_job(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error == "Add an API key before generating clips."


def test_scoring_corrupt_api_key_fails_job(
    clean_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    _uid, job_id = _seed_queued_job(
        provider="groq",
        base_url="https://api.groq.com/openai/v1",
        raw_encrypted_key=b"corrupt-ciphertext",
    )
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.duration_s = 60.0
        session.commit()

    vod_file = tmp_path / "jobs" / job_id / "vod.mp4"
    vod_file.parent.mkdir(parents=True, exist_ok=True)
    vod_file.write_bytes(b"valid-vod")
    transcript_file = tmp_path / "jobs" / job_id / "transcript.json"
    transcript_file.write_text(
        json.dumps([{"word": "resumed", "start": 0.5, "end": 0.9}]),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.workers.pipeline.probe_media_duration", lambda _p: 60.0)

    process_job(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error == "The saved API key could not be decrypted. Enter it again."


@pytest.mark.parametrize("canary_fn", [_sk_canary, _gsk_canary])
def test_process_job_redacts_scoring_canary_from_logs_and_error(
    clean_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    canary_fn,
) -> None:
    import logging

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    canary = canary_fn()
    _uid, job_id = _seed_queued_job(
        provider="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_plaintext=canary,
    )
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.duration_s = 60.0
        session.commit()

    vod_file = tmp_path / "jobs" / job_id / "vod.mp4"
    vod_file.parent.mkdir(parents=True, exist_ok=True)
    vod_file.write_bytes(b"valid-vod")
    transcript_file = tmp_path / "jobs" / job_id / "transcript.json"
    transcript_file.write_text(
        json.dumps([{"word": "resumed", "start": 0.5, "end": 0.9}]),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.workers.pipeline.probe_media_duration", lambda _p: 60.0)

    received_keys: list[str] = []

    def exploding_scoring(*_args, api_key: str, **_kwargs):
        received_keys.append(api_key)
        raise pipeline_module.ClipForgeError("internal", f"scoring crashed with {canary}")

    monkeypatch.setattr("app.workers.pipeline.run_scoring", exploding_scoring)

    configure_logging()
    with caplog.at_level(logging.INFO):
        process_job(job_id)

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert received_keys == [canary]
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error is not None
        assert canary not in job.error
        assert "[REDACTED]" in job.error
    assert canary not in joined


def test_process_job_soft_time_limit_marks_timeout(
    clean_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _uid, job_id = _seed_queued_job()
    cancelled = False

    def spy_cancel() -> None:
        nonlocal cancelled
        cancelled = True

    monkeypatch.setattr("app.workers.pipeline.cancel_score_pool", spy_cancel)
    monkeypatch.setattr(
        "app.workers.pipeline.run_pipeline",
        lambda *_a, **_kw: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )
    process_job(job_id)
    assert cancelled is True
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.attempt == 1
        assert job.status == "failed"
        assert job.error == "This job exceeded the 45 minute limit and was stopped."


def test_process_job_at_attempt_cap_fails_without_downloading_and_releases_lock(
    clean_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _uid, job_id = _seed_queued_job()
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
    _uid, job_id = _seed_queued_job()
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
