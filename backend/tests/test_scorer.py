import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError
from pydantic import ValidationError

from app.logging import configure_logging
from app.services import scorer as scorer_module
from app.services.scorer import (
    HEURISTIC_HOOK,
    SCORE_CONCURRENCY,
    SYSTEM_PROMPT,
    HighlightResponse,
    ScoredWindow,
    build_chunks,
    build_user_prompt,
    extend_window,
    load_windows,
    merge_candidates,
    save_windows,
    score_chunk_heuristic,
    score_transcript,
)
from app.services.transcribe import Word


def _sk_canary() -> str:
    return "sk-canary-" + "DO-NOT-LEAK-" + "0123456789abcdef"


def _gsk_canary() -> str:
    return "gsk_" + "canary-" + "DO-NOT-LEAK-" + "abcdef0123456789"


def _make_words(tokens: list[str], *, start: float = 0.0, step: float = 0.5) -> list[Word]:
    words: list[Word] = []
    t = start
    for tok in tokens:
        words.append(Word(word=tok, start=t, end=t + 0.4))
        t += step
    return words


def test_chunk_grid_boundary_with_min_words_patched(monkeypatch: pytest.MonkeyPatch) -> None:
    assert scorer_module.MIN_WORDS == 8
    assert SCORE_CONCURRENCY == 4
    monkeypatch.setattr(scorer_module, "MIN_WORDS", 1)

    words = [
        Word(word="w0", start=0.0, end=0.5),
        Word(word="w39", start=39.0, end=39.5),
        Word(word="w44", start=44.0, end=44.5),
        Word(word="w46", start=46.0, end=46.5),
    ]
    chunks = build_chunks(words)
    assert chunks == [[0, 1, 2], [2, 3]]
    assert 2 in chunks[0] and 2 in chunks[1]
    assert 3 not in chunks[0] and 3 in chunks[1]


def test_system_prompt_matches_architecture_v1_asset() -> None:
    expected = (
        "You are a short-form content strategist specializing in GTA 6 streaming content. "
        "Identify moments with viral clip potential: insane stunts, heist fails, rage moments, "
        "funny RP interactions, big reactions, unexpected chaos. Avoid: walking/traveling, menus, "
        "dead air, generic commentary. For each candidate return: chunk_word_start, chunk_word_end "
        "(indices within the provided chunk), score 0-100, title (max 60 chars, clickbait-y but "
        "accurate), hook (one sentence on why it works). Respond with JSON matching the provided "
        "schema. If nothing is clip-worthy, return an empty list."
    )
    assert SYSTEM_PROMPT == expected

    arch_path = Path(__file__).resolve().parents[2] / "docs" / "ARCHITECTURE.md"
    if arch_path.exists():
        arch_text = arch_path.read_text(encoding="utf-8")
        quote_lines = [
            line.lstrip("> ").strip()
            for line in arch_text.splitlines()
            if line.strip().startswith(">")
        ]
        assert expected in " ".join(quote_lines)


def test_user_prompt_has_zero_index_and_no_timestamps() -> None:
    words = _make_words(["hello", "chat", "we", "just", "pulled", "a", "wild", "heist"])
    prompt = build_user_prompt(words, list(range(len(words))))
    assert "[0] hello" in prompt
    assert '{"highlights":[]}' in prompt
    assert re.search(r"\d+\.\d+", prompt) is None


def test_seconds_payload_fails_pydantic_validation() -> None:
    with pytest.raises(ValidationError):
        HighlightResponse.model_validate(
            {
                "highlights": [
                    {
                        "start_seconds": 1,
                        "end_seconds": 2,
                        "score": 80,
                        "title": "Bad shape",
                        "hook": "Uses seconds",
                    }
                ]
            }
        )


def test_title_over_60_and_float_score_validate_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scorer_module, "MIN_WORDS", 1)
    words = _make_words(["one", "two", "three", "four", "five", "six", "seven", "eight"])
    long_title = "A" * 75 + "   extra   spaces"
    calls: list[dict[str, object]] = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            content = json.dumps(
                {
                    "highlights": [
                        {
                            "chunk_word_start": 1,
                            "chunk_word_end": 5,
                            "score": 99.6,
                            "title": long_title,
                            "hook": "Chaos erupts mid-heist.",
                        }
                    ]
                }
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    windows = score_transcript(
        words,
        duration_s=30.0,
        api_key="sk-test-1234567890",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        client_factory=lambda _k, _u: SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        ),
    )

    assert len(calls) == 1
    assert len(windows) == 1
    assert len(windows[0].title) == 60
    assert windows[0].score == 100
    assert windows[0].score_source == "llm"


