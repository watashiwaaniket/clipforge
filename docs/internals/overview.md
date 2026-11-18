# Architecture Overview

ClipForge splits lightweight control-plane operations from heavy media and LLM execution across two deployment boundaries.

## Execution Boundaries

- **Frontend (Next.js App Router on Vercel or local host)**: Handles UI, Auth.js session state (plus local guest sign-in when `ALLOW_GUEST_AUTH=true`), and same-origin clip streaming (`/api/media/clips/[id]`), because HTML `<video>` tags cannot send `Authorization: Bearer` headers.
- **API Process (FastAPI)**: Verifies HS256 bearer JWTs, enforces per-user rate limits and disk headroom, validates key configurations, exposes provider catalog/model metadata (`/api/v1/providers*`), and enqueues Celery jobs (`process_job.delay(job_id)`). The API process never downloads VODs, runs ffmpeg, or holds decrypted user API keys outside a key-validation or stateless Antigravity model-probe request.
- **Worker Process (Celery + Redis + PostgreSQL)**: Owns all long-running steps — `yt-dlp` download, `ffmpeg` audio extraction and 9:16 rendering, Whisper API calls, and parallel LLM scoring — and persists clip metadata to Postgres and rendered MP4s under `STORAGE_DIR`.

## Security & Secret Boundaries

- User LLM keys are Fernet-encrypted at rest (`api_keys.encrypted_key`) and decrypted only inside the worker at job execution time, or briefly inside `POST /api/v1/keys` to run the validation ping (plus caller-supplied OAuth tokens in `POST /api/v1/providers/antigravity/models` for stateless model discovery).
- `secret_scope()` binds decrypted keys and OAuth tokens into context-local structlog redaction so exceptions and worker threads never emit key material.
- `AUTH_SECRET` (Auth.js session encryption) and `JWT_SECRET` (API HS256 signing) are intentionally separate secrets so compromising one does not forge the other.

## Job State Machine

Job statuses move strictly forward:

```text
queued -> downloading -> transcribing -> scoring -> clipping -> completed
```

Any non-terminal state can transition to `failed` via `fail_job()`. Once a job reaches `completed` or `failed`, terminal transitions are no-ops, and raw artifacts (`vod.mp4`, extracted WAV, `transcript.json`, `windows.json`, `work/`) are removed while finished clips are preserved.

## Specification Precedence

When documents disagree:

1. `AGENTS.md` is authoritative on product scope and non-negotiable rules.
2. `docs/DESIGN.md` overrides stale rows in `docs/ARCHITECTURE.md` (notably `/api/v1` route prefixes, moving VOD length/sub-only checks from `POST /jobs` into the worker, and worker requeue/resume behavior).
