import json
import math
import string
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Self

import httpx
import structlog
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.errors import ClipForgeError
from app.logging import secret_boundary, secret_scope
from app.services.providers import (
    ANTIGRAVITY_PROVIDER,
    AntigravityProvider,
    ProviderConfigError,
    build_antigravity_provider,
    build_openai_client,
    is_local_antigravity_sentinel,
    is_retryable_antigravity_error,
)
from app.services.transcribe import Word

log = structlog.get_logger()

WINDOW_S = 45.0
OVERLAP_S = 5.0
STEP_S = WINDOW_S - OVERLAP_S
MIN_WORDS = 8
SCORE_CONCURRENCY = 4
PAD_S = 8.0
DEAD_AIR_S = 1.5
MIN_WINDOW_S = 2.0
LLM_TEMPERATURE = 0.2
LLM_MAX_TOKENS = 800
LLM_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
CORRECTIVE_LINE = "your last reply was not valid JSON"
HEURISTIC_HOOK = "Heuristic pick (keyword density). LLM scoring was unavailable."

SYSTEM_PROMPT = (
    "You are a short-form content strategist specializing in GTA 6 streaming content. "
    "Identify moments with viral clip potential: insane stunts, heist fails, rage moments, "
    "funny RP interactions, big reactions, unexpected chaos. Avoid: walking/traveling, menus, "
    "dead air, generic commentary. For each candidate return: chunk_word_start, chunk_word_end "
    "(indices within the provided chunk), score 0-100, title (max 60 chars, clickbait-y but "
    "accurate), hook (one sentence on why it works). Respond with JSON matching the provided "
    "schema. If nothing is clip-worthy, return an empty list."
)

_STRONG_TOKENS = frozenset(
    {
        "crash",
        "wreck",
        "heist",
        "fail",
        "failed",
        "police",
        "busted",
        "wanted",
        "shot",
        "shoot",
        "dead",
        "died",
        "explode",
        "explosion",
        "glitch",
        "bug",
        "insane",
        "crazy",
        "rage",
        "scream",
    }
)
_WEAK_TOKENS = frozenset({"omg", "bro", "dude", "chat", "wait", "run", "money", "stars"})
_WEAK_PHRASES = (
    ("no", "way"),
    ("oh", "my", "god"),
    ("let's", "go"),
    ("lets", "go"),
    ("clip", "that"),
)

_active_pool_lock = threading.Lock()
_active_executor: ThreadPoolExecutor | None = None
_active_cancel_event: threading.Event | None = None


@dataclass(frozen=True)
class ScoredWindow:
    start_s: float
    end_s: float
    score: int
    title: str
    hook: str
    score_source: Literal["llm", "heuristic"]
    word_start: int
    word_end: int


@dataclass(frozen=True)
class _RawCandidate:
    word_start: int
    word_end: int
    score: int
    title: str
    hook: str
    score_source: Literal["llm", "heuristic"]


class HighlightCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_word_start: int = Field(ge=0)
    chunk_word_end: int = Field(ge=0)
    score: int = Field(ge=0, le=100)
    title: str = Field(min_length=1)
    hook: str = Field(min_length=1)

    @field_validator("score", mode="before")
    @classmethod
    def round_score(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
        if not math.isfinite(value):
            return value
        return max(0, min(100, int(round(value))))

    @field_validator("title", "hook", mode="before")
    @classmethod
    def strip_and_cap(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, str):
            return value
        cap = 60 if info.field_name == "title" else 300
        cleaned = " ".join(value.split())
        return cleaned[:cap]

    @model_validator(mode="after")
    def in_order(self) -> Self:
        if self.chunk_word_end < self.chunk_word_start:
            raise ValueError("end before start")
        return self


class HighlightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    highlights: list[HighlightCandidate]


def _set_active_pool(executor: ThreadPoolExecutor, cancel_event: threading.Event) -> None:
    global _active_executor, _active_cancel_event
    with _active_pool_lock:
        _active_executor = executor
        _active_cancel_event = cancel_event


def _clear_active_pool(executor: ThreadPoolExecutor) -> None:
    global _active_executor, _active_cancel_event
    with _active_pool_lock:
        if _active_executor is executor:
            _active_executor = None
            _active_cancel_event = None


def cancel_score_pool() -> None:
    with _active_pool_lock:
        executor = _active_executor
        cancel_event = _active_cancel_event
    if cancel_event is not None:
        cancel_event.set()
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def build_chunks(words: list[Word]) -> list[list[int]]:
    if not words:
        return []
    chunks: list[list[int]] = []
    t = words[0].start
    last = words[-1].end
    while t < last:
        idxs = [i for i, w in enumerate(words) if t <= w.start < t + WINDOW_S]
        if len(idxs) >= MIN_WORDS:
            chunks.append(idxs)
        t += STEP_S
    return chunks


def build_user_prompt(words: list[Word], chunk_indices: list[int]) -> str:
    numbered = "\n".join(
        f"[{local_idx}] {words[global_idx].word}"
        for local_idx, global_idx in enumerate(chunk_indices)
    )
    return (
        "Words are numbered from 0. chunk_word_start and chunk_word_end are inclusive indices "
        "into this list only. Do not return seconds.\n"
        'Respond with a JSON object of this shape, and no other keys: {"highlights":[]}\n'
        'Each item in "highlights" must have: '
        '{"chunk_word_start":0,"chunk_word_end":0,"score":0,"title":"","hook":""}\n'
        "An empty highlights array means nothing in this chunk is clip-worthy.\n\n"
        f"{numbered}"
    )


def _strip_single_json_fence(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    first_nl = text.find("\n")
    if first_nl != -1:
        header = text[:first_nl].strip().lower()
        if header in {"```json", "```"} and text.endswith("```"):
            return text[first_nl + 1 : -3].strip()
    if text.lower().startswith("```json") and text.endswith("```"):
        return text[len("```json") : -3].strip()
    if text.endswith("```") and len(text) > 6:
        return text[3:-3].strip()
    return text


def _parse_highlight_response(raw_content: str | None) -> HighlightResponse:
    if not isinstance(raw_content, str) or not raw_content.strip():
        raise ValueError("Empty LLM response")
    cleaned = _strip_single_json_fence(raw_content)
    parsed = json.loads(cleaned)
    if isinstance(parsed, list):
        parsed = {"highlights": parsed}
    elif not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object or array")
    return HighlightResponse.model_validate(parsed)


def _is_retryable_llm_exception(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError, APITimeoutError)):
        return True
    if isinstance(exc, APIConnectionError):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or 500 <= exc.status_code < 600
    return False


def _mentions_response_format(exc: APIStatusError) -> bool:
    parts = [str(exc)]
    body = getattr(exc, "body", None)
    if body is not None:
        parts.append(str(body))
    return "response_format" in " ".join(parts).lower()


def _create_completion(
    client: Any,
    model: str,
    messages: list[dict[str, str]],
) -> str | None:
    if isinstance(client, AntigravityProvider):
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception(is_retryable_antigravity_error),
            reraise=True,
        )
        def _run_antigravity() -> str | None:
            return client.complete(
                messages=messages,
                temperature=LLM_TEMPERATURE,
                max_tokens=LLM_MAX_TOKENS,
                json_mode=True,
                json_schema=HighlightResponse.model_json_schema(),
            )

        return _run_antigravity()

    def _invoke(include_response_format: bool) -> Any:
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception(_is_retryable_llm_exception),
            reraise=True,
        )
        def _run() -> Any:
            kwargs: dict[str, Any] = {
                "model": model,
                "temperature": LLM_TEMPERATURE,
                "max_tokens": LLM_MAX_TOKENS,
                "messages": messages,
            }
            if include_response_format:
                kwargs["response_format"] = {"type": "json_object"}
            return client.chat.completions.create(**kwargs)

        return _run()

    try:
        response = _invoke(include_response_format=True)
    except APIStatusError as exc:
        if exc.status_code == 400 and _mentions_response_format(exc):
            response = _invoke(include_response_format=False)
        else:
            raise

    choices = getattr(response, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    return getattr(message, "content", None)


def score_chunk_heuristic(words: list[Word], chunk_indices: list[int]) -> _RawCandidate | None:
    if not chunk_indices:
        return None

    raw_tokens = " ".join(words[idx].word for idx in chunk_indices).split()
    if not raw_tokens:
        return None

    strong_hits = 0
    weak_hits = 0
    caps_words = 0
    exclaim_words = 0
    first_strong_idx: int | None = None
    norm_tokens: list[str] = []

    for pos, raw_tok in enumerate(raw_tokens):
        if "!" in raw_tok:
            exclaim_words += 1
        stripped_orig = raw_tok.strip(string.punctuation)
        if len(stripped_orig) > 2 and stripped_orig.isupper():
            caps_words += 1
        norm = stripped_orig.casefold()
        norm_tokens.append(norm)
        if norm in _STRONG_TOKENS:
            strong_hits += 1
            if first_strong_idx is None:
                first_strong_idx = pos
        elif norm in _WEAK_TOKENS:
            weak_hits += 1

    if strong_hits == 0 or first_strong_idx is None:
        return None

    for idx in range(len(norm_tokens)):
        for phrase in _WEAK_PHRASES:
            phrase_len = len(phrase)
            if idx + phrase_len <= len(norm_tokens) and tuple(
                norm_tokens[idx : idx + phrase_len]
            ) == phrase:
                weak_hits += 1

    score = min(100, (strong_hits + weak_hits) * 12 + caps_words * 8 + exclaim_words * 6)
    if score < 24:
        return None

    title_start = max(0, first_strong_idx - 2)
    title_end = title_start + 6
    if title_end > len(raw_tokens):
        title_end = len(raw_tokens)
        title_start = max(0, title_end - 6)
    title = " ".join(raw_tokens[title_start:title_end])
    title = " ".join(title.split())[:60]
    if not title:
        title = "Highlight clip"

    return _RawCandidate(
        word_start=chunk_indices[0],
        word_end=chunk_indices[-1],
        score=score,
        title=title,
        hook=HEURISTIC_HOOK,
        score_source="heuristic",
    )


def _score_single_chunk(
    *,
    client: Any,
    model: str,
    words: list[Word],
    chunk_indices: list[int],
    cancel_event: threading.Event,
) -> tuple[list[_RawCandidate], Literal["llm", "heuristic"]]:
    if cancel_event.is_set():
        return [], "llm"

    user_prompt = build_user_prompt(words, chunk_indices)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    raw_reply: str | None = None
    try:
        raw_reply = _create_completion(client, model, messages)
        validated = _parse_highlight_response(raw_reply)
    except (json.JSONDecodeError, ValidationError, ValueError):
        if cancel_event.is_set():
            return [], "llm"
        bad_snippet = raw_reply[:500] if isinstance(raw_reply, str) else ""
        retry_content = (
            f"Previous reply:\n{bad_snippet}\n\n{CORRECTIVE_LINE}"
            if bad_snippet
            else CORRECTIVE_LINE
        )
        retry_messages = [
            *messages,
            {"role": "user", "content": retry_content},
        ]
        try:
            retry_reply = _create_completion(client, model, retry_messages)
            validated = _parse_highlight_response(retry_reply)
        except Exception as retry_exc:
            log.warning("score_llm_chunk_failed", exc_info=retry_exc)
            fallback = score_chunk_heuristic(words, chunk_indices)
            return ([fallback] if fallback is not None else []), "heuristic"
    except Exception as exc:
        log.warning("score_llm_transport_failed", exc_info=exc)
        fallback = score_chunk_heuristic(words, chunk_indices)
        return ([fallback] if fallback is not None else []), "heuristic"

    candidates: list[_RawCandidate] = []
    chunk_len = len(chunk_indices)
    for item in validated.highlights:
        if item.chunk_word_start >= chunk_len or item.chunk_word_end >= chunk_len:
            continue
        candidates.append(
            _RawCandidate(
                word_start=chunk_indices[item.chunk_word_start],
                word_end=chunk_indices[item.chunk_word_end],
                score=item.score,
                title=item.title,
                hook=item.hook,
                score_source="llm",
            )
        )
    return candidates, "llm"


def extend_window(
    words: list[Word], i0: int, i1: int, duration_s: float
) -> tuple[float, float] | None:
    raw_start = words[i0].start
    raw_end = words[i1].end
    start = max(0.0, raw_start - PAD_S)
    end = min(duration_s, raw_end + PAD_S)
    for k in range(i0, 0, -1):
        if words[k].start - words[k - 1].end > DEAD_AIR_S:
            start = max(start, words[k].start)
            break
    for k in range(i1, len(words) - 1):
        if words[k + 1].start - words[k].end > DEAD_AIR_S:
            end = min(end, words[k].end)
            break
    if end - start < MIN_WINDOW_S:
        return None
    return start, end


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    inter = min(a_end, b_end) - max(a_start, b_start)
    if inter <= 0.0:
        return False
    min_len = min(a_end - a_start, b_end - b_start)
    if min_len <= 0.0:
        return True
    return (inter / min_len) >= 0.5


def merge_candidates(
    words: list[Word],
    candidates: list[_RawCandidate],
    duration_s: float,
    *,
    max_clips: int = 8,
) -> list[ScoredWindow]:
    if max_clips <= 0 or not words or not candidates:
        return []

    padded: list[ScoredWindow] = []
    for cand in candidates:
        extended = extend_window(words, cand.word_start, cand.word_end, duration_s)
        if extended is None:
            continue
        start_s, end_s = extended
        padded.append(
            ScoredWindow(
                start_s=start_s,
                end_s=end_s,
                score=cand.score,
                title=cand.title,
                hook=cand.hook,
                score_source=cand.score_source,
                word_start=cand.word_start,
                word_end=cand.word_end,
            )
        )

    padded.sort(key=lambda w: (-w.score, w.start_s, w.title))

    kept: list[ScoredWindow] = []
    for window in padded:
        if any(_overlaps(window.start_s, window.end_s, k.start_s, k.end_s) for k in kept):
            continue
        kept.append(window)
        if len(kept) >= max_clips:
            break
    return kept


def _default_client_factory(
    api_key: str, base_url: str, provider: str, *, model: str = ""
) -> tuple[Any, Any]:
    try:
        if provider == ANTIGRAVITY_PROVIDER:
            ag_provider = build_antigravity_provider(
                credential=api_key,
                base_url=base_url,
                model=model,
                timeout=LLM_HTTP_TIMEOUT,
            )
            return ag_provider, ag_provider
        return build_openai_client(
            api_key=api_key,
            base_url=base_url,
            provider=provider,
            timeout=LLM_HTTP_TIMEOUT,
        )
    except ProviderConfigError as exc:
        raise ClipForgeError("internal", str(exc)) from exc


def score_transcript(
    words: list[Word],
    *,
    duration_s: float,
    api_key: str,
    base_url: str,
    model: str,
    provider: str = "compatible",
    max_clips: int = 8,
    job_id: str | None = None,
    client_factory: Callable[[str, str], Any] | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
) -> list[ScoredWindow]:
    chunks = build_chunks(words)
    if not chunks:
        return []

    skip_scope = provider == ANTIGRAVITY_PROVIDER and is_local_antigravity_sentinel(api_key)
    scope_ctx = secret_boundary() if skip_scope else secret_scope(api_key)
    with scope_ctx:
        closable: Any = None
        if client_factory is not None:
            created = client_factory(api_key, base_url)
            if isinstance(created, tuple) and len(created) == 2:
                closable, client = created
            else:
                client = created
        else:
            closable, client = _default_client_factory(
                api_key, base_url, provider, model=model
            )

        total_chunks = len(chunks)
        raw_candidates: list[_RawCandidate] = []
        cancel_event = threading.Event()
        executor = ThreadPoolExecutor(max_workers=SCORE_CONCURRENCY)
        _set_active_pool(executor, cancel_event)

        try:
            future_to_index = {}
            for chunk_index, chunk_indices in enumerate(chunks, start=1):
                ctx = copy_context()
                fut = executor.submit(
                    ctx.run,
                    _score_single_chunk,
                    client=client,
                    model=model,
                    words=words,
                    chunk_indices=chunk_indices,
                    cancel_event=cancel_event,
                )
                future_to_index[fut] = chunk_index

            completed = 0
            for fut in as_completed(future_to_index):
                chunk_index = future_to_index[fut]
                chunk_candidates, source = fut.result()
                raw_candidates.extend(chunk_candidates)
                completed += 1
                log.info(
                    "score_chunk",
                    job_id=job_id,
                    index=chunk_index,
                    total=total_chunks,
                    source=source,
                )
                if on_chunk is not None:
                    on_chunk(completed, total_chunks)

            executor.shutdown(wait=True)
        except BaseException:
            cancel_event.set()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            _clear_active_pool(executor)
            if closable is not None:
                closable.close()

    return merge_candidates(words, raw_candidates, duration_s, max_clips=max_clips)


def save_windows(path: Path, windows: list[ScoredWindow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    payload = [asdict(w) for w in windows]
    tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp_path.replace(path)


def load_windows(path: Path) -> list[ScoredWindow] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(raw, list):
        return None

    windows: list[ScoredWindow] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        start_s = item.get("start_s")
        end_s = item.get("end_s")
        score = item.get("score")
        title = item.get("title")
        hook = item.get("hook")
        score_source = item.get("score_source")
        word_start = item.get("word_start")
        word_end = item.get("word_end")

        if (
            isinstance(start_s, bool)
            or isinstance(end_s, bool)
            or not isinstance(start_s, (int, float))
            or not isinstance(end_s, (int, float))
        ):
            return None
        start_f = float(start_s)
        end_f = float(end_s)
        if not (math.isfinite(start_f) and math.isfinite(end_f)) or start_f < 0 or end_f <= start_f:
            return None

        if isinstance(score, bool) or not isinstance(score, int) or not (0 <= score <= 100):
            return None
        if not isinstance(title, str) or not title.strip():
            return None
        if not isinstance(hook, str) or not hook.strip():
            return None
        if score_source not in {"llm", "heuristic"}:
            return None
        if (
            isinstance(word_start, bool)
            or isinstance(word_end, bool)
            or not isinstance(word_start, int)
            or not isinstance(word_end, int)
            or word_start < 0
            or word_end < word_start
        ):
            return None

        windows.append(
            ScoredWindow(
                start_s=start_f,
                end_s=end_f,
                score=score,
                title=title.strip(),
                hook=hook.strip(),
                score_source=score_source,
                word_start=word_start,
                word_end=word_end,
            )
        )
    return windows