def test_one_bad_json_retries_once_with_500_cap_and_corrective_line() -> None:
    words = _make_words(["one", "two", "three", "four", "five", "six", "seven", "eight"])
    calls: list[list[dict[str, str]]] = []
    huge_bad_reply = "BAD_PREFIX_" + ("x" * 1200) + "_NEVER_ATTACHED"

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs["messages"])
            if len(calls) == 1:
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=huge_bad_reply))]
                )
            valid = json.dumps(
                {
                    "highlights": [
                        {
                            "chunk_word_start": 0,
                            "chunk_word_end": 6,
                            "score": 88,
                            "title": "Recovered JSON clip",
                            "hook": "Second attempt worked.",
                        }
                    ]
                }
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=valid))]
            )

    windows = score_transcript(
        words,
        duration_s=20.0,
        api_key="sk-test-1234567890",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        client_factory=lambda _k, _u: SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        ),
    )

    assert len(calls) == 2
    assert "your last reply was not valid JSON" not in json.dumps(calls[0])
    last_user_msg = calls[1][-1]["content"]
    assert calls[1][-1]["role"] == "user"
    assert last_user_msg.splitlines()[-1] == "your last reply was not valid JSON"
    assert "_NEVER_ATTACHED" not in last_user_msg
    assert len(windows) == 1
    assert windows[0].score_source == "llm"
    assert windows[0].title == "Recovered JSON clip"


def test_two_bad_replies_fall_back_to_heuristic() -> None:
    words = _make_words(
        ["bro", "that", "POLICE", "crash!", "was", "so", "crazy", "here"]
    )
    calls = 0

    class FakeCompletions:
        def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="still not json"))]
            )

    windows = score_transcript(
        words,
        duration_s=20.0,
        api_key="sk-test-1234567890",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        client_factory=lambda _k, _u: SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        ),
    )

    assert calls == 2
    assert len(windows) == 1
    assert windows[0].score_source == "heuristic"
    assert windows[0].hook == HEURISTIC_HOOK


@pytest.mark.parametrize("reply_text", ["[]", '{"highlights":[]}', "```json\n[]\n```"])
def test_empty_valid_replies_produce_zero_windows_and_no_heuristic(
    monkeypatch: pytest.MonkeyPatch,
    reply_text: str,
) -> None:
    words = _make_words(["CRASH!", "EXPLOSION!", "HEIST", "FAILED", "OMG!", "BRO", "CHAT", "WAIT"])
    monkeypatch.setattr(
        "app.services.scorer.score_chunk_heuristic",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("heuristic must not run")),
    )

    class FakeCompletions:
        def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=reply_text))]
            )

    windows = score_transcript(
        words,
        duration_s=20.0,
        api_key="sk-test-1234567890",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        client_factory=lambda _k, _u: SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        ),
    )

    assert windows == []


def test_response_format_400_retries_without_consuming_json_retry() -> None:
    words = _make_words(["one", "two", "three", "four", "five", "six", "seven", "eight"])
    req = httpx.Request("POST", "https://compatible.example.com/v1/chat/completions")
    seen_response_formats: list[bool] = []

    class FakeCompletions:
        def create(self, **kwargs):
            seen_response_formats.append("response_format" in kwargs)
            step = len(seen_response_formats)
            if step == 1:
                raise APIStatusError(
                    "response_format json_object is not supported by this model",
                    response=httpx.Response(400, request=req),
                    body=None,
                )
            if step == 2:
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="bad json"))]
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "highlights": [
                                        {
                                            "chunk_word_start": 0,
                                            "chunk_word_end": 5,
                                            "score": 82,
                                            "title": "No response_format needed",
                                            "hook": "Worked after corrective retry.",
                                        }
                                    ]
                                }
                            )
                        )
                    )
                ]
            )

    windows = score_transcript(
        words,
        duration_s=20.0,
        api_key="sk-test-1234567890",
        base_url="https://compatible.example.com/v1",
        model="local-model",
        client_factory=lambda _k, _u: SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        ),
    )

    assert seen_response_formats == [True, False, True]
    assert len(windows) == 1
    assert windows[0].score_source == "llm"


def test_heuristic_keyword_rules_and_title_centering() -> None:
    weak_only = _make_words(["chat", "wait", "chatting", "let's", "go", "no", "way", "here"])
    assert score_chunk_heuristic(weak_only, list(range(len(weak_only)))) is None

    strong_tokens = _make_words(
        ["we", "were", "driving", "then", "CRASH!", "bro", "omg", "clip", "that", "ending"]
    )
    cand = score_chunk_heuristic(strong_tokens, list(range(len(strong_tokens))))
    assert cand is not None
    assert cand.score_source == "heuristic"
    assert cand.hook == HEURISTIC_HOOK
    # 1 strong (crash) + 3 weak (bro, omg, clip that) = 48 + 1 caps (8) + 1 exclaim (6) = 62
    assert cand.score == 62
    assert cand.title == "driving then CRASH! bro omg clip"


