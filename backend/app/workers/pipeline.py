import base64
import json
import os
import shutil
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import redis
import structlog
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import delete, select

from app.config import settings
from app.db import session_scope
from app.errors import ClipForgeError, TranscriptionFailed
from app.logging import push_secret, secret_boundary
from app.models import ApiKey, Clip, Job
from app.security import decrypt_api_key
from app.services.downloader import probe_and_download, probe_media_duration
from app.services.jobs import (
    ACTIVE_JOB_STATUSES,
    ORDER,
    fail_job,
    is_terminal,
    set_progress,
    transition_job,
)
from app.services.scorer import (
    ScoredWindow,
    cancel_score_pool,
    load_windows,
    save_windows,
    score_transcript,
)
from app.services.storage import (
    cleanup_raw,
    clip_relative_path,
    clips_dir,
    ensure_job_dirs,
    job_dir,
    transcript_path,
    vod_path,
    windows_path,
    work_dir,
)
from app.services.transcribe import (
    Transcriber,
    WhisperApiTranscriber,
    Word,
    load_transcript,
    save_transcript,
)
from app.services.video import (
    VideoDimensions,
    clip_error_message,
    clip_error_stderr,
    probe_video_dimensions,
    render_clip,
)
from app.workers.celery_app import celery_app

log = structlog.get_logger()

NO_WINDOWS_MESSAGE = "No clip-worthy moments in this VOD."
ALL_CLIPS_FAILED_MESSAGE = "Finished, but every clip failed to export."

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


def run_scoring(
    words: list[Word],
    *,
    duration_s: float,
    api_key: str,
    base_url: str,
    model: str,
    provider: str,
    max_clips: int,
    job_id: str,
    on_chunk: Callable[[int, int], None] | None = None,
) -> list[ScoredWindow]:
    return score_transcript(
        words,
        duration_s=duration_s,
        api_key=api_key,
        base_url=base_url,
        model=model,
        provider=provider,
        max_clips=max_clips,
        job_id=job_id,
        on_chunk=on_chunk,
    )


def _advance_job(job_id: UUID, target_status: str, message: str) -> bool:
    """Move the job forward when needed. False when the row is gone or already terminal."""
    with session_scope() as session:
        job = session.get(Job, job_id, with_for_update=True)
        if job is None or job.status in {"completed", "failed"}:
            return False
        if ORDER.index(target_status) > ORDER.index(job.status):
            prev_status = job.status
            transition_job(session, job, target_status, message)
            log.info(
                "job_transition",
                job_id=str(job_id),
                **{"from": prev_status, "to": target_status},
            )
    return True


def _reset_clip_outputs(job_id: UUID) -> None:
    """Drop clip rows and files from an earlier attempt so a retry does not duplicate them."""
    with session_scope() as session:
        session.execute(delete(Clip).where(Clip.job_id == job_id))
    for directory in (clips_dir(job_id), work_dir(job_id)):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)


def _insert_clip(
    job_id: UUID,
    clip_id: UUID,
    window: ScoredWindow,
    *,
    status: str,
    caption_status: str,
    file_path: str | None,
    error: str | None,
) -> None:
    with session_scope() as session:
        session.add(
            Clip(
                id=clip_id,
                job_id=job_id,
                start_s=window.start_s,
                end_s=window.end_s,
                score=window.score,
                title=window.title,
                hook=window.hook,
                score_source=window.score_source,
                file_path=file_path,
                caption_status=caption_status,
                status=status,
                error=error,
            )
        )


