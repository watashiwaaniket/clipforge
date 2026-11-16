import shutil
from pathlib import Path
from uuid import UUID

from app.config import settings


def resolve_under_storage(relative: str | Path) -> Path:
    rel_path = Path(relative)
    if rel_path.is_absolute():
        raise ValueError("Storage path must be relative.")
    base = Path(settings.storage_dir).resolve()
    target = (base / rel_path).resolve()
    if target == base or not target.is_relative_to(base):
        raise ValueError("Storage path escapes STORAGE_DIR.")
    return target


def job_dir(job_id: UUID | str) -> Path:
    uid = UUID(str(job_id))
    return resolve_under_storage(f"jobs/{uid}")


def vod_path(job_id: UUID | str) -> Path:
    return job_dir(job_id) / "vod.mp4"


def audio_path(job_id: UUID | str) -> Path:
    return job_dir(job_id) / "audio.wav"


def chunks_dir(job_id: UUID | str) -> Path:
    return job_dir(job_id) / "chunks"


def transcript_path(job_id: UUID | str) -> Path:
    return job_dir(job_id) / "transcript.json"


def windows_path(job_id: UUID | str) -> Path:
    return job_dir(job_id) / "windows.json"


def work_dir(job_id: UUID | str) -> Path:
    return job_dir(job_id) / "work"


def clips_dir(job_id: UUID | str) -> Path:
    return job_dir(job_id) / "clips"


def clip_relative_path(job_id: UUID | str, clip_id: UUID | str) -> str:
    jid = UUID(str(job_id))
    cid = UUID(str(clip_id))
    return f"jobs/{jid}/clips/{cid}.mp4"


def ensure_job_dirs(job_id: UUID | str) -> Path:
    root = job_dir(job_id)
    root.mkdir(parents=True, exist_ok=True)
    work_dir(job_id).mkdir(parents=True, exist_ok=True)
    clips_dir(job_id).mkdir(parents=True, exist_ok=True)
    return root


def has_enough_free_disk() -> bool:
    base = Path(settings.storage_dir)
    base.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(base).free >= settings.min_free_disk_bytes


def cleanup_raw(job_id: UUID | str) -> None:
    # PR 9 replaces this no-op with deletion of raw media files.
    return