def test_padded_overlap_suppression_and_max_clips_cap() -> None:
    # Unpadded [0, 10] and [10, 20] with no dead air overlap after +/-8s -> [0, 18] and [2, 28].
    words = [Word(word=f"w{i}", start=float(i), end=float(i + 1)) for i in range(20)]
    cands = [
        scorer_module._RawCandidate(0, 9, 90, "High score", "hook", "llm"),
        scorer_module._RawCandidate(10, 19, 80, "Lower touching", "hook", "llm"),
    ]
    kept = merge_candidates(words, cands, duration_s=100.0, max_clips=8)
    assert len(kept) == 1
    assert kept[0].score == 90
    assert kept[0].start_s == 0.0
    assert kept[0].end_s == 18.0

    # 10s window score 40 sharing >= 80% of its padded length with score-90 window is dropped.
    words_shared = [
        Word(word="s0", start=10.0, end=11.0),
        Word(word="s1", start=19.0, end=20.0),
    ]
    cands_shared = [
        scorer_module._RawCandidate(0, 1, 90, "Dominant", "h1", "llm"),
        scorer_module._RawCandidate(0, 1, 40, "Shadowed", "h2", "llm"),
    ]
    assert len(merge_candidates(words_shared, cands_shared, duration_s=100.0)) == 1

    # Ninth non-overlapping window is dropped when max_clips_per_job == 8.
    spread_words: list[Word] = []
    spread_cands: list[scorer_module._RawCandidate] = []
    for idx in range(9):
        base_t = idx * 60.0
        spread_words.append(Word(word=f"w{idx}a", start=base_t, end=base_t + 1.0))
        spread_words.append(Word(word=f"w{idx}b", start=base_t + 9.0, end=base_t + 10.0))
        spread_cands.append(
            scorer_module._RawCandidate(2 * idx, 2 * idx + 1, 95 - idx, f"Clip {idx}", "h", "llm")
        )
    capped = merge_candidates(spread_words, spread_cands, duration_s=600.0, max_clips=8)
    assert len(capped) == 8
    assert [w.title for w in capped] == [f"Clip {idx}" for idx in range(8)]


def test_dead_air_clamp_blocks_long_gap_and_allows_short_gap() -> None:
    words_long_gap = [
        Word(word="before", start=49.0, end=50.0),
        Word(word="after", start=70.0, end=73.0),
    ]
    extended = extend_window(words_long_gap, 1, 1, duration_s=100.0)
    assert extended == (70.0, 81.0)

    words_short_gap = [
        Word(word="before", start=68.0, end=69.0),
        Word(word="after", start=70.0, end=73.0),
    ]
    extended_short = extend_window(words_short_gap, 1, 1, duration_s=100.0)
    assert extended_short == (62.0, 81.0)

    # Clamped window shorter than 2.0s is dropped
    tiny_isolated = [
        Word(word="prev", start=0.0, end=1.0),
        Word(word="island", start=10.0, end=11.0),
        Word(word="next", start=20.0, end=21.0),
    ]
    assert extend_window(tiny_isolated, 1, 1, duration_s=30.0) is None


def test_worked_index_mapping_example_and_out_of_range_handling() -> None:
    starts = [0.0, 1.0, 2.0, 3.0, 10.0, 20.0, 40.0, 44.0, 46.0, 50.0, 55.0, 60.0, 70.0, 80.0]
    letters = list("abcdefghijklmn")
    words = [Word(word=ch, start=t, end=t + 0.5) for ch, t in zip(letters, starts, strict=True)]

    chunks = build_chunks(words)
    assert chunks == [list(range(0, 8)), list(range(6, 14))]

    call_no = 0

    class FakeCompletions:
        def create(self, **kwargs):
            nonlocal call_no
            call_no += 1
            prompt = kwargs["messages"][1]["content"]
            # Chunk 1 has [0] g, [1] h, [2] i ...
            if "[0] g" in prompt:
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps(
                                    {
                                        "highlights": [
                                            {
                                                "chunk_word_start": 1,
                                                "chunk_word_end": 2,
                                                "score": 91,
                                                "title": "Mapped chunk 1",
                                                "hook": "Starts at global index 7.",
                                            },
                                            {
                                                "chunk_word_start": 99,
                                                "chunk_word_end": 100,
                                                "score": 95,
                                                "title": "Out of range",
                                                "hook": "Should be dropped",
                                            },
                                        ]
                                    }
                                )
                            )
                        )
                    ]
                )
            # Chunk 0 returns only out-of-range indices -> contributes nothing, no heuristic
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "highlights": [
                                        {
                                            "chunk_word_start": 50,
                                            "chunk_word_end": 55,
                                            "score": 99,
                                            "title": "Ignored",
                                            "hook": "Ignored",
                                        }
                                    ]
                                }
                            )
                        )
                    )
                ]
            )

    windows = score_transcript(
        words,
        duration_s=100.0,
        api_key="sk-test-1234567890",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        client_factory=lambda _k, _u: SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        ),
    )

    assert call_no == 2
    assert len(windows) == 1
    assert windows[0].word_start == 7
    assert windows[0].word_end == 8
    assert windows[0].title == "Mapped chunk 1"