def run_clipping(
    job_id: UUID,
    lock_token: str,
    *,
    vod: Path,
    windows: list[ScoredWindow],
    words: list[Word],
    duration_s: float | None,
) -> None:
    _reset_clip_outputs(job_id)

    if not windows:
        _advance_job(job_id, "completed", NO_WINDOWS_MESSAGE)
        log.info("clip_done", job_id=str(job_id), ready=0, failed=0)
        return

    total = len(windows)
    if not _advance_job(job_id, "clipping", f"Cutting clip 1 of {total}"):
        return
    refresh_job_lock(job_id, lock_token)

    dimensions: VideoDimensions | None = None
    probe_error: Exception | None = None
    try:
        dimensions = probe_video_dimensions(vod)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        probe_error = exc
        log.warning("clip_probe_failed", job_id=str(job_id), stderr=clip_error_stderr(exc))

    clip_work_dir = work_dir(job_id)
    clip_out_dir = clips_dir(job_id)
    ready = 0
    for index, window in enumerate(windows, start=1):
        set_progress(job_id, f"Cutting clip {index} of {total}")
        refresh_job_lock(job_id, lock_token)
        clip_id = uuid4()
        try:
            if probe_error is not None:
                raise probe_error
            assert dimensions is not None
            render_clip(
                vod,
                window,
                words,
                clip_work_dir,
                clip_out_dir / f"{clip_id}.mp4",
                clip_id=clip_id,
                dimensions=dimensions,
                duration_s=duration_s,
            )
        except SoftTimeLimitExceeded:
            # Celery's soft limit is an Exception subclass; it must reach process_job.
            raise
        except Exception as exc:
            log.exception(
                "clip_failed",
                job_id=str(job_id),
                clip_index=index,
                stderr=clip_error_stderr(exc),
            )
            _insert_clip(
                job_id,
                clip_id,
                window,
                status="failed",
                caption_status="failed",
                file_path=None,
                error=clip_error_message(exc),
            )
            continue

        _insert_clip(
            job_id,
            clip_id,
            window,
            status="ready",
            caption_status="burned",
            file_path=clip_relative_path(job_id, clip_id),
            error=None,
        )
        ready += 1

    message = f"Ready · {ready} clips" if ready else ALL_CLIPS_FAILED_MESSAGE
    _advance_job(job_id, "completed", message)
    log.info("clip_done", job_id=str(job_id), ready=ready, failed=total - ready)


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
        stored_duration_s = result.duration_s
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

    w_path = windows_path(uid)
    windows = load_windows(w_path)
    if windows is None:
        with session_scope() as session:
            job = session.get(Job, uid)
            if job is None or job.status in {"completed", "failed"}:
                return
            if ORDER.index("scoring") > ORDER.index(job.status):
                prev_status = job.status
                transition_job(session, job, "scoring", "Scoring transcript for highlights")
                log.info(
                    "job_transition",
                    job_id=str(uid),
                    **{"from": prev_status, "to": "scoring"},
                )

        refresh_job_lock(uid, lock_token)

        with session_scope() as session:
            key_row = session.scalar(select(ApiKey).where(ApiKey.user_id == user_id))
            if key_row is None:
                raise ClipForgeError("internal", "Add an API key before generating clips.")
            provider = key_row.provider
            base_url = key_row.base_url
            model = key_row.model
            encrypted_key = key_row.encrypted_key

        score_token = decrypt_api_key(encrypted_key)
        try:
            push_secret(score_token)
            scoring_duration = stored_duration_s
            if scoring_duration is None:
                scoring_duration = probe_media_duration(target_vod) or words[-1].end

            def _on_score_chunk(done: int, total: int) -> None:
                set_progress(uid, f"Scoring highlights ({done}/{total})")
                refresh_job_lock(uid, lock_token)

            windows = run_scoring(
                words,
                duration_s=scoring_duration,
                api_key=score_token,
                base_url=base_url,
                model=model,
                provider=provider,
                max_clips=settings.max_clips_per_job,
                job_id=str(uid),
                on_chunk=_on_score_chunk,
            )
        finally:
            del score_token

        save_windows(w_path, windows)
        log.info(
            "score_done",
            job_id=str(uid),
            window_count=len(windows),
        )

    media_duration_s = stored_duration_s
    if media_duration_s is None:
        media_duration_s = probe_media_duration(target_vod)
    run_clipping(
        uid,
        lock_token,
        vod=target_vod,
        windows=windows,
        words=words,
        duration_s=media_duration_s,
    )


def _safe_cleanup_raw(job_id: UUID | str) -> None:
    try:
        cleanup_raw(job_id)
    except Exception:
        log.exception("cleanup_failed", job_id=str(job_id))


def _has_raw_artifacts(job_id: UUID | str) -> bool:
    root = job_dir(job_id)
    if not root.exists():
        return False
    return (
        any(root.glob("vod.*"))
        or (root / "audio.wav").exists()
        or (root / "chunks").exists()
        or transcript_path(job_id).exists()
        or windows_path(job_id).exists()
        or work_dir(job_id).exists()
    )


def _envelope_matches_process_job(raw: bytes | str, target_job_id: str) -> bool:
    try:
        text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw
        parsed = json.loads(text)
    except Exception:
        return False

    if isinstance(parsed, list):
        if not parsed:
            return False
        first = parsed[0]
        if isinstance(first, str):
            try:
                parsed = json.loads(first)
            except Exception:
                return False
        else:
            parsed = first

    if not isinstance(parsed, dict):
        return False

    headers = parsed.get("headers")
    task_name = headers.get("task") if isinstance(headers, dict) else None
    if task_name is None:
        task_name = parsed.get("task")
    if task_name != "process_job":
        return False

    direct_args = parsed.get("args")
    if isinstance(direct_args, list) and direct_args:
        return str(direct_args[0]) == target_job_id

    body = parsed.get("body")
    if body is None:
        return False

    properties = parsed.get("properties")
    body_encoding = properties.get("body_encoding") if isinstance(properties, dict) else None
    if isinstance(body, str):
        try:
            if body_encoding == "base64":
                decoded_bytes = base64.b64decode(body)
                body = json.loads(decoded_bytes.decode("utf-8"))
            else:
                body = json.loads(body)
        except Exception:
            return False

    if isinstance(body, list) and body:
        first_elem = body[0]
        if isinstance(first_elem, list) and first_elem:
            return str(first_elem[0]) == target_job_id
    elif isinstance(body, dict):
        args = body.get("args")
        if isinstance(args, list) and args:
            return str(args[0]) == target_job_id

    return False


