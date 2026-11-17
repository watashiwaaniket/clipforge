import json
import math
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import structlog
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.errors import NoSpeech, TranscriptionFailed
from app.logging import secret_scope
from app.services.downloader import probe_media_duration

log = structlog.get_logger()

WHISPER_CHUNK_SECONDS = 600
WHISPER_FFMPEG_TIMEOUT_S = 600
WHISPER_HTTP_TIMEOUT = httpx.Timeout(120.0, connect=5.0)
WHISPER_BASE_URL = "https://api.openai.com/v1"
WHISPER_MODEL = "whisper-1"


@dataclass(frozen=True)
class Word:
    word: str
    start: float
    end: float


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> list[Word]: ...


def build_extract_wav_argv(input_path: Path, wav_path: Path) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]


def build_slice_wav_argv(wav_path: Path, chunk_path: Path, offset_s: int) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(offset_s),
        "-t",
        str(WHISPER_CHUNK_SECONDS),
        "-i",
        str(wav_path),
        "-c",
        "copy",
        str(chunk_path),
    ]


def compute_chunk_offsets(
    duration_s: float, chunk_seconds: int = WHISPER_CHUNK_SECONDS
) -> list[int]:
    if not math.isfinite(duration_s) or duration_s <= 0:
        return []
    offsets: list[int] = []
    offset = 0
    while offset < duration_s:
        offsets.append(offset)
        offset += chunk_seconds
    return offsets


def _is_retryable_whisper_exception(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError, APITimeoutError)):
        return True
    if isinstance(exc, APIConnectionError):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or 500 <= exc.status_code < 600
    return False


def _default_client_factory(api_key: str) -> tuple[httpx.Client | None, Any]:
    http_client = httpx.Client(follow_redirects=False, timeout=WHISPER_HTTP_TIMEOUT)
    client = OpenAI(
        api_key=api_key,
        base_url=WHISPER_BASE_URL,
        http_client=http_client,
        max_retries=0,
    )
    return http_client, client


def _parse_chunk_words(result: Any, offset_s: int) -> list[Word]:
    raw_words = result.get("words") if isinstance(result, dict) else getattr(result, "words", None)
    if not isinstance(raw_words, list):
        raise TranscriptionFailed(
            "Transcription did not return word timestamps.",
            error_code="no_word_timestamps",
        )

    parsed: list[Word] = []
    for item in raw_words:
        if isinstance(item, dict):
            raw_word = item.get("word")
            raw_start = item.get("start")
            raw_end = item.get("end")
        else:
            raw_word = getattr(item, "word", None)
            raw_start = getattr(item, "start", None)
            raw_end = getattr(item, "end", None)

        if not isinstance(raw_word, str):
            continue
        cleaned_word = raw_word.strip()
        if not cleaned_word:
            continue

        if (
            isinstance(raw_start, bool)
            or isinstance(raw_end, bool)
            or not isinstance(raw_start, (int, float))
            or not isinstance(raw_end, (int, float))
        ):
            continue
        raw_start_f = float(raw_start)
        raw_end_f = float(raw_end)
        if not (math.isfinite(raw_start_f) and math.isfinite(raw_end_f)) or raw_end_f < raw_start_f:
            continue
        start = max(0.0, raw_start_f + offset_s)
        end = max(start, raw_end_f + offset_s)
        parsed.append(Word(word=cleaned_word, start=start, end=end))
    return parsed


def save_transcript(path: Path, words: list[Word]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    payload = [asdict(w) for w in words]
    tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp_path.replace(path)


def load_transcript(path: Path) -> list[Word] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(raw, list) or len(raw) == 0:
        return None

    words: list[Word] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        w = item.get("word")
        s = item.get("start")
        e = item.get("end")
        if not isinstance(w, str) or not w.strip():
            return None
        if (
            isinstance(s, bool)
            or isinstance(e, bool)
            or not isinstance(s, (int, float))
            or not isinstance(e, (int, float))
        ):
            return None
        start = float(s)
        end = float(e)
        if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end < start:
            return None
        words.append(Word(word=w.strip(), start=start, end=end))
    return words


class WhisperApiTranscriber:
    def __init__(
        self,
        api_key: str,
        language: str = "en",
        *,
        client_factory: Callable[[str], Any] | None = None,
        on_chunk: Callable[[int, int], None] | None = None,
    ) -> None:
        self._api_key = api_key
        self._language = language.strip()
        self._client_factory = client_factory
        self._on_chunk = on_chunk
        self.last_chunk_count = 0

    def _build_client(self) -> tuple[httpx.Client | None, Any]:
        if self._client_factory is not None:
            created = self._client_factory(self._api_key)
            if isinstance(created, tuple) and len(created) == 2:
                return created
            return None, created
        return _default_client_factory(self._api_key)

    def _run_ffmpeg(self, argv: list[str]) -> None:
        try:
            subprocess.run(
                argv,
                check=True,
                capture_output=True,
                text=True,
                timeout=WHISPER_FFMPEG_TIMEOUT_S,
                shell=False,
                start_new_session=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise TranscriptionFailed() from exc

    def _transcribe_chunk_with_retry(self, client: Any, chunk_path: Path) -> Any:
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception(_is_retryable_whisper_exception),
            reraise=True,
        )
        def _invoke() -> Any:
            with chunk_path.open("rb") as handle:
                kwargs: dict[str, Any] = {
                    "model": WHISPER_MODEL,
                    "file": handle,
                    "response_format": "verbose_json",
                    "timestamp_granularities": ["word"],
                }
                if self._language:
                    kwargs["language"] = self._language
                return client.audio.transcriptions.create(**kwargs)

        try:
            return _invoke()
        except TranscriptionFailed:
            raise
        except Exception as exc:
            log.warning("whisper_chunk_failed", exc_info=exc)
            raise TranscriptionFailed() from exc

    def transcribe(self, audio_path: Path) -> list[Word]:
        work_root = audio_path.parent
        wav_file = work_root / "audio.wav"
        chunk_folder = work_root / "chunks"
        self.last_chunk_count = 0

        with secret_scope(self._api_key):
            try:
                self._run_ffmpeg(build_extract_wav_argv(audio_path, wav_file))
                duration_s = probe_media_duration(wav_file)
                if duration_s is None:
                    raise TranscriptionFailed()

                offsets = compute_chunk_offsets(duration_s, WHISPER_CHUNK_SECONDS)
                if not offsets:
                    raise NoSpeech()

                total_chunks = len(offsets)
                self.last_chunk_count = total_chunks
                chunk_folder.mkdir(parents=True, exist_ok=True)

                http_client, client = self._build_client()
                all_words: list[Word] = []
                try:
                    for index, offset_s in enumerate(offsets, start=1):
                        if self._on_chunk is not None:
                            self._on_chunk(index, total_chunks)
                        chunk_file = chunk_folder / f"{index}.wav"
                        self._run_ffmpeg(build_slice_wav_argv(wav_file, chunk_file, offset_s))
                        result = self._transcribe_chunk_with_retry(client, chunk_file)
                        chunk_words = _parse_chunk_words(result, offset_s)
                        all_words.extend(chunk_words)
                        chunk_file.unlink(missing_ok=True)
                finally:
                    if http_client is not None:
                        http_client.close()

                all_words.sort(key=lambda w: (w.start, w.end))
                if not all_words:
                    raise NoSpeech()
                return all_words
            finally:
                shutil.rmtree(chunk_folder, ignore_errors=True)
                wav_file.unlink(missing_ok=True)
