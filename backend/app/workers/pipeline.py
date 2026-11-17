import os
import socket
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import redis
import structlog
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.errors import ClipForgeError, TranscriptionFailed
from app.logging import push_secret, secret_boundary
from app.models import ApiKey, Job
from app.security import decrypt_api_key
from app.services.downloader import probe_and_download, probe_media_duration
from app.services.jobs import ORDER, fail_job, is_terminal, set_progress, transition_job
from app.services.storage import cleanup_raw, ensure_job_dirs, transcript_path, vod_path
from app.services.transcribe import (
    Transcriber,
    WhisperApiTranscriber,
    load_transcript,
    save_transcript,
)
from app.workers.celery_app import celery_app

log = structlog.get_logger()

# PHASE 2 ONLY. Deleted in phase 3 when scoring is real.
STUB_AFTER_TRANSCRIBE = True

JOB_LOCK_TTL_S = 60
JOB_LOCK_REFRESH_INTERVAL_S = 20.0
MAX_JOB_ATTEMPTS = 3

_CAS_DEL_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

_CAS_EXPIRE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], tonumber(ARGV[2]))
else
    return 0
end
"""

_worker_instance_id = uuid4().hex


def init_worker_instance_id() -> str:
    global _worker_instance_id
    _worker_instance_id = uuid4().hex
    return _worker_instance_id


def _make_lock_token() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{_worker_instance_id}"


def _job_lock_key(job_id: UUID | str) -> str:
    return f"joblock:{job_id}"


def _is_pid_dead(pid: int) -> bool:
    if pid <= 0:
        return True
    proc_stat = Path(f"/proc/{pid}/stat")
    if Path("/proc").exists():
        if not proc_stat.exists():
            return True
        try:
            content = proc_stat.read_text(encoding="utf-8", errors="ignore")
            rparen = content.rfind(")")
            if rparen != -1:
                fields = content[rparen + 2 :].split()
                if fields and fields[0] == "Z":
                    return True
            return False
        except OSError:
            return True

    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return True


def _is_holder_stale(holder_token: str) -> bool:
    parts = holder_token.rsplit(":", 2)
    if len(parts) != 3:
        return False
    holder_host, holder_pid_str, holder_instance = parts
    if holder_host != socket.gethostname():
        return False
    try:
        holder_pid = int(holder_pid_str)
    except ValueError:
        return False
    if _is_pid_dead(holder_pid):
        return True
    return holder_pid == os.getpid() and holder_instance != _worker_instance_id


def _redis_client() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url)


def holds_job_lock(job_id: UUID | str, lock_token: str) -> bool:
    client = _redis_client()
    try:
        current = client.get(_job_lock_key(job_id))
        if current is None:
            return False
        decoded = (
            current.decode("utf-8", errors="ignore")
            if isinstance(current, bytes)
            else str(current)
        )
        return decoded == lock_token
    finally:
        client.close()


def refresh_job_lock(job_id: UUID | str, lock_token: str) -> bool:
    client = _redis_client()
    try:
        res = client.eval(_CAS_EXPIRE_LUA, 1, _job_lock_key(job_id), lock_token, JOB_LOCK_TTL_S)
        return bool(res)
    finally:
        client.close()


def release_job_lock(job_id: UUID | str, lock_token: str | None) -> None:
    if not lock_token:
        return
    client = _redis_client()
    try:
        client.eval(_CAS_DEL_LUA, 1, _job_lock_key(job_id), lock_token)
    finally:
        client.close()


def start_lock_refresher(job_id: str, lock_token: str) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def _run() -> None:
        while not stop_event.wait(JOB_LOCK_REFRESH_INTERVAL_S):
            try:
                refresh_job_lock(job_id, lock_token)
            except Exception:
                log.warning("job_lock_refresh_failed", job_id=job_id)

    thread = threading.Thread(target=_run, name=f"lock-refresh-{job_id}", daemon=True)
    thread.start()
    return stop_event, thread


def begin_attempt(job_id: str) -> tuple[str, str | None]:
    uid = UUID(str(job_id))

    # 1. Terminal check before touching lock or attempt.
    with session_scope() as session:
        job = session.get(Job, uid)
        if job is None or job.status in {"completed", "failed"}:
            return "skip", None

    # 2. Acquire Redis lock, evict dead/reused-PID local holder, or defer with countdown.
    lock_key = _job_lock_key(uid)
    lock_token = _make_lock_token()
    acquired = False
    client = _redis_client()
    try:
        for _ in range(2):
            if client.set(lock_key, lock_token, nx=True, ex=JOB_LOCK_TTL_S):
                acquired = True
                break
            holder_raw = client.get(lock_key)
            if holder_raw is None:
                continue
            holder = (
                holder_raw.decode("utf-8", errors="ignore")
                if isinstance(holder_raw, bytes)
                else str(holder_raw)
            )
            if _is_holder_stale(holder):
                client.eval(_CAS_DEL_LUA, 1, lock_key, holder)
                if client.set(lock_key, lock_token, nx=True, ex=JOB_LOCK_TTL_S):
                    acquired = True
                    break
            break
    finally:
        client.close()

    if not acquired:
        process_job.apply_async(args=[str(uid)], countdown=15)
        return "skip", None

    try:
        # 3. Increment attempt inside short FOR UPDATE transaction before any download.
        with session_scope() as session:
            job = session.get(Job, uid, with_for_update=True)
            if job is None or job.status in {"completed", "failed"}:
                release_job_lock(uid, lock_token)
                return "skip", None
            job.attempt += 1
            job.updated_at = datetime.now(UTC)
            attempt = job.attempt

        # 4. Enforce attempt cap (3 executions allowed; 4th marks failed and cleans up).
        if attempt > MAX_JOB_ATTEMPTS:
            fail_job(
                uid,
                "worker_lost",
                "The worker restarted while this job was running. Please try again.",
            )
            return "stop", lock_token

        return "run", lock_token
    except Exception:
        try:
            release_job_lock(uid, lock_token)
        except Exception:
            log.warning("job_lock_release_failed", job_id=str(uid))
        raise


def _persist_download_metadata(
    job_id: UUID,
    *,
    title: str,
    duration_s: float,
    thumbnail_url: str | None,
) -> None:
    minutes = max(1, round(duration_s / 60))
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None or job.status in {"completed", "failed"}:
            return
        job.title = title
        job.duration_s = duration_s
        job.thumbnail_url = thumbnail_url
        job.progress_message = f"Downloading · {title} · {minutes} min"
        job.updated_at = datetime.now(UTC)


def _select_whisper_key(user_id: UUID) -> str:
    with session_scope() as session:
        row = session.scalar(select(ApiKey).where(ApiKey.user_id == user_id))
        if row is not None:
            host = (urlsplit(row.base_url).hostname or "").lower()
            if host == "api.openai.com":
                return decrypt_api_key(row.encrypted_key)

    fallback = settings.openai_api_key.strip()
    if fallback:
        return fallback

    raise TranscriptionFailed(
        "Transcription needs an OpenAI Whisper key on the server, or an OpenAI key saved in "
        "your account. Other providers can score clips but cannot transcribe in this build.",
        error_code="transcribe_failed",
    )


def build_transcriber(
    api_key: str,
    *,
    language: str,
    on_chunk: Callable[[int, int], None] | None = None,
) -> Transcriber:
    return WhisperApiTranscriber(api_key, language=language, on_chunk=on_chunk)


def run_pipeline(job_id: str, lock_token: str) -> None:
    uid = UUID(str(job_id))
    with session_scope() as session:
        job = session.get(Job, uid)
        if job is None or job.status in {"completed", "failed"}:
            return
        user_id = job.user_id
        source_url = job.source_url
        stored_duration_s = job.duration_s
        if ORDER.index("downloading") > ORDER.index(job.status):
            prev_status = job.status
            transition_job(session, job, "downloading", "Downloading the VOD")
            log.info(
                "job_transition",
                job_id=str(uid),
                **{"from": prev_status, "to": "downloading"},
            )

    refresh_job_lock(uid, lock_token)
    job_root = ensure_job_dirs(uid)
    target_vod = vod_path(uid)

    should_download = True
    if target_vod.exists() and stored_duration_s is not None:
        existing_duration = probe_media_duration(target_vod)
        if existing_duration is not None and abs(existing_duration - stored_duration_s) <= 2.0:
            should_download = False

    if should_download:
        result = probe_and_download(
            source_url,
            job_root,
            on_metadata=lambda title, duration_s, thumbnail_url: _persist_download_metadata(
                uid,
                title=title,
                duration_s=duration_s,
                thumbnail_url=thumbnail_url,
            ),
        )
        _persist_download_metadata(
            uid,
            title=result.title,
            duration_s=result.duration_s,
            thumbnail_url=result.thumbnail_url,
        )

    t_path = transcript_path(uid)
    words = load_transcript(t_path)
    if words is None:
        with session_scope() as session:
            job = session.get(Job, uid)
            if job is None or job.status in {"completed", "failed"}:
                return
            if ORDER.index("transcribing") > ORDER.index(job.status):
                prev_status = job.status
                transition_job(session, job, "transcribing", "Transcribing speech")
                log.info(
                    "job_transition",
                    job_id=str(uid),
                    **{"from": prev_status, "to": "transcribing"},
                )

        refresh_job_lock(uid, lock_token)

        def _on_chunk(chunk_num: int, total_chunks: int) -> None:
            set_progress(uid, f"Transcribing speech ({chunk_num}/{total_chunks})")
            refresh_job_lock(uid, lock_token)

        whisper_key = _select_whisper_key(user_id)
        try:
            push_secret(whisper_key)
            transcriber = build_transcriber(
                whisper_key,
                language=settings.whisper_language,
                on_chunk=_on_chunk,
            )
            words = transcriber.transcribe(target_vod)
        finally:
            del whisper_key

        save_transcript(t_path, words)
        chunk_count = getattr(transcriber, "last_chunk_count", 0)
        log.info(
            "transcribe_done",
            job_id=str(uid),
            word_count=len(words),
            chunks=chunk_count,
        )

    if STUB_AFTER_TRANSCRIBE:
        raise ClipForgeError(
            "internal",
            "Transcription finished. Later stages are not included in this build yet.",
        )


@celery_app.task(name="process_job", max_retries=0)
def process_job(job_id: str) -> None:
    decision, lock_token = begin_attempt(job_id)
    if decision == "skip":
        return

    stop_event: threading.Event | None = None
    refresh_thread: threading.Thread | None = None
    with secret_boundary():
        try:
            if decision == "stop":
                return
            assert lock_token is not None
            stop_event, refresh_thread = start_lock_refresher(job_id, lock_token)
            run_pipeline(job_id, lock_token)
        except SoftTimeLimitExceeded:
            fail_job(job_id, "timeout", "This job exceeded the 45 minute limit and was stopped.")
        except ClipForgeError as exc:
            fail_job(job_id, exc.error_code, exc.user_message)
        except Exception:
            log.exception("job_crashed", job_id=job_id)
            fail_job(job_id, "internal", "Something went wrong while processing this VOD.")
        finally:
            if stop_event is not None and refresh_thread is not None:
                stop_event.set()
                refresh_thread.join(timeout=5.0)
            try:
                if (
                    lock_token is not None
                    and is_terminal(job_id)
                    and holds_job_lock(job_id, lock_token)
                ):
                    cleanup_raw(job_id)
            finally:
                release_job_lock(job_id, lock_token)
