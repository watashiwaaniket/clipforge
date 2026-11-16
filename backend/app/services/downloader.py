import ipaddress
import json
import math
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings
from app.errors import DownloadFailed, SubscriberOnly, UnsupportedUrl, VodTooLong

log = structlog.get_logger()

_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_PATH_RE = re.compile(r"^/(?:shorts|live)/([A-Za-z0-9_-]{11})$")
_TWITCH_VOD_PATH_RE = re.compile(r"^/videos/([0-9]+)$")

_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "music.youtube.com",
    }
)
_YOUTUBE_SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
_TWITCH_HOSTS = frozenset({"www.twitch.tv", "twitch.tv", "m.twitch.tv"})

_SUB_ONLY_MARKERS = ("subscriber-only", "subscribers only", "sub-only")
_NON_RETRYABLE_DOWNLOAD_MARKERS = (
    "private video",
    "this video is private",
    "video unavailable",
    "has been removed",
    "age-restricted",
    "sign in to confirm your age",
    "not available in your country",
    "geo-restricted",
    "members-only",
    "join this channel to get access",
    "account associated with this video has been terminated",
    "copyright claim",
    "does not exist",
)


class _RetryableYtDlpError(Exception):
    pass


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    platform: Literal["twitch", "youtube"]
    title: str
    duration_s: float
    thumbnail_url: str | None


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def detect_platform(url: str) -> Literal["twitch", "youtube"]:
    raw = url.strip()
    if not raw or any(ch.isspace() for ch in raw):
        raise UnsupportedUrl()

    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https":
        raise UnsupportedUrl()
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise UnsupportedUrl()

    try:
        _ = parsed.port
    except ValueError as exc:
        raise UnsupportedUrl() from exc

    raw_host = parsed.hostname
    if not raw_host:
        raise UnsupportedUrl()
    host = raw_host.lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost") or _is_ip_literal(host):
        raise UnsupportedUrl()

    path = parsed.path[:-1] if parsed.path.endswith("/") else parsed.path

    if host in _YOUTUBE_HOSTS:
        if host in _YOUTUBE_SHORT_HOSTS:
            match = re.fullmatch(r"/([A-Za-z0-9_-]{11})", path)
            if not match:
                raise UnsupportedUrl()
            return "youtube"

        if path == "/watch":
            params = parse_qs(parsed.query, keep_blank_values=False)
            values = params.get("v")
            if not values or len(values) != 1 or not _YOUTUBE_ID_RE.fullmatch(values[0]):
                raise UnsupportedUrl()
            return "youtube"

        if _YOUTUBE_PATH_RE.fullmatch(path):
            return "youtube"

        raise UnsupportedUrl()

    if host in _TWITCH_HOSTS:
        if _TWITCH_VOD_PATH_RE.fullmatch(path):
            return "twitch"
        raise UnsupportedUrl()

    raise UnsupportedUrl()


def _cleanup_partial_vods(job_dir: Path) -> None:
    if not job_dir.exists():
        return
    for pattern in ("vod.*", "*.part", "*.ytdl"):
        for item in job_dir.glob(pattern):
            if item.is_file():
                item.unlink(missing_ok=True)


def _classify_ytdlp_failure(stderr: str, job_dir: Path | None = None) -> None:
    lowered = stderr.lower()
    if any(marker in lowered for marker in _SUB_ONLY_MARKERS):
        if job_dir is not None:
            _cleanup_partial_vods(job_dir)
        raise SubscriberOnly()
    if any(marker in lowered for marker in _NON_RETRYABLE_DOWNLOAD_MARKERS):
        if job_dir is not None:
            _cleanup_partial_vods(job_dir)
        raise DownloadFailed()


def _run_ytdlp_with_retries(
    argv: list[str],
    *,
    timeout: int,
    job_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(_RetryableYtDlpError),
        reraise=True,
    )
    def _invoke() -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
                start_new_session=False,
            )
        except subprocess.TimeoutExpired as exc:
            if job_dir is not None:
                _cleanup_partial_vods(job_dir)
            raise DownloadFailed("The download timed out. Try again.") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr or ""
            _classify_ytdlp_failure(stderr, job_dir=job_dir)
            raise _RetryableYtDlpError(stderr) from exc

    try:
        return _invoke()
    except _RetryableYtDlpError as exc:
        if job_dir is not None:
            _cleanup_partial_vods(job_dir)
        raise DownloadFailed() from exc