def is_job_in_broker(redis_client: redis.Redis, job_id: UUID | str) -> bool:
    target_id = str(job_id)
    if redis_client.exists(_job_lock_key(target_id)):
        return True

    for raw in redis_client.hvals("unacked"):
        if _envelope_matches_process_job(raw, target_id):
            return True

    for raw in redis_client.lrange("celery", 0, -1):
        if _envelope_matches_process_job(raw, target_id):
            return True

    return False


ENQUEUE_GRACE_SECONDS = 180
REDRIVER_TTL_SECONDS = 86_400
REAPER_RECHECK_SLEEP_S = 1.0


@celery_app.task(name="reap_stale_jobs")
def reap_stale_jobs() -> None:
    now = datetime.now(UTC)
    timeout_cutoff = now - timedelta(seconds=settings.job_timeout_seconds)
    enqueue_cutoff = now - timedelta(seconds=ENQUEUE_GRACE_SECONDS)

    client = _redis_client()
    try:
        # 1. Running-timeout pass
        with session_scope() as session:
            timeout_ids = session.scalars(
                select(Job.id).where(
                    Job.status.in_(ACTIVE_JOB_STATUSES),
                    Job.started_at.is_not(None),
                    Job.started_at < timeout_cutoff,
                )
            ).all()

        for uid in timeout_ids:
            if client.exists(_job_lock_key(uid)):
                continue
            fail_job(
                uid,
                "timeout",
                "This job exceeded the 45 minute limit and was stopped.",
            )
            if not client.exists(_job_lock_key(uid)) and is_terminal(uid):
                _safe_cleanup_raw(uid)

        # 2. Lost-enqueue pass (enqueued_at IS NULL)
        with session_scope() as session:
            lost_enqueue_ids = session.scalars(
                select(Job.id).where(
                    Job.status == "queued",
                    Job.attempt == 0,
                    Job.enqueued_at.is_(None),
                    Job.created_at < enqueue_cutoff,
                )
            ).all()

        for uid in lost_enqueue_ids:
            if is_job_in_broker(client, uid):
                continue
            time.sleep(REAPER_RECHECK_SLEEP_S)
            if is_job_in_broker(client, uid):
                continue
            should_fail_enqueue = False
            with session_scope() as session:
                job = session.scalar(
                    select(Job).where(Job.id == uid).with_for_update(skip_locked=True)
                )
                if (
                    job is not None
                    and job.status == "queued"
                    and job.enqueued_at is None
                    and job.attempt == 0
                ):
                    should_fail_enqueue = True
            if should_fail_enqueue:
                fail_job(
                    uid,
                    "enqueue_failed",
                    "Could not start the job. Try again in a moment.",
                )

        # 3. Lost-queued pass (enqueued_at IS NOT NULL)
        with session_scope() as session:
            lost_queued_ids = session.scalars(
                select(Job.id).where(
                    Job.status == "queued",
                    Job.enqueued_at.is_not(None),
                    Job.enqueued_at < enqueue_cutoff,
                )
            ).all()

        for uid in lost_queued_ids:
            if is_job_in_broker(client, uid):
                continue
            time.sleep(REAPER_RECHECK_SLEEP_S)
            if is_job_in_broker(client, uid):
                continue
            should_fail_redrive = False
            with session_scope() as session:
                job = session.scalar(
                    select(Job).where(Job.id == uid).with_for_update(skip_locked=True)
                )
                if job is None or job.status != "queued":
                    continue
                if client.set(f"reaper:redriven:{uid}", "1", nx=True, ex=REDRIVER_TTL_SECONDS):
                    try:
                        process_job.delay(str(uid))
                    except Exception:
                        client.delete(f"reaper:redriven:{uid}")
                        log.exception("reaper_redrive_failed", job_id=str(uid))
                        continue
                    now_ts = datetime.now(UTC)
                    job.enqueued_at = now_ts
                    job.updated_at = now_ts
                    log.info("job_redriven", job_id=str(uid))
                else:
                    should_fail_redrive = True
            if should_fail_redrive:
                fail_job(
                    uid,
                    "enqueue_failed",
                    "Could not start the job. Try again in a moment.",
                )

        # 4. Orphan-media pass
        with session_scope() as session:
            terminal_ids = session.scalars(
                select(Job.id).where(Job.status.in_(("completed", "failed")))
            ).all()

        for uid in terminal_ids:
            if client.exists(_job_lock_key(uid)):
                continue
            try:
                if _has_raw_artifacts(uid):
                    _safe_cleanup_raw(uid)
            except Exception:
                log.exception("cleanup_failed", job_id=str(uid))
    finally:
        client.close()


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
            cancel_score_pool()
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
                    _safe_cleanup_raw(job_id)
            finally:
                release_job_lock(job_id, lock_token)

