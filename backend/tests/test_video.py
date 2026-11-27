import json
import re
import shutil
import subprocess
from collections.abc import Callable, Generator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import redis
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import ApiKey, Clip, Job, User
from app.security import encrypt_api_key, hash_password
from app.services.scorer import ScoredWindow, save_windows
from app.services.storage import resolve_under_storage
from app.services.transcribe import Word, save_transcript
from app.services.video import (
    CLIP_FAILED_MESSAGE,
    CLIP_TIMEOUT_MESSAGE,
    ClipExportError,
    VideoDimensions,
    build_ass,
    build_burn_argv,
    build_cut_argv,
    clip_bounds,
    clip_error_message,
    compute_crop,
    format_ass_time,
    group_cues,
    render_clip,
)
from app.workers.pipeline import process_job


def _window(**overrides: object) -> ScoredWindow:
    base: dict[str, object] = {
        "start_s": 95.0,
        "end_s": 110.0,
        "score": 88,
        "title": "Big moment",
        "hook": "Hook line.",
        "score_source": "llm",
        "word_start": 0,
        "word_end": 0,
    }
    base.update(overrides)
    return ScoredWindow(**base)  # type: ignore[arg-type]


def _style_map(ass: str) -> dict[str, str]:
    lines = ass.splitlines()
    styles_at = lines.index("[V4+ Styles]")
    fields = [f.strip() for f in lines[styles_at + 1].removeprefix("Format:").split(",")]
    values = lines[styles_at + 2].removeprefix("Style:").strip().split(",")
    assert len(fields) == len(values)
    return dict(zip(fields, values, strict=True))


def _dialogues(ass: str) -> list[list[str]]:
    return [
        line.removeprefix("Dialogue: ").split(",", 9)
        for line in ass.splitlines()
        if line.startswith("Dialogue: ")
    ]


# --- ASS ---------------------------------------------------------------------


def test_ass_snapshot_style_and_escaping() -> None:
    words = [
        Word(word="hel{lo", start=100.0, end=100.4),
        Word(word="wor}ld\\N", start=100.5, end=101.0),
    ]
    ass = build_ass(words, _window(word_start=0, word_end=1), ff_ss=94.5)

    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    assert "WrapStyle: 0" in ass
    style = _style_map(ass)
    assert style["Name"] == "Karaoke"
    assert style["Fontname"] == "Liberation Sans"
    assert style["Fontsize"] == "68"
    assert style["MarginV"] == "420"
    assert style["MarginL"] == "80"
    assert style["MarginR"] == "80"
    assert style["Outline"] == "4"
    assert style["Shadow"] == "0"
    assert style["Alignment"] == "2"
    assert style["Bold"] == "-1"
    assert style["BorderStyle"] == "1"
    assert style["PrimaryColour"] == "&H0000FFFF"
    assert style["SecondaryColour"] == "&H00FFFFFF"
    assert style["OutlineColour"] == "&H00000000"

    dialogues = _dialogues(ass)
    assert len(dialogues) == 1
    text = dialogues[0][9]
    assert "\\k" in text
    # Only our own override blocks may contain braces; the spoken text may not.
    spoken = re.sub(r"\{\\k\d+\}", "", text)
    assert "{" not in spoken and "}" not in spoken
    assert "hello" in spoken
    assert "\\N" not in spoken  # transcript backslashes cannot inject a line break


def test_ass_timebase_is_ff_ss_not_vod_or_window_start() -> None:
    words = [Word(word="boom", start=100.0, end=100.5)]
    ass = build_ass(words, _window(start_s=95.0, end_s=110.0), ff_ss=90.0)
    dialogues = _dialogues(ass)
    assert dialogues[0][1] == "0:00:10.00"
    assert dialogues[0][2] == "0:00:10.50"


def test_format_ass_time_rolls_over_hours() -> None:
    assert format_ass_time(0) == "0:00:00.00"
    assert format_ass_time(3725.456) == "1:02:05.46"


def test_karaoke_durations_hold_until_next_word() -> None:
    words = [
        Word(word="one", start=10.0, end=10.2),
        Word(word="two", start=11.0, end=11.3),  # 0.8s pause before this word
    ]
    ass = build_ass(words, _window(start_s=10.0, end_s=12.0, word_end=1), ff_ss=9.5)
    text = _dialogues(ass)[0][9]
    assert text == "{\\k100}one {\\k30}two"


