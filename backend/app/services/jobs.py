from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.orm import Session

from app.db import session_scope
from app.errors import InvalidTransition
from app.logging import redact
from app.models import Job

log = structlog.get_logger()

ORDER = ("queued", "downloading", "transcribing", "scoring", "clipping", "completed")
ACTIVE_JOB_STATUSES = ("queued", "downloading", "transcribing", "scoring", "clipping")


def transition_job(session: Session, job: Job, new_status: str, message: str) -> None:
    if new_status == "failed" or new_status not in ORDER:
        raise InvalidTransition(job.status, new_status)
    if job.status in {"completed", "failed"}:
        raise InvalidTransition(job.status, new_status)
    if ORDER.index(new_status) <= ORDER.index(job.status):
        raise InvalidTransition(job.status, new_status)

    job.status = new_status
    job.progress_message = message
    job.updated_at = datetime.now(UTC)
    if new_status == "downloading" and job.started_at is None:
        job.started_at = job.updated_at
    if new_status == "completed":
        job.finished_at = job.updated_at


def set_progress(job_id: UUID | str, message: str) -> None:
    uid = UUID(str(job_id))
    with session_scope() as session:
        job = session.get(Job, uid)
        if job is None or job.status in {"completed", "failed"}:
            return
        job.progress_message = message
        job.updated_at = datetime.now(UTC)


def is_terminal(job_id: UUID | str) -> bool:
    uid = UUID(str(job_id))
    with session_scope() as session:
        job = session.get(Job, uid)
        return job is not None and job.status in {"completed", "failed"}


def fail_job(job_id: UUID | str, error_code: str, user_message: str) -> None:
    uid = UUID(str(job_id))
    with session_scope() as session:
        job = session.get(Job, uid, with_for_update=True)
        if job is None or job.status in {"completed", "failed"}:
            return
        now = datetime.now(UTC)
        job.status = "failed"
        job.error = redact(user_message)[:500]
        job.finished_at = now
        job.updated_at = now
        platform = job.platform
        origin = job.started_at or job.created_at
        elapsed_s = round((now - origin).total_seconds(), 2) if origin else 0.0
    log.info(
        "job_failed",
        job_id=str(uid),
        error_code=error_code,
        platform=platform,
        elapsed_s=elapsed_s,
    )