@pytest.mark.parametrize("canary_fn", [_sk_canary, _gsk_canary])
def test_score_canary_is_redacted_in_worker_thread_logs(
    caplog: pytest.LogCaptureFixture,
    canary_fn,
) -> None:
    canary = canary_fn()
    words = _make_words(["we", "wait", "and", "keep", "walking", "calmly", "around", "town"])
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    received_keys: list[str] = []

    class ExplodingCompletions:
        def create(self, **_kwargs):
            raise APIStatusError(
                f"upstream rejected key={canary}",
                response=httpx.Response(401, request=req),
                body=None,
            )

    def spy_factory(api_key: str, _base_url: str):
        received_keys.append(api_key)
        return SimpleNamespace(chat=SimpleNamespace(completions=ExplodingCompletions()))

    configure_logging()
    with caplog.at_level(logging.WARNING):
        windows = score_transcript(
            words,
            duration_s=20.0,
            api_key=canary,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            client_factory=spy_factory,
        )

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert received_keys == [canary]
    assert windows == []
    assert canary not in joined
    assert "[REDACTED]" in joined


def test_windows_save_and_load_roundtrip_and_invalid_rejected(tmp_path: Path) -> None:
    path = tmp_path / "windows.json"
    windows = [
        ScoredWindow(
            start_s=2.0,
            end_s=20.0,
            score=92,
            title="Wild getaway",
            hook="Police chase starts fast.",
            score_source="llm",
            word_start=4,
            word_end=19,
        )
    ]
    save_windows(path, windows)
    assert load_windows(path) == windows

    # Empty list is valid for windows.json
    save_windows(path, [])
    assert load_windows(path) == []

    # Invalid JSON / invalid fields return None
    path.write_text("{bad", encoding="utf-8")
    assert load_windows(path) is None

    path.write_text(
        json.dumps([{"start_s": 10.0, "end_s": 5.0, "score": 80, "title": "x", "hook": "y"}]),
        encoding="utf-8",
    )
    assert load_windows(path) is None


def test_transport_retries_429_and_falls_back_to_heuristic_after_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _s: None)
    words = _make_words(["omg", "that", "HEIST!", "failed", "so", "hard", "bro", "clip", "that"])
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

    attempts_recover = 0

    class Recover429:
        def create(self, **_kwargs):
            nonlocal attempts_recover
            attempts_recover += 1
            if attempts_recover < 3:
                raise APIStatusError(
                    "rate limited", response=httpx.Response(429, request=req), body=None
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "highlights": [
                                        {
                                            "chunk_word_start": 0,
                                            "chunk_word_end": 8,
                                            "score": 85,
                                            "title": "Recovered after 429",
                                            "hook": "Third attempt worked.",
                                        }
                                    ]
                                }
                            )
                        )
                    )
                ]
            )

    recovered = score_transcript(
        words,
        duration_s=20.0,
        api_key="sk-test-1234567890",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        client_factory=lambda _k, _u: SimpleNamespace(
            chat=SimpleNamespace(completions=Recover429())
        ),
    )
    assert attempts_recover == 3
    assert len(recovered) == 1
    assert recovered[0].score_source == "llm"

    attempts_exhaust = 0

    class Exhaust429:
        def create(self, **_kwargs):
            nonlocal attempts_exhaust
            attempts_exhaust += 1
            raise APIStatusError(
                "rate limited", response=httpx.Response(429, request=req), body=None
            )

    fallback = score_transcript(
        words,
        duration_s=20.0,
        api_key="sk-test-1234567890",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        client_factory=lambda _k, _u: SimpleNamespace(
            chat=SimpleNamespace(completions=Exhaust429())
        ),
    )
    assert attempts_exhaust == 3
    assert len(fallback) == 1
    assert fallback[0].score_source == "heuristic"
    assert fallback[0].hook == HEURISTIC_HOOK