def test_ass_uses_index_range_and_time_overlap() -> None:
    words = [
        Word(word="before", start=1.0, end=1.5),
        Word(word="inside", start=5.0, end=5.5),
        Word(word="late", start=30.0, end=30.5),
    ]
    ass = build_ass(words, _window(start_s=4.0, end_s=10.0, word_start=1, word_end=2), ff_ss=3.5)
    text = "".join(d[9] for d in _dialogues(ass))
    assert "inside" in text
    assert "before" not in text
    assert "late" not in text


def test_cue_grouping_caps_words_and_lines() -> None:
    nine = [Word(word=f"w{i}", start=float(i), end=i + 0.5) for i in range(9)]
    cues = group_cues(nine)
    assert [len(c) for c in cues] == [8, 1]

    long_words = [
        Word(word="abcdefghijklm", start=float(i), end=i + 0.5) for i in range(5)
    ]  # 13 chars: two per 28-char line (27 chars), so a third line flushes after four words
    cues = group_cues(long_words)
    assert [len(c) for c in cues] == [4, 1]

    ass = build_ass(long_words, _window(start_s=0.0, end_s=5.0, word_end=4), ff_ss=0.0)
    first = _dialogues(ass)[0][9]
    assert first.count("\\N") == 1


# --- crop and argv -------------------------------------------------------------


def test_crop_math_nearest_even() -> None:
    assert compute_crop(1920, 1080) == (608, 1080)
    assert compute_crop(1080, 1920) == (1080, 1920)
    assert compute_crop(1920, 1081) == (608, 1080)
    assert compute_crop(1080, 2340) == (1080, 1920)
    assert compute_crop(720, 1600) == (720, 1280)
    assert compute_crop(1081, 2340) == (1080, 1920)
    with pytest.raises(ClipExportError):
        compute_crop(1, 1)


def test_clip_bounds_clamp_to_zero_and_duration() -> None:
    assert clip_bounds(_window(start_s=0.2, end_s=10.0), 100.0) == (0.0, 10.5)
    assert clip_bounds(_window(start_s=50.0, end_s=99.8), 100.0) == (49.5, 100.0)
    assert clip_bounds(_window(start_s=50.0, end_s=60.0), None) == (49.5, 60.5)


def test_pass_argv_shapes() -> None:
    crop_w, crop_h = compute_crop(1920, 1080)
    cut = build_cut_argv(
        Path("/storage/jobs/x/vod.mp4"),
        ff_ss=94.5,
        ff_to=110.5,
        crop_w=crop_w,
        crop_h=crop_h,
        raw_out=Path("/storage/jobs/x/work/c_raw.mp4"),
    )
    joined = " ".join(cut)
    assert "crop=608:1080,scale=1080:1920:flags=lanczos,setsar=1" in joined
    assert "loudnorm=I=-14:TP=-1.5:LRA=11" in joined
    assert cut[cut.index("-pix_fmt") + 1] == "yuv420p"
    assert cut.index("-pix_fmt") < cut.index("-c:v")
    assert "-crf" in cut and cut[cut.index("-crf") + 1] == "20"
    assert cut.index("-ss") < cut.index("-i")
    assert cut.index("-to") < cut.index("-i")

    tall_w, tall_h = compute_crop(1080, 2340)
    assert (tall_w, tall_h) == (1080, 1920)
    tall_cut = build_cut_argv(
        Path("/storage/jobs/x/vod.mp4"),
        ff_ss=0.0,
        ff_to=10.0,
        crop_w=tall_w,
        crop_h=tall_h,
        raw_out=Path("/storage/jobs/x/work/tall_raw.mp4"),
    )
    assert "crop=1080:1920,scale=1080:1920:flags=lanczos,setsar=1" in " ".join(tall_cut)

    clip_id = uuid4()
    burn = build_burn_argv(clip_id)
    assert f"ass={clip_id}.ass" in burn
    assert burn[burn.index("-c:a") + 1] == "copy"
    assert burn[burn.index("-pix_fmt") + 1] == "yuv420p"
    assert burn.index("-pix_fmt") < burn.index("-c:v")
    assert burn[-1] == f"../clips/{clip_id}.mp4"


def test_clip_error_message_mapping() -> None:
    assert clip_error_message(subprocess.TimeoutExpired(["ffmpeg"], 600)) == CLIP_TIMEOUT_MESSAGE
    assert clip_error_message(ClipExportError("x", timed_out=True)) == CLIP_TIMEOUT_MESSAGE
    assert clip_error_message(ClipExportError("x", stderr="/storage/path")) == CLIP_FAILED_MESSAGE
    assert clip_error_message(RuntimeError("boom")) == CLIP_FAILED_MESSAGE


