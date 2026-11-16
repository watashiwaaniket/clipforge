import json
import subprocess
from pathlib import Path

import pytest

from app.errors import DownloadFailed, SubscriberOnly, UnsupportedUrl, VodTooLong
from app.services.downloader import detect_platform, probe_and_download


def test_detect_platform_accept_and_reject_table() -> None:
    accepted: list[tuple[str, str]] = [
        ("https://youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "youtube"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
        ("https://youtu.be/dQw4w9WgXcQ/", "youtube"),
        ("https://www.youtu.be/dQw4w9WgXcQ?si=abc", "youtube"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "youtube"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ/", "youtube"),
        ("https://www.twitch.tv/videos/1234567890", "twitch"),
        ("https://twitch.tv/videos/1234567890/", "twitch"),
        ("https://m.twitch.tv/videos/1234567890?t=1h2m3s", "twitch"),
    ]
    for url, expected in accepted:
        assert detect_platform(url) == expected, url

    rejected = [
        "javascript:alert(1)",
        "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://user:pass@youtube.com/watch?v=dQw4w9WgXcQ",
        "https://127.0.0.1/watch?v=dQw4w9WgXcQ",
        "https://localhost/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/playlist?list=PL12345",
        "https://www.youtube.com/@streamer",
        "https://www.youtube.com/feed/subscriptions",
        "https://www.youtube.com/watch?v=short",
        "https://clips.twitch.tv/FunnyClipName",
        "https://www.twitch.tv/streamer/clip/FunnyClipName",
        "https://www.twitch.tv/streamer",
        "https://vimeo.com/123456",
        "",
    ]
    for url in rejected:
        with pytest.raises(UnsupportedUrl):
            detect_platform(url)


def test_probe_too_long_rejects_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        assert kwargs.get("shell") is False
        assert kwargs.get("start_new_session") is False
        if "--dump-single-json" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "duration": 241 * 60,
                        "title": "Too long VOD",
                        "extractor_key": "Youtube",
                    }
                ),
                stderr="",
            )
        raise AssertionError("Download should not run when probe duration exceeds cap")

    monkeypatch.setattr("app.services.downloader.subprocess.run", fake_run)
    with pytest.raises(VodTooLong, match="max 240 min"):
        probe_and_download("https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path / "job")

    assert len(calls) == 1
    assert "--skip-download" in calls[0]


def test_probe_live_stream_rejects_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"is_live": True, "live_status": "is_live", "title": "Live"}),
            stderr="",
        )

    monkeypatch.setattr("app.services.downloader.subprocess.run", fake_run)
    with pytest.raises(DownloadFailed) as exc_info:
        probe_and_download("https://www.twitch.tv/videos/1234567890", tmp_path / "job")

    assert exc_info.value.error_code == "live_or_unknown_length"
    assert "Live streams aren't supported" in exc_info.value.user_message
    assert len(calls) == 1


def test_subscriber_only_cleans_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir(parents=True)

    def fake_run(argv, **kwargs):
        if "--dump-single-json" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "duration": 120,
                        "title": "Sub VOD",
                        "extractor_key": "TwitchVod",
                    }
                ),
                stderr="",
            )
        (job_dir / "vod.mp4.part").write_bytes(b"partial")
        raise subprocess.CalledProcessError(
            1, argv, stderr="ERROR: This VOD is subscriber-only"
        )

    monkeypatch.setattr("app.services.downloader.subprocess.run", fake_run)
    with pytest.raises(SubscriberOnly):
        probe_and_download("https://www.twitch.tv/videos/1234567890", job_dir)

    assert list(job_dir.glob("*")) == []


def test_timeout_does_not_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_run(argv, **kwargs):
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("app.services.downloader.subprocess.run", fake_run)
    with pytest.raises(DownloadFailed, match="The download timed out. Try again."):
        probe_and_download("https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path / "job")
    assert calls == 1


def test_happy_path_download_and_mkv_remux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir = tmp_path / "job-happy"

    def fake_run(argv, **kwargs):
        assert kwargs.get("shell") is False
        assert kwargs.get("start_new_session") is False
        if "--dump-single-json" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "duration": 125.0,
                        "title": "  GTA 6 Trailer Chaos  ",
                        "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
                        "extractor_key": "Youtube",
                    }
                ),
                stderr="",
            )
        if argv[0] == "yt-dlp":
            (job_dir / "vod.mkv").write_bytes(b"fake-mkv-bytes")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "ffmpeg":
            (job_dir / "vod.mp4").write_bytes(b"fake-mp4-bytes")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "ffprobe":
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"format": {"duration": "130.5"}}), stderr=""
            )
        raise AssertionError(f"Unexpected command: {argv}")

    monkeypatch.setattr("app.services.downloader.subprocess.run", fake_run)
    result = probe_and_download("https://www.youtube.com/watch?v=dQw4w9WgXcQ", job_dir)

    assert result.path == job_dir / "vod.mp4"
    assert result.path.exists()
    assert not (job_dir / "vod.mkv").exists()
    assert result.platform == "youtube"
    assert result.title == "GTA 6 Trailer Chaos"
    assert result.duration_s == 130.5
    assert result.thumbnail_url == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
