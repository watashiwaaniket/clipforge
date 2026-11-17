# Pipeline Constraints & Invariants

These cross-component constraints are easy to violate unintentionally when modifying the worker pipeline.

## Request vs. Worker Validation Split

- `POST /api/v1/jobs` performs only cheap checks: `detect_platform()` URL parsing, active API key existence, per-user concurrency/daily limits, and storage headroom.
- `yt-dlp` probing (`probe_and_download()`) runs **only** inside the Celery worker because `--dump-single-json` takes 2–15+ seconds and would stall API workers. As a result, `MAX_VOD_MINUTES` and subscriber-only rejections surface as failed jobs rather than `400` responses from `POST /api/v1/jobs`.

## Crash Recovery & Resume Artifacts

Because Celery uses `task_acks_late=True` and `task_reject_on_worker_lost=True`, a mid-job worker kill requeues the task instead of immediately failing the job:

- `jobs.attempt` caps execution at 3 attempts before failing with `worker_lost`.
- On retry, the worker resumes from valid atomic artifacts in the job directory (`vod.mp4`, `transcript.json`, `windows.json`) rather than restarting from scratch.
- Redis `broker_transport_options.visibility_timeout` (`7200`s) must stay strictly greater than `task_time_limit` (`2760`s); otherwise Redis redelivers a still-running job.

## Scoring & Prompt Contract

- `backend/tests/test_scorer.py` reads the `>` blockquote in `docs/ARCHITECTURE.md` §3.3 to verify `SYSTEM_PROMPT` stays aligned with the v1 prompt asset. Keep that blockquote in `docs/ARCHITECTURE.md` intact if you edit docs.
- The LLM is asked only for chunk-local word indices (`chunk_word_start`, `chunk_word_end`), never timestamps in seconds.
- On invalid JSON/schema output, the scorer retries once with a corrective message; a second failure falls back to deterministic keyword/caps heuristics (`score_source="heuristic"`).

## Subprocess & FFmpeg Traps

- Every `subprocess.run()` call must keep `start_new_session=False` so `docker compose kill` terminates child `ffmpeg` / `yt-dlp` processes instead of leaving orphans.
- Captions use `Liberation Sans` (`fonts-liberation` in `backend/Dockerfile`) because `python:3.11-slim` does not ship Arial and `libass` silently drops missing fonts.
- Whisper audio is extracted to 16 kHz mono WAV and sliced into 10-minute chunks (`WHISPER_CHUNK_SECONDS = 600`) to stay below the Whisper API's 25 MB upload limit.
- Clip extraction runs in per-clip `try/except` blocks so one corrupted window marks only that clip `failed` rather than failing the whole job.