def probe_media_duration(media_path: Path) -> float | None:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_entries",
                "format=duration",
                str(media_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
            start_new_session=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    try:
        payload = json.loads(proc.stdout or "{}")
        raw_duration = payload.get("format", {}).get("duration")
        if raw_duration is None:
            return None
        value = float(raw_duration)
        if not math.isfinite(value) or value <= 0:
            return None
        return value
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _sanitize_thumbnail_url(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > 2048:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        return None
    return candidate


def _validate_probe_extractor(
    info: dict[str, object], fallback_platform: Literal["twitch", "youtube"]
) -> Literal["twitch", "youtube"]:
    raw_extractor = str(info.get("extractor_key") or info.get("extractor") or "").strip().lower()
    if not raw_extractor:
        return fallback_platform
    if "youtube" in raw_extractor and "tab" not in raw_extractor:
        return "youtube"
    if "twitch" in raw_extractor and "clip" not in raw_extractor:
        return "twitch"
    raise UnsupportedUrl()


def probe_and_download(
    url: str,
    job_dir: Path,
    *,
    on_metadata: Callable[[str, float, str | None], None] | None = None,
) -> DownloadResult:
    url_platform = detect_platform(url)
    job_dir.mkdir(parents=True, exist_ok=True)

    probe_argv = [
        "yt-dlp",
        "--skip-download",
        "--dump-single-json",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        url,
    ]
    probe_proc = _run_ytdlp_with_retries(probe_argv, timeout=60)

    try:
        info = json.loads(probe_proc.stdout or "")
    except json.JSONDecodeError as exc:
        raise DownloadFailed() from exc
    if not isinstance(info, dict):
        raise DownloadFailed()

    platform = _validate_probe_extractor(info, url_platform)

    if info.get("is_live") is True or info.get("live_status") == "is_live":
        raise DownloadFailed(
            "Live streams aren't supported. Paste the VOD after the stream ends.",
            error_code="live_or_unknown_length",
        )

    raw_duration = info.get("duration")
    if (
        isinstance(raw_duration, bool)
        or not isinstance(raw_duration, int | float)
        or not math.isfinite(float(raw_duration))
        or float(raw_duration) <= 0
    ):
        raise DownloadFailed(
            "Could not read the video length, so it was not downloaded.",
            error_code="live_or_unknown_length",
        )

    probe_duration_s = float(raw_duration)
    max_seconds = float(settings.max_vod_minutes * 60)
    if probe_duration_s > max_seconds:
        raise VodTooLong(f"VOD is too long (max {settings.max_vod_minutes} min in this build).")

    title = str(info.get("title") or "Untitled VOD").strip()[:300] or "Untitled VOD"
    thumbnail_url = _sanitize_thumbnail_url(info.get("thumbnail"))
    if on_metadata is not None:
        on_metadata(title, probe_duration_s, thumbnail_url)

    download_argv = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--newline",
        "--restrict-filenames",
        "--merge-output-format",
        "mp4",
        "--format",
        "bv*[height<=1080]+ba/bv*+ba/b",
        "--max-filesize",
        "8G",
        "--output",
        str(job_dir / "vod.%(ext)s"),
        url,
    ]
    _run_ytdlp_with_retries(download_argv, timeout=2400, job_dir=job_dir)

    vod_mp4 = job_dir / "vod.mp4"
    if not vod_mp4.exists():
        for ext in ("mkv", "webm"):
            alt_file = job_dir / f"vod.{ext}"
            if alt_file.exists():
                try:
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-hide_banner",
                            "-nostdin",
                            "-loglevel",
                            "error",
                            "-y",
                            "-i",
                            str(alt_file),
                            "-c",
                            "copy",
                            str(vod_mp4),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=600,
                        shell=False,
                        start_new_session=False,
                    )
                except (subprocess.SubprocessError, OSError) as exc:
                    _cleanup_partial_vods(job_dir)
                    raise DownloadFailed() from exc
                alt_file.unlink(missing_ok=True)
                break

    if not vod_mp4.exists() or vod_mp4.stat().st_size == 0:
        _cleanup_partial_vods(job_dir)
        raise DownloadFailed()

    container_duration_s = probe_media_duration(vod_mp4)
    final_duration_s = probe_duration_s
    if container_duration_s is not None:
        if container_duration_s > max_seconds:
            _cleanup_partial_vods(job_dir)
            raise VodTooLong(f"VOD is too long (max {settings.max_vod_minutes} min in this build).")
        if abs(container_duration_s - probe_duration_s) > 2.0:
            final_duration_s = container_duration_s

    log.info(
        "download_done",
        job_id=job_dir.name,
        platform=platform,
        duration_s=round(final_duration_s, 2),
        bytes=vod_mp4.stat().st_size,
    )
    return DownloadResult(
        path=vod_mp4,
        platform=platform,
        title=title,
        duration_s=final_duration_s,
        thumbnail_url=thumbnail_url,
    )
