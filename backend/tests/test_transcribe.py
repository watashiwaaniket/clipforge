import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError, APITimeoutError

from app.errors import NoSpeech, TranscriptionFailed
from app.logging import configure_logging, redact
from app.services.transcribe import (
    WHISPER_CHUNK_SECONDS,
    WhisperApiTranscriber,
    Word,
    build_extract_wav_argv,
    compute_chunk_offsets,
    load_transcript,
    save_transcript,
)


def _sk_canary() -> str:
    return "sk-canary-" + "DO-NOT-LEAK-" + "0123456789abcdef"


def _gsk_canary() -> str:
    return "gsk_" + "canary-" + "DO-NOT-LEAK-" + "abcdef0123456789"


def test_extract_wav_argv_has_expected_audio_flags_and_no_shell_chars(tmp_path: Path) -> None:
    vod = tmp_path / "vod.mp4"
    wav = tmp_path / "audio.wav"
    argv = build_extract_wav_argv(vod, wav)
    assert argv[0] == "ffmpeg"
    assert "-ac" in argv and argv[argv.index("-ac") + 1] == "1"
    assert "-ar" in argv and argv[argv.index("-ar") + 1] == "16000"
    assert "-c:a" in argv and argv[argv.index("-c:a") + 1] == "pcm_s16le"
    for arg in argv:
        assert not any(ch in arg for ch in (";", "|", "&", "`", "$(", ">", "<"))


def test_chunk_count_for_1500s_wav_is_three() -> None:
    offsets = compute_chunk_offsets(1500.0, WHISPER_CHUNK_SECONDS)
    assert offsets == [0, 600, 1200]
    assert compute_chunk_offsets(1200.0, WHISPER_CHUNK_SECONDS) == [0, 600]


def test_whisper_transcriber_two_chunks_offsets_filtering_sorting_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vod = tmp_path / "vod.mp4"
    vod.write_bytes(b"fake-vod")

    def fake_run(argv, **kwargs):
        assert kwargs.get("shell") is False
        assert kwargs.get("start_new_session") is False
        out = Path(argv[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"wav-bytes")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("app.services.transcribe.subprocess.run", fake_run)
    monkeypatch.setattr("app.services.transcribe.probe_media_duration", lambda _p: 900.0)

    calls: list[dict[str, object]] = []
    chunk_responses = [
        {
            "words": [
                {"word": " second", "start": 2.0, "end": 2.5},
                {"word": "   ", "start": 0.5, "end": 0.8},
                {"word": "bad", "start": 5.0, "end": 4.0},
                {"word": "first ", "start": 0.2, "end": 0.7},
            ]
        },
        {
            "words": [
                {"word": "late", "start": 10.0, "end": 10.4},
                {"word": "chunk2", "start": 1.5, "end": 1.9},
            ]
        },
    ]

    class FakeTranscriptions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return chunk_responses[len(calls) - 1]

    fake_client = SimpleNamespace(audio=SimpleNamespace(transcriptions=FakeTranscriptions()))
    progress_events: list[tuple[int, int]] = []

    transcriber = WhisperApiTranscriber(
        "sk-test-1234567890",
        language="",
        client_factory=lambda _k: (None, fake_client),
        on_chunk=lambda idx, total: progress_events.append((idx, total)),
    )
    words = transcriber.transcribe(vod)

    assert transcriber.last_chunk_count == 2
    assert progress_events == [(1, 2), (2, 2)]
    assert len(calls) == 2
    assert all(c["model"] == "whisper-1" for c in calls)
    assert all("language" not in c for c in calls)

    assert words == [
        Word(word="first", start=0.2, end=0.7),
        Word(word="second", start=2.0, end=2.5),
        Word(word="chunk2", start=601.5, end=601.9),
        Word(word="late", start=610.0, end=610.4),
    ]
    assert not (tmp_path / "audio.wav").exists()
    assert not (tmp_path / "chunks").exists()


def test_missing_words_raises_no_word_timestamps_and_ignores_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vod = tmp_path / "vod.mp4"
    vod.write_bytes(b"fake-vod")

    monkeypatch.setattr(
        "app.services.transcribe.subprocess.run",
        lambda argv, **_kw: (
            Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True),
            Path(argv[-1]).write_bytes(b"wav"),
            subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
        )[-1],
    )
    monkeypatch.setattr("app.services.transcribe.probe_media_duration", lambda _p: 100.0)

    class FakeTranscriptions:
        def create(self, **kwargs):
            assert kwargs["language"] == "en"
            return {"segments": [{"start": 0.0, "end": 5.0, "text": "hello world"}], "words": None}

    fake_client = SimpleNamespace(audio=SimpleNamespace(transcriptions=FakeTranscriptions()))
    transcriber = WhisperApiTranscriber(
        "sk-test-1234567890",
        language="en",
        client_factory=lambda _k: (None, fake_client),
    )

    with pytest.raises(TranscriptionFailed) as exc_info:
        transcriber.transcribe(vod)

    assert exc_info.value.error_code == "no_word_timestamps"
    assert exc_info.value.user_message == "Transcription did not return word timestamps."
    assert not (tmp_path / "audio.wav").exists()


