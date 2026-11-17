import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.logging import redact
from app.services.scorer import ScoredWindow
from app.services.transcribe import Word

FFPROBE_TIMEOUT_S = 30
FFMPEG_TIMEOUT_S = 600
CLIP_SLACK_S = 0.5

OUT_WIDTH = 1080
OUT_HEIGHT = 1920

CUE_MAX_CHARS_PER_LINE = 28
CUE_MAX_LINES = 2
CUE_MAX_WORDS = 8

CLIP_TIMEOUT_MESSAGE = "This clip took too long to export."
CLIP_FAILED_MESSAGE = "This clip could not be exported."


class ClipExportError(Exception):
    """Raised by `render_clip` and helpers. Caught per clip by the pipeline."""

    def __init__(self, message: str, *, stderr: str = "", timed_out: bool = False) -> None:
        self.stderr = stderr
        self.timed_out = timed_out
        super().__init__(message)


@dataclass(frozen=True)
class VideoDimensions:
    width: int
    height: int


def clip_error_message(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return CLIP_TIMEOUT_MESSAGE
    if isinstance(exc, ClipExportError) and exc.timed_out:
        return CLIP_TIMEOUT_MESSAGE
    return CLIP_FAILED_MESSAGE


def clip_error_stderr(exc: BaseException) -> str:
    """Redacted ffmpeg stderr for logs only, never for the user string."""
    raw = exc.stderr if isinstance(exc, ClipExportError) else ""
    return redact(raw)[:500]


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run(argv: list[str], *, timeout: int, cwd: Path | None = None, label: str) -> None:
    try:
        subprocess.run(
            argv,
            check=True,
            capture_output=True,
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
            shell=False,
            start_new_session=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClipExportError(
            f"{label} timed out", stderr=_decode(exc.stderr), timed_out=True
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ClipExportError(f"{label} failed", stderr=_decode(exc.stderr)) from exc
    except OSError as exc:
        raise ClipExportError(f"{label} could not start") from exc


# --- probe and crop ---------------------------------------------------------


def build_probe_dimensions_argv(vod: Path) -> list[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "stream=width,height",
        "-select_streams",
        "v:0",
        str(vod),
    ]


def probe_video_dimensions(vod: Path) -> VideoDimensions:
    argv = build_probe_dimensions_argv(vod)
    try:
        proc = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_S,
            shell=False,
            start_new_session=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClipExportError("ffprobe timed out", timed_out=True) from exc
    except (subprocess.SubprocessError, OSError) as exc:
        raise ClipExportError("ffprobe failed") from exc

    try:
        payload = json.loads(proc.stdout or "{}")
        stream = payload["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ClipExportError("ffprobe returned no video stream") from exc
    if width <= 0 or height <= 0:
        raise ClipExportError("bad dimensions")
    return VideoDimensions(width=width, height=height)


def even_floor(value: int) -> int:
    return value if value % 2 == 0 else value - 1


def compute_crop(width: int, height: int) -> tuple[int, int]:
    """Centered 9:16 crop with even sides.

    Height is floored to even. Width is the nearest even of `crop_h * 9 / 16`
    (1080 -> 607.5 -> 608, not the floor-to-even 606), capped at the even source width.
    """
    crop_h = even_floor(height)
    nearest_even = ((int(crop_h * 9 / 16) + 1) // 2) * 2
    crop_w = min(even_floor(width), nearest_even)
    if crop_w < 2 or crop_h < 2:
        raise ClipExportError("bad dimensions")
    return crop_w, crop_h


def clip_bounds(window: ScoredWindow, duration_s: float | None) -> tuple[float, float]:
    ff_ss = max(0.0, window.start_s - CLIP_SLACK_S)
    ff_to = window.end_s + CLIP_SLACK_S
    if duration_s is not None:
        ff_to = min(duration_s, ff_to)
    if ff_to <= ff_ss:
        raise ClipExportError("empty clip range")
    return ff_ss, ff_to


def _fmt_seconds(value: float) -> str:
    return f"{value:.3f}"


def build_cut_argv(
    vod: Path,
    *,
    ff_ss: float,
    ff_to: float,
    crop_w: int,
    crop_h: int,
    raw_out: Path,
) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        _fmt_seconds(ff_ss),
        "-to",
        _fmt_seconds(ff_to),
        "-i",
        str(vod),
        "-vf",
        f"crop={crop_w}:{crop_h},scale={OUT_WIDTH}:{OUT_HEIGHT}:flags=lanczos",
        "-af",
        "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:v",
        "libx264",
        "-crf",
        "20",
        "-preset",
        "fast",
        "-threads",
        "2",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(raw_out),
    ]


def build_burn_argv(clip_id: UUID) -> list[str]:
    """Pass 2 runs with cwd=work/ so the ASS path needs no filter escaping."""
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        f"{clip_id}_raw.mp4",
        "-vf",
        f"ass={clip_id}.ass",
        "-c:a",
        "copy",
        "-c:v",
        "libx264",
        "-crf",
        "20",
        "-preset",
        "fast",
        "-threads",
        "2",
        "-movflags",
        "+faststart",
        f"../clips/{clip_id}.mp4",
    ]


# --- ASS --------------------------------------------------------------------

ASS_STYLE_FIELDS = (
    "Name",
    "Fontname",
    "Fontsize",
    "PrimaryColour",
    "SecondaryColour",
    "OutlineColour",
    "BackColour",
    "Bold",
    "Italic",
    "Underline",
    "StrikeOut",
    "ScaleX",
    "ScaleY",
    "Spacing",
    "Angle",
    "BorderStyle",
    "Outline",
    "Shadow",
    "Alignment",
    "MarginL",
    "MarginR",
    "MarginV",
    "Encoding",
)

ASS_STYLE_VALUES = {
    "Name": "Karaoke",
    "Fontname": "Liberation Sans",
    "Fontsize": "68",
    "PrimaryColour": "&H0000FFFF",
    "SecondaryColour": "&H00FFFFFF",
    "OutlineColour": "&H00000000",
    "BackColour": "&H00000000",
    "Bold": "-1",
    "Italic": "0",
    "Underline": "0",
    "StrikeOut": "0",
    "ScaleX": "100",
    "ScaleY": "100",
    "Spacing": "0",
    "Angle": "0",
    "BorderStyle": "1",
    "Outline": "4",
    "Shadow": "0",
    "Alignment": "2",
    "MarginL": "80",
    "MarginR": "80",
    "MarginV": "420",
    "Encoding": "1",
}


def _ass_header() -> str:
    style = ",".join(ASS_STYLE_VALUES[field] for field in ASS_STYLE_FIELDS)
    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {OUT_WIDTH}",
            f"PlayResY: {OUT_HEIGHT}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: " + ", ".join(ASS_STYLE_FIELDS),
            f"Style: {style}",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )


def format_ass_time(seconds: float) -> str:
    total_cs = max(0, round(seconds * 100))
    hours, rem = divmod(total_cs, 360_000)
    minutes, rem = divmod(rem, 6_000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def escape_ass_text(text: str) -> str:
    """Strip override braces and backslashes so transcript text cannot inject tags."""
    cleaned = text.replace("{", "").replace("}", "").replace("\\", "")
    return " ".join(cleaned.split())


def caption_words(words: list[Word], window: ScoredWindow) -> list[Word]:
    """Words inside the window's index range whose timestamps overlap the window."""
    lo = max(0, window.word_start)
    hi = min(len(words) - 1, window.word_end)
    picked: list[Word] = []
    for index in range(lo, hi + 1):
        word = words[index]
        if word.end < window.start_s or word.start > window.end_s:
            continue
        if not escape_ass_text(word.word):
            continue
        picked.append(word)
    return picked


def _wrap_lines(texts: list[str]) -> list[list[int]]:
    """Greedy-wrap word indices into lines of at most CUE_MAX_CHARS_PER_LINE."""
    lines: list[list[int]] = []
    current: list[int] = []
    current_len = 0
    for index, text in enumerate(texts):
        added = len(text) if not current else current_len + 1 + len(text)
        if current and added > CUE_MAX_CHARS_PER_LINE:
            lines.append(current)
            current = [index]
            current_len = len(text)
        else:
            current.append(index)
            current_len = added
    if current:
        lines.append(current)
    return lines


def group_cues(words: list[Word]) -> list[list[Word]]:
    cues: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        candidate = [*current, word]
        texts = [escape_ass_text(w.word) for w in candidate]
        if current and (
            len(candidate) > CUE_MAX_WORDS or len(_wrap_lines(texts)) > CUE_MAX_LINES
        ):
            cues.append(current)
            current = [word]
        else:
            current = candidate
    if current:
        cues.append(current)
    return cues


def _cue_text(cue: list[Word]) -> str:
    """Karaoke text for one cue.

    Each `\\k` holds its word until the next word starts, so a pause between words
    does not make later words light up early. Times run on a cumulative centisecond
    clock relative to the cue's first word so rounding does not drift.
    """
    texts = [escape_ass_text(w.word) for w in cue]
    lines = _wrap_lines(texts)
    break_before = lines[1][0] if len(lines) > 1 else None

    origin = cue[0].start
    cursor_cs = 0
    parts: list[str] = []
    for index, word in enumerate(cue):
        until = cue[index + 1].start if index + 1 < len(cue) else word.end
        end_cs = max(cursor_cs + 1, round((until - origin) * 100))
        duration_cs = end_cs - cursor_cs
        cursor_cs = end_cs
        token = f"{{\\k{duration_cs}}}{texts[index]}"
        if index == 0:
            parts.append(token)
        elif index == break_before:
            parts.append("\\N" + token)
        else:
            parts.append(" " + token)
    return "".join(parts)


def build_ass(words: list[Word], window: ScoredWindow, ff_ss: float) -> str:
    """Build the karaoke subtitle file.

    Zero on the ASS clock is `ff_ss` (the `-ss` of pass 1), not the VOD start and
    not `window.start_s`.
    """
    events: list[str] = []
    for cue in group_cues(caption_words(words, window)):
        start = max(0.0, cue[0].start - ff_ss)
        end = max(start, cue[-1].end - ff_ss)
        events.append(
            "Dialogue: 0,"
            f"{format_ass_time(start)},{format_ass_time(end)},"
            f"Karaoke,,0,0,0,,{_cue_text(cue)}"
        )
    return _ass_header() + "\n" + "\n".join(events) + ("\n" if events else "")


# --- render -----------------------------------------------------------------


def render_clip(
    vod: Path,
    window: ScoredWindow,
    words: list[Word],
    work_dir: Path,
    out_path: Path,
    *,
    clip_id: UUID,
    dimensions: VideoDimensions,
    duration_s: float | None,
) -> Path:
    """Cut, crop, normalize, and burn captions for one window. Raises on failure.

    The raw cut and the ASS are always removed. The final file is removed too unless
    the burn succeeded, so a retry never sees a partial clip.
    """
    if out_path.name != f"{clip_id}.mp4" or out_path.parent.resolve() != (
        work_dir.parent / "clips"
    ).resolve():
        raise ClipExportError("output path does not match the clip id")

    crop_w, crop_h = compute_crop(dimensions.width, dimensions.height)
    ff_ss, ff_to = clip_bounds(window, duration_s)

    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = work_dir / f"{clip_id}_raw.mp4"
    ass_path = work_dir / f"{clip_id}.ass"

    succeeded = False
    try:
        _run(
            build_cut_argv(
                vod,
                ff_ss=ff_ss,
                ff_to=ff_to,
                crop_w=crop_w,
                crop_h=crop_h,
                raw_out=raw_path,
            ),
            timeout=FFMPEG_TIMEOUT_S,
            label="ffmpeg cut",
        )
        ass_path.write_text(build_ass(words, window, ff_ss), encoding="utf-8")
        _run(build_burn_argv(clip_id), timeout=FFMPEG_TIMEOUT_S, cwd=work_dir, label="ffmpeg burn")
        if not out_path.is_file() or out_path.stat().st_size <= 0:
            raise ClipExportError("ffmpeg burn produced an empty file")
        succeeded = True
        return out_path
    finally:
        raw_path.unlink(missing_ok=True)
        ass_path.unlink(missing_ok=True)
        if not succeeded:
            out_path.unlink(missing_ok=True)
