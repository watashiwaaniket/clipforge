import os
import socket
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import redis
import structlog

from app.config import settings
from app.db import session_scope
from app.errors import ClipForgeError
from app.models import Job
from app.services.downloader import probe_and_download, probe_media_duration
from app.services.jobs import ORDER, fail_job, is_terminal, transition_job
from app.services.storage import cleanup_raw, ensure_job_dirs, vod_path
from app.workers.celery_app import celery_app

log = structlog.get_logger()

# PHASE 1 ONLY. Deleted in phase 2 when transcribe is real.
STUB_AFTER_DOWNLOAD = True

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


def run_pipeline(job_id: str, lock_token: str) -> None:
    uid = UUID(str(job_id))
    with session_scope() as session:
        job = session.get(Job, uid)
        if job is None or job.status in {"completed", "failed"}:
            return
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

    if STUB_AFTER_DOWNLOAD:
        raise ClipForgeError(
            "internal",
            "Download finished. Later stages are not included in this build yet.",
        )


@celery_app.task(name="process_job", max_retries=0)
def process_job(job_id: str) -> None:
    decision, lock_token = begin_attempt(job_id)
    if decision == "skip":
        return

    stop_event: threading.Event | None = None
    refresh_thread: threading.Thread | None = None
    try:
        if decision == "stop":
            return
        assert lock_token is not None
        stop_event, refresh_thread = start_lock_refresher(job_id, lock_token)
        run_pipeline(job_id, lock_token)
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
