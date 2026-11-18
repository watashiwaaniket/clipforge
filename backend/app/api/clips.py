import re
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db import get_session
from app.models import Clip, Job, User
from app.services.storage import clip_relative_path, resolve_under_storage

router = APIRouter(prefix="/api/v1/clips", tags=["clips"])
log = structlog.get_logger()

_SLUG_STRIP = re.compile(r"[^A-Za-z0-9_-]+")
_SLUG_MAX = 60


def download_slug(title: str | None) -> str:
    """ASCII `[A-Za-z0-9_-]{1,60}` from the title, else `clip`.

    Every character outside the allowed set (including CR, LF, quotes, and
    non-ASCII) collapses to `-`, so the header value cannot be broken.
    """
    if not title:
        return "clip"
    slug = _SLUG_STRIP.sub("-", title).strip("-_")[:_SLUG_MAX].strip("-_")
    return slug or "clip"


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")


@router.get("/{clip_id}/video")
def get_clip_video(
    clip_id: UUID,
    download: str | None = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> FileResponse:
    clip = session.scalar(
        select(Clip)
        .join(Job, Clip.job_id == Job.id)
        .where(Clip.id == clip_id, Job.user_id == user.id)
    )
    if clip is None or clip.status != "ready" or not clip.file_path:
        raise _not_found()

    # Only ever serve the clip's own output path, even if the row was tampered with.
    if clip.file_path != clip_relative_path(clip.job_id, clip.id):
        log.warning("clip_path_mismatch", clip_id=str(clip.id))
        raise _not_found()
    try:
        path = resolve_under_storage(clip.file_path)
    except ValueError:
        raise _not_found() from None
    if not path.is_file():
        raise _not_found()

    if download == "1":
        disposition = f'attachment; filename="clipforge-{download_slug(clip.title)}.mp4"'
    else:
        disposition = 'inline; filename="clip.mp4"'

    return FileResponse(
        path,
        media_type="video/mp4",
        headers={
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        },
    )