def test_empty_words_raises_no_speech(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vod = tmp_path / "vod.mp4"
    vod.write_bytes(b"fake-vod")

    monkeypatch.setattr(
        "app.services.transcribe.subprocess.run",
        lambda argv, **_kw: (
            Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True),
            Path(argv[-1]).write_bytes(b"wav"),
            subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
        )[-1],
    )
    monkeypatch.setattr("app.services.transcribe.probe_media_duration", lambda _p: 100.0)

    class FakeTranscriptions:
        def create(self, **_kwargs):
            return {"words": []}

    fake_client = SimpleNamespace(audio=SimpleNamespace(transcriptions=FakeTranscriptions()))
    transcriber = WhisperApiTranscriber(
        "sk-test-1234567890",
        language="en",
        client_factory=lambda _k: (None, fake_client),
    )

    with pytest.raises(NoSpeech):
        transcriber.transcribe(vod)


def test_whisper_retries_429_and_does_not_retry_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vod = tmp_path / "vod.mp4"
    vod.write_bytes(b"fake-vod")

    monkeypatch.setattr(
        "app.services.transcribe.subprocess.run",
        lambda argv, **_kw: (
            Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True),
            Path(argv[-1]).write_bytes(b"wav"),
            subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
        )[-1],
    )
    monkeypatch.setattr("app.services.transcribe.probe_media_duration", lambda _p: 100.0)
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _s: None)

    req = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")

    # 1. Two 429s then success
    attempts_429 = 0

    class Fake429:
        def create(self, **_kwargs):
            nonlocal attempts_429
            attempts_429 += 1
            if attempts_429 < 3:
                raise APIStatusError(
                    "rate limited",
                    response=httpx.Response(429, request=req),
                    body=None,
                )
            return {"words": [{"word": "ok", "start": 1.0, "end": 1.2}]}

    words = WhisperApiTranscriber(
        "sk-test-1234567890",
        client_factory=lambda _k: (
            None,
            SimpleNamespace(audio=SimpleNamespace(transcriptions=Fake429())),
        ),
    ).transcribe(vod)
    assert attempts_429 == 3
    assert words == [Word(word="ok", start=1.0, end=1.2)]

    # 2. 401 fails on first attempt without retries
    attempts_401 = 0

    class Fake401:
        def create(self, **_kwargs):
            nonlocal attempts_401
            attempts_401 += 1
            raise APIStatusError(
                "invalid auth",
                response=httpx.Response(401, request=req),
                body=None,
            )

    with pytest.raises(TranscriptionFailed):
        WhisperApiTranscriber(
            "sk-test-1234567890",
            client_factory=lambda _k: (
                None,
                SimpleNamespace(audio=SimpleNamespace(transcriptions=Fake401())),
            ),
        ).transcribe(vod)
    assert attempts_401 == 1

    # 3. Timeout retries 3 times before failing
    attempts_timeout = 0

    class FakeTimeout:
        def create(self, **_kwargs):
            nonlocal attempts_timeout
            attempts_timeout += 1
            raise APITimeoutError(request=req)

    with pytest.raises(TranscriptionFailed):
        WhisperApiTranscriber(
            "sk-test-1234567890",
            client_factory=lambda _k: (
                None,
                SimpleNamespace(audio=SimpleNamespace(transcriptions=FakeTimeout())),
            ),
        ).transcribe(vod)
    assert attempts_timeout == 3


@pytest.mark.parametrize("canary_fn", [_sk_canary, _gsk_canary])
def test_whisper_canary_is_redacted_from_error_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    canary_fn,
) -> None:
    import logging

    vod = tmp_path / "vod.mp4"
    vod.write_bytes(b"fake-vod")
    canary = canary_fn()
    received_keys: list[str] = []

    monkeypatch.setattr(
        "app.services.transcribe.subprocess.run",
        lambda argv, **_kw: (
            Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True),
            Path(argv[-1]).write_bytes(b"wav"),
            subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
        )[-1],
    )
    monkeypatch.setattr("app.services.transcribe.probe_media_duration", lambda _p: 100.0)

    class ExplodingTranscriptions:
        def create(self, **_kwargs):
            raise RuntimeError(f"upstream rejected credential={canary}")

    def spy_factory(api_key: str):
        received_keys.append(api_key)
        return (
            None,
            SimpleNamespace(audio=SimpleNamespace(transcriptions=ExplodingTranscriptions())),
        )

    configure_logging()
    transcriber = WhisperApiTranscriber(canary, client_factory=spy_factory)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(TranscriptionFailed) as exc_info:
            transcriber.transcribe(vod)

    from app.logging import secret_scope

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert received_keys == [canary]
    assert canary not in exc_info.value.user_message
    assert canary not in joined
    assert "[REDACTED]" in joined
    with secret_scope(canary):
        assert canary not in redact(f"error with {canary}")


def test_transcript_save_and_load_roundtrip_and_invalid_rejected(tmp_path: Path) -> None:
    path = tmp_path / "transcript.json"
    words = [Word(word="hello", start=0.1, end=0.4), Word(word="world", start=0.5, end=0.9)]
    save_transcript(path, words)
    assert load_transcript(path) == words

    path.write_text("[]", encoding="utf-8")
    assert load_transcript(path) is None

    path.write_text("{corrupt", encoding="utf-8")
    assert load_transcript(path) is None

    path.write_text(json.dumps([{"word": "ok", "start": 2.0, "end": 1.0}]), encoding="utf-8")
    assert load_transcript(path) is None