def test_resolve_under_storage_rejects_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    job_id = uuid4()
    with pytest.raises(ValueError):
        resolve_under_storage(f"jobs/{job_id}/clips/../../../../etc/passwd")
    with pytest.raises(ValueError):
        resolve_under_storage("/etc/passwd")
    assert resolve_under_storage(f"jobs/{job_id}/clips/a.mp4").is_relative_to(tmp_path)


# --- render_clip with a fake ffmpeg -------------------------------------------


def _fake_ffmpeg(
    *,
    fail_on: Callable[[int, list[str]], BaseException | None] = lambda _n, _a: None,
    width: int = 1920,
    height: int = 1080,
) -> tuple[Callable[..., subprocess.CompletedProcess], list[tuple[list[str], dict]]]:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append((list(argv), dict(kwargs)))
        if argv[0] == "ffprobe":
            if "stream=width,height" in argv:
                payload = {"streams": [{"width": width, "height": height}]}
            else:
                payload = {"format": {"duration": "180.0"}}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
        assert argv[0] == "ffmpeg", argv
        ffmpeg_calls = sum(1 for a, _ in calls if a[0] == "ffmpeg")
        error = fail_on(ffmpeg_calls, list(argv))
        if error is not None:
            raise error
        cwd = kwargs.get("cwd")
        out = Path(str(cwd)) / argv[-1] if cwd else Path(argv[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake-mp4")
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    return fake_run, calls


def test_render_clip_runs_two_passes_and_cleans_intermediates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_run, calls = _fake_ffmpeg()
    monkeypatch.setattr("app.services.video.subprocess.run", fake_run)
    work = tmp_path / "work"
    clip_id = uuid4()
    out = tmp_path / "clips" / f"{clip_id}.mp4"

    result = render_clip(
        tmp_path / "vod.mp4",
        _window(),
        [Word(word="hey", start=100.0, end=100.4)],
        work,
        out,
        clip_id=clip_id,
        dimensions=VideoDimensions(1920, 1080),
        duration_s=180.0,
    )

    assert result == out and out.read_bytes() == b"fake-mp4"
    assert not (work / f"{clip_id}_raw.mp4").exists()
    assert not (work / f"{clip_id}.ass").exists()
    assert len(calls) == 2
    (cut, cut_kwargs), (burn, burn_kwargs) = calls
    assert "crop=608:1080" in " ".join(cut)
    assert cut[cut.index("-ss") + 1] == "94.500"
    assert burn_kwargs["cwd"] == str(work)
    for kwargs in (cut_kwargs, burn_kwargs):
        assert kwargs["shell"] is False
        assert kwargs["start_new_session"] is False
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 600


def test_render_clip_burn_failure_removes_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_burn(n: int, argv: list[str]) -> BaseException | None:
        if n == 2:
            return subprocess.CalledProcessError(1, argv, stderr=b"libass: error")
        return None

    fake_run, _calls = _fake_ffmpeg(fail_on=fail_burn)
    monkeypatch.setattr("app.services.video.subprocess.run", fake_run)
    work = tmp_path / "work"
    clip_id = uuid4()
    out = tmp_path / "clips" / f"{clip_id}.mp4"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"partial")

    with pytest.raises(ClipExportError) as excinfo:
        render_clip(
            tmp_path / "vod.mp4",
            _window(),
            [Word(word="hey", start=100.0, end=100.4)],
            work,
            out,
            clip_id=clip_id,
            dimensions=VideoDimensions(1920, 1080),
            duration_s=None,
        )
    assert excinfo.value.stderr == "libass: error"
    assert not out.exists()
    assert list(work.iterdir()) == []


def test_render_clip_timeout_is_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run, _ = _fake_ffmpeg(fail_on=lambda _n, argv: subprocess.TimeoutExpired(argv, 600))
    monkeypatch.setattr("app.services.video.subprocess.run", fake_run)
    clip_id = uuid4()
    with pytest.raises(ClipExportError) as excinfo:
        render_clip(
            tmp_path / "vod.mp4",
            _window(),
            [],
            tmp_path / "work",
            tmp_path / "clips" / f"{clip_id}.mp4",
            clip_id=clip_id,
            dimensions=VideoDimensions(1920, 1080),
            duration_s=None,
        )
    assert clip_error_message(excinfo.value) == CLIP_TIMEOUT_MESSAGE


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_render_clip_with_real_ffmpeg(tmp_path: Path) -> None:
    vod = tmp_path / "vod.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=15:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
            str(vod),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    clip_id = uuid4()
    out = tmp_path / "clips" / f"{clip_id}.mp4"
    window = _window(start_s=0.5, end_s=2.0, word_start=0, word_end=1)
    words = [Word(word="real", start=0.6, end=1.0), Word(word="ffmpeg", start=1.1, end=1.8)]

    render_clip(
        vod,
        window,
        words,
        tmp_path / "work",
        out,
        clip_id=clip_id,
        dimensions=VideoDimensions(320, 180),
        duration_s=3.0,
    )

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_entries", "stream=codec_type,width,height", str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    streams = json.loads(probe.stdout)["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in streams)
    assert list((tmp_path / "work").iterdir()) == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_render_clip_tall_10bit_source_produces_square_9_16_yuv420p(tmp_path: Path) -> None:
    vod = tmp_path / "tall.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=360x780:rate=15:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-pix_fmt", "yuv420p10le",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
            str(vod),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    clip_id = uuid4()
    out = tmp_path / "clips" / f"{clip_id}.mp4"
    window = _window(start_s=0.2, end_s=1.5, word_start=0, word_end=0)
    words = [Word(word="tall", start=0.3, end=1.0)]

    render_clip(
        vod,
        window,
        words,
        tmp_path / "work",
        out,
        clip_id=clip_id,
        dimensions=VideoDimensions(360, 780),
        duration_s=2.0,
    )

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_entries",
            "stream=codec_type,width,height,sample_aspect_ratio,display_aspect_ratio,pix_fmt",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    streams = json.loads(probe.stdout)["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["sample_aspect_ratio"] == "1:1"
    assert video["display_aspect_ratio"] == "9:16"
    assert video["pix_fmt"] == "yuv420p"


# --- pipeline clipping stage ---------------------------------------------------


@pytest.fixture
def flush_test_redis() -> Generator[None, None, None]:
    client = redis.Redis.from_url(settings.redis_url)
    try:
        client.flushdb()
        yield
        client.flushdb()
    finally:
        client.close()


def _seed_scored_job(
    tmp_path: Path, windows: list[ScoredWindow], words: list[Word]
) -> str:
    with SessionLocal() as session:
        user = User(
            email=f"clip-{uuid4().hex[:8]}@example.com",
            password_hash=hash_password("correct-horse-battery"),
        )
        session.add(user)
        session.flush()
        session.add(
            ApiKey(
                user_id=user.id,
                provider="openai",
                encrypted_key=encrypt_api_key("sk-clip-test-key-123456"),
                base_url="https://api.openai.com/v1",
                model="gpt-4o-mini",
            )
        )
        job = Job(
            user_id=user.id,
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            platform="youtube",
            status="scoring",
            progress_message="Scoring highlights (1/1)",
            duration_s=180.0,
        )
        session.add(job)
        session.commit()
        job_id = str(job.id)

    job_root = tmp_path / "jobs" / job_id
    job_root.mkdir(parents=True)
    (job_root / "vod.mp4").write_bytes(b"vod")
    save_transcript(job_root / "transcript.json", words)
    save_windows(job_root / "windows.json", windows)
    return job_id


def _clips_for(job_id: str) -> list[Clip]:
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(Clip).where(Clip.job_id == UUID(job_id)).order_by(Clip.start_s)
            )
        )


