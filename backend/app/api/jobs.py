from contextlib import aclosing
from datetime import UTC, datetime
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import settings
from app.db import get_session, session_scope
from app.errors import UnsupportedUrl
from app.models import ApiKey, Clip, Job, User
from app.schemas import (
    ClipResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobDetailResponse,
    JobListItemResponse,
)
from app.services.downloader import detect_platform
from app.services.jobs import ACTIVE_JOB_STATUSES, fail_job
from app.services.storage import has_enough_free_disk
from app.workers.pipeline import process_job

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])
log = structlog.get_logger()

MAX_CREATE_JOB_BODY_BYTES = 2048


async def enforce_max_job_body_size(request: Request) -> None:
    raw_len = request.headers.get("content-length")
    if raw_len is not None:
        try:
            if int(raw_len) > MAX_CREATE_JOB_BODY_BYTES:
                raise HTTPException(
                    status_code=422,
                    detail="Request body too large.",
                )
        except ValueError:
            pass

    if hasattr(request, "_body"):
        if len(request._body) > MAX_CREATE_JOB_BODY_BYTES:
            raise HTTPException(
                status_code=422,
                detail="Request body too large.",
            )
        return

    total = 0
    chunks: list[bytes] = []
    async with aclosing(request.stream()) as stream:
        async for chunk in stream:
            total += len(chunk)
            if total > MAX_CREATE_JOB_BODY_BYTES:
                raise HTTPException(
                    status_code=422,
                    detail="Request body too large.",
                )
            chunks.append(chunk)
    request._body = b"".join(chunks)


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobCreateResponse,
    dependencies=[Depends(enforce_max_job_body_size)],
)
def create_job(
    body: JobCreateRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> JobCreateResponse:
    clean_url = body.url.strip()
    try:
        platform = detect_platform(clean_url)
    except UnsupportedUrl as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.user_message,
        ) from exc

    has_key = session.scalar(select(ApiKey.id).where(ApiKey.user_id == user.id))
    if has_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add an API key before generating clips.",
        )

    if not has_enough_free_disk():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The server cannot store this VOD right now. Try again later.",
        )

    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:uid))"), {"uid": str(user.id)})

    active_count = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.user_id == user.id, Job.status.in_(ACTIVE_JOB_STATUSES))
    )
    if (active_count or 0) >= settings.max_concurrent_jobs:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"You already have {settings.max_concurrent_jobs} jobs generating. "
            "Wait for one to finish.",
        )

    today_utc = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_count = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.user_id == user.id, Job.created_at >= today_utc)
    )
    if (daily_count or 0) >= settings.max_jobs_per_day:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily limit reached ({settings.max_jobs_per_day} jobs). Try again tomorrow.",
        )

    job = Job(
        user_id=user.id,
        source_url=clean_url,
        platform=platform,
        status="queued",
        progress_message="Queued",
        enqueued_at=None,
    )
    session.add(job)
    session.commit()
    job_id = job.id

    try:
        process_job.delay(str(job_id))
    except Exception as exc:
        log.exception("enqueue_failed", job_id=str(job_id))
        fail_job(job_id, "enqueue_failed", "Could not start the job. Try again in a moment.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not start the job. Try again in a moment.",
        ) from exc

    with session_scope() as update_session:
        row = update_session.get(Job, job_id)
        if row is not None:
            row.enqueued_at = datetime.now(UTC)

    log.info("job_enqueued", job_id=str(job_id), user_id=str(user.id), platform=platform)
    return JobCreateResponse(job_id=job_id)


@router.get("", response_model=list[JobListItemResponse])
def list_jobs(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[JobListItemResponse]:
    rows = session.scalars(
        select(Job)
        .where(Job.user_id == user.id)
        .order_by(Job.created_at.desc())
        .limit(50)
    ).all()
    return [
        JobListItemResponse(
            id=row.id,
            source_url=row.source_url,
            status=row.status,
            progress_message=row.progress_message,
            error=row.error,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/{job_id}", response_model=JobDetailResponse)
def get_job_detail(
    job_id: UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> JobDetailResponse:
    job = session.scalar(select(Job).where(Job.id == job_id, Job.user_id == user.id))
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found.",
        )

    clips = session.scalars(
        select(Clip)
        .where(Clip.job_id == job.id)
        .order_by(Clip.score.desc(), Clip.start_s.asc())
    ).all()

    return JobDetailResponse(
        id=job.id,
        source_url=job.source_url,
        platform=job.platform,
        title=job.title,
        duration_s=job.duration_s,
        thumbnail_url=job.thumbnail_url,
        status=job.status,
        progress_message=job.progress_message,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        clips=[
            ClipResponse(
                id=clip.id,
                start_s=clip.start_s,
                end_s=clip.end_s,
                score=clip.score,
                title=clip.title,
                hook=clip.hook,
                score_source=clip.score_source,
                caption_status=clip.caption_status,
                status=clip.status,
                error=clip.error,
                created_at=clip.created_at,
            )
            for clip in clips
        ],
    )