def _job(job_id: str) -> Job:
    with SessionLocal() as session:
        job = session.get(Job, UUID(job_id))
        assert job is not None
        session.expunge(job)
        return job


TWO_WINDOWS = [
    _window(start_s=10.0, end_s=40.0, score=91, score_source="llm", word_start=0, word_end=0),
    _window(
        start_s=60.0, end_s=90.0, score=70, score_source="heuristic", word_start=1, word_end=1
    ),
]
TWO_WORDS = [Word(word="first", start=12.0, end=12.5), Word(word="second", start=62.0, end=62.5)]


def test_one_failed_clip_does_not_fail_the_job(
    clean_db: None,
    flush_test_redis: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr("app.workers.pipeline.probe_media_duration", lambda _p: 180.0)
    job_id = _seed_scored_job(tmp_path, TWO_WINDOWS, TWO_WORDS)
    fake_run, _ = _fake_ffmpeg(
        fail_on=lambda n, argv: (
            subprocess.CalledProcessError(1, argv, stderr=b"x") if n == 1 else None
        )
    )
    monkeypatch.setattr("app.services.video.subprocess.run", fake_run)

    process_job(job_id)

    job = _job(job_id)
    assert job.status == "completed"
    assert job.error is None
    assert job.progress_message == "Ready · 1 clips"
    assert job.finished_at is not None

    first, second = _clips_for(job_id)
    assert (first.status, first.caption_status, first.file_path) == ("failed", "failed", None)
    assert first.error == CLIP_FAILED_MESSAGE
    assert first.score_source == "llm"
    assert (second.status, second.caption_status, second.error) == ("ready", "burned", None)
    assert second.score_source == "heuristic"
    assert second.file_path == f"jobs/{job_id}/clips/{second.id}.mp4"
    assert (second.start_s, second.end_s, second.score) == (60.0, 90.0, 70)
    assert (tmp_path / second.file_path).read_bytes() == b"fake-mp4"


def test_every_clip_failing_still_completes(
    clean_db: None,
    flush_test_redis: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    job_id = _seed_scored_job(tmp_path, TWO_WINDOWS, TWO_WORDS)
    fake_run, _ = _fake_ffmpeg(fail_on=lambda _n, argv: subprocess.TimeoutExpired(argv, 600))
    monkeypatch.setattr("app.services.video.subprocess.run", fake_run)

    process_job(job_id)

    job = _job(job_id)
    assert job.status == "completed"
    assert job.progress_message == "Finished, but every clip failed to export."
    clips = _clips_for(job_id)
    assert [c.status for c in clips] == ["failed", "failed"]
    assert {c.error for c in clips} == {CLIP_TIMEOUT_MESSAGE}


def test_zero_windows_completes_without_ffmpeg(
    clean_db: None,
    flush_test_redis: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr("app.workers.pipeline.probe_media_duration", lambda _p: 180.0)
    job_id = _seed_scored_job(tmp_path, [], TWO_WORDS)

    def forbid(*_a: object, **_kw: object) -> None:
        raise AssertionError("ffmpeg/ffprobe should not run during zero-window clipping")

    monkeypatch.setattr("app.services.video.subprocess.run", forbid)
    process_job(job_id)

    job = _job(job_id)
    assert job.status == "completed"
    assert job.progress_message == "No clip-worthy moments in this VOD."
    assert _clips_for(job_id) == []


def test_resume_replaces_clip_rows_and_files_from_earlier_attempt(
    clean_db: None,
    flush_test_redis: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    job_id = _seed_scored_job(tmp_path, TWO_WINDOWS[:1], TWO_WORDS)
    stale_id = uuid4()
    with SessionLocal() as session:
        job = session.get(Job, UUID(job_id))
        assert job is not None
        job.status = "clipping"
        session.add(
            Clip(
                id=stale_id,
                job_id=job.id,
                start_s=1.0,
                end_s=2.0,
                score=50,
                title="stale",
                hook="stale",
                score_source="llm",
                file_path=f"jobs/{job_id}/clips/{stale_id}.mp4",
                caption_status="burned",
                status="ready",
            )
        )
        session.commit()
    stale_file = tmp_path / "jobs" / job_id / "clips" / f"{stale_id}.mp4"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_bytes(b"old")
    stale_work = tmp_path / "jobs" / job_id / "work" / "leftover_raw.mp4"
    stale_work.parent.mkdir(parents=True)
    stale_work.write_bytes(b"old")

    fake_run, _ = _fake_ffmpeg()
    seen_raw_during_render: list[bool] = []

    def tracking_run(argv, **kwargs):
        seen_raw_during_render.append(
            (tmp_path / "jobs" / job_id / "vod.mp4").exists()
            and (tmp_path / "jobs" / job_id / "windows.json").exists()
        )
        return fake_run(argv, **kwargs)

    monkeypatch.setattr("app.services.video.subprocess.run", tracking_run)
    process_job(job_id)

    clips = _clips_for(job_id)
    assert len(clips) == 1 and clips[0].id != stale_id
    assert seen_raw_during_render and all(seen_raw_during_render)
    assert not stale_file.exists()
    assert not stale_work.exists()
    assert _job(job_id).status == "completed"
    # Raw artifacts are deleted once the job reaches completed, while the new clip remains.
    assert not (tmp_path / "jobs" / job_id / "vod.mp4").exists()
    assert not (tmp_path / "jobs" / job_id / "windows.json").exists()
    assert (tmp_path / "jobs" / job_id / "clips" / f"{clips[0].id}.mp4").exists()


def test_soft_time_limit_during_clip_is_not_swallowed(
    clean_db: None,
    flush_test_redis: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    job_id = _seed_scored_job(tmp_path, TWO_WINDOWS, TWO_WORDS)
    fake_run, _ = _fake_ffmpeg(fail_on=lambda _n, _argv: SoftTimeLimitExceeded())
    monkeypatch.setattr("app.services.video.subprocess.run", fake_run)

    process_job(job_id)

    job = _job(job_id)
    assert job.status == "failed"
    assert job.error == "This job exceeded the 45 minute limit and was stopped."
    assert _clips_for(job_id) == []
