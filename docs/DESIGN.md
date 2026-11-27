# ClipForge MVP — Phases 1–6

| | |
|---|---|
| **Author** | [Author] |
| **Date** | 2026-11-16 |
| **Status** | Finalized |
| **Audience** | Senior engineers implementing the remaining MVP |
| **Scope** | Phases 1–6 only. Phase 0 is already in the tree and is treated as fixed. |

This document is the implementation contract for the rest of the MVP. If it conflicts with `AGENTS.md`, `AGENTS.md` wins. If it conflicts with `docs/ARCHITECTURE.md` only, this document records the resolution. API paths are `/api/v1/...` as in `AGENTS.md` §8, not the unprefixed paths drawn in `docs/ARCHITECTURE.md` §1 and §5.

---

## Overview

ClipForge is a bring-your-own-key web app that turns a public Twitch VOD or YouTube video into at most eight scored, captioned, 9:16 clips aimed at GTA 6 streamers. Phase 0 already boots the skeleton: FastAPI on `:8000` (`GET /api/v1/health` → `{"ok": true}`), an idle Celery worker, Postgres 16, Redis 7, a Next.js 15 landing page on the host, and pydantic-settings for the locked environment variables. There is no schema, no auth, no job API, and no pipeline.

Phases 1–6 fill that skeleton in order. A signed-in user (or local guest workspace when `ALLOW_GUEST_AUTH=true`) stores one LLM provider configuration (OpenAI-compatible providers such as OpenAI, Groq, Together, Ollama, or custom `compatible`, plus the local/OAuth `antigravity` adapter, Fernet-encrypted at rest). `POST /api/v1/jobs` does cheap validation only and returns `202 {"job_id": "..."}` without downloading anything. The Celery task `process_job` then downloads with yt-dlp, transcribes with the OpenAI Whisper API behind a `Transcriber` interface, scores ~45s transcript chunks with the user's configured provider, and ffmpeg-cuts the top clips (center crop, loudness normalize, burned-in ASS karaoke). The dashboard polls every 2.5s while a job is active and plays or downloads finished clips. One clip failure cannot fail the job. Raw media is deleted after the job reaches `completed` or `failed`; final clips are kept.

The HTTP process never holds a VOD, a transcript, or a decrypted key outside `POST /api/v1/keys` validation and `POST /api/v1/providers/antigravity/models` model discovery. Scoring always decrypts the user key inside the worker. Whisper decrypts that same key when the stored provider host is `api.openai.com`, and otherwise uses `OPENAI_API_KEY` when that fallback is set. The decrypted value is never logged.

---

## Background & Motivation

### What Phase 0 actually shipped

Confirmed in the working tree, not just the docs:

- `docker-compose.yml` runs `db` (Postgres 16, unpublished ports, healthcheck), `redis` (Redis 7, unpublished), `api` (`uvicorn app.main:app` on `:8000` with `--reload`), and `worker` (`celery -A app.workers.celery_app:celery_app worker`). Shared anchor `x-backend` bind-mounts `./storage:/storage` and `./backend:/app`. The production frontend `frontend/Dockerfile` exists and is **not** in compose. Dev frontend is `npm run dev` via `start.sh` / `start.ps1`.
- `backend/app/main.py` configures structlog JSON logging, explicit CORS (`allow_origins=settings.cors_origin_list`, methods `GET POST DELETE OPTIONS`, headers `Authorization` and `Content-Type`), and `GET /api/v1/health`. Wildcard CORS raises `RuntimeError` (`backend/tests/test_health.py`).
- `backend/app/config.py` `Settings` already defines `database_url`, `redis_url`, `encryption_key`, `jwt_secret`, `storage_dir` (`/storage`), `max_vod_minutes` (240), `max_clips_per_job` (8), `openai_api_key`, `cors_origins`. Empty secrets are currently legal. The singleton `settings = Settings()` is created at import.
- `backend/app/db.py` exposes `Base`, a `pool_pre_ping` engine, `SessionLocal`, and `get_session()`. No models, no Alembic directory, no `alembic.ini`.
- `backend/app/workers/celery_app.py` sets `task_acks_late=True`, `task_reject_on_worker_lost=True`, `worker_prefetch_multiplier=1`, `task_track_started=True`. No tasks are imported. The worker boots and idles.
- `backend/app/api/` and `backend/app/services/` are empty packages. `backend/requirements.txt` sets **floors** (`>=`), not pins: FastAPI, SQLAlchemy 2, Alembic, psycopg, Celery, Redis, cryptography, structlog, httpx, tenacity, openai, yt-dlp. ffmpeg and ca-certificates are installed in `backend/Dockerfile` (`python:3.11-slim`). No font package is installed. The ingestion PR bounds yt-dlp as `yt-dlp>=2025.1.15,<2027.1.1`. Raising that ceiling is its own change, not a silent resolver bump.
- Frontend: `frontend/app/page.tsx` landing page, `health-pill.tsx` polling `/api/v1/health` every 10s, Tailwind tokens in `globals.css` (`--bg #0e0f0c`, `--bg-raise #171910`, `--ink #f3f0e7`, `--acid #dff25a`), fonts Instrument Sans and IBM Plex Mono. Dependencies are only `next@^15.1`, React 19, Tailwind 3. No Auth.js, no TanStack Query.
- `.env` is gitignored. `start.sh` and `start.ps1` copy `.env.example` and fill a blank `ENCRYPTION_KEY` (urlsafe-base64 32 bytes) and `JWT_SECRET` (64 hex chars) without printing the values. `storage/*` is gitignored except `storage/.gitkeep`. Media extensions `*.mp4`, `*.wav`, `*.ass` are gitignored.

### Pain this design removes

Without these phases a user cannot sign in, store a key, submit a URL, or receive a clip. The blueprint in `docs/ARCHITECTURE.md` is right about the pipeline and wrong about one failure-mode row (VOD length rejected inside `POST /jobs`). Leaving that fuzzy would either stall the request path on yt-dlp or create two competing rejection implementations. This document picks one.

### Locked product

User signs in (or uses local guest mode when `ALLOW_GUEST_AUTH=true`), pastes one public Twitch VOD or YouTube URL, configures one LLM provider (`openai`, `groq`, `together`, `ollama`, `compatible`, or `antigravity`), hits Generate. The worker downloads, word-level transcribes, scores viral moments for GTA 6 streaming, cuts the top clips, center-crops 9:16, burns karaoke captions. The dashboard lists jobs and plays or downloads clips. There is no in-browser editor.

---

## Goals & Non-Goals

### Goals

1. Ship phases 1–6 so `./start.sh` from a fresh clone yields a usable dashboard on `:3000` and API on `:8000`.
2. A real public VOD plus a configured LLM provider key/bridge produces captioned 9:16 clips with scores and titles. Typical 1–2 hour VODs finish in about 15 minutes. Hard stop is 45 minutes.
3. Every failure the user can hit has a `jobs.error` string (and a clip-level string when one export fails) and a stable `error_code` in logs.
4. Prove, with tests, that a canary API key never appears in logs, task arguments, HTTP bodies, or Postgres plaintext columns.
5. Stay inside the locked stack. No new language, framework, or database. No new Python runtime dependency: JWT verification and password hashing use the stdlib plus `cryptography.Fernet`, which is already pinned. Frontend additions are only the locked ones: Auth.js (`next-auth` 5), TanStack Query 5, and `jose` to sign HS256.

### Non-goals (do not design or build)

From `AGENTS.md` §2 and the product lock:

- Live-stream monitoring, chat or danmu ingestion, auto-posting, face-tracking crop, per-clip fees, more than one active key per user.
- In-browser editor, trim handles, share links, public clip URLs, account deletion, email verification, password reset.
- faster-whisper (the `Transcriber` interface must allow it later; do not implement it).
- Object storage (R2/S3), horizontal worker routing, MediaPipe, a metrics database, Prometheus, a separate cookie or session store.
- Playlist downloads, Twitch clips, channel URLs, using a `t=` timestamp as a clip range. A `t=` query on a VOD URL is ignored; the full VOD is processed.
- Cookie or credential upload for yt-dlp. If YouTube blocks the download, the job fails with a clear message.

---

## Key Decisions

1. **Duration and subscriber-only are worker failures, not `POST` rejections.** `detect_platform()` lives in `downloader.py` and is called on the HTTP path because it is a string check. `probe_and_download()` also lives in `downloader.py` and runs only inside `process_job`, where duration, liveness, and sub-only are known. `POST /api/v1/jobs` does not invoke yt-dlp. This overrides the single `docs/ARCHITECTURE.md` §7 row "VOD too long → rejected at POST, no job created" because `AGENTS.md` wins, golden rule 2 requires a job id in milliseconds, and §5 of the architecture doc already says POST only checks URL shape plus an active key. The user-facing strings from §3.1 are unchanged. Rationale is in [Rejection split](#rejection-split-post-vs-worker).

2. **FastAPI owns `users` and `password_hash`. Auth.js issues the HS256 JWT. FastAPI only verifies it.** The frontend is on Vercel and must not open Postgres. Auth.js Credentials `authorize()` calls `POST /api/v1/auth/login` (or `POST /api/v1/auth/guest` when `ALLOW_GUEST_AUTH=true`). The Auth.js `jwt` callback signs a compact JWS (`jose`, `alg=HS256`, shared `JWT_SECRET`). The default Auth.js cookie may stay encrypted; FastAPI never sees that cookie. It sees `Authorization: Bearer`. Do not use Auth.js's default JWE as the API credential — Python cannot verify it with the shared secret alone.

3. **One `api_keys` row per user, replaced by `POST /api/v1/keys`.** Ciphertext only (with a sentinel plaintext encrypted at rest when `antigravity` uses local CLI/bridge or local OAuth credentials). `GET` returns provider metadata (`provider`, `base_url`, `model`, timestamps) and never key material, a prefix, or a hint. Scoring always uses the user's configured provider. Whisper uses that same key when the stored provider host is `api.openai.com`. `OPENAI_API_KEY` is only the Whisper fallback when the user did not store an OpenAI key, matching `.env.example` and `AGENTS.md` §6 ("User BYOK is primary"). It is never used to score. This closes the cost question: the operator is not billed for transcription while an OpenAI user key is on file.

4. **Word indices, never seconds, in the LLM contract.** Chunks are ~45s with a 40s step (~5s overlap). Invalid JSON gets one corrective retry, then a deterministic heuristic tags `score_source=heuristic`. The v1 system prompt still says "return an empty list" and is not edited. Before a reply counts as invalid JSON, a bare JSON list is wrapped as `{"highlights": ...}`. A validated empty highlight list, either shape, is zero windows and does not call the heuristic. Non-maximum suppression runs on the padded intervals, not the raw ones.

5. **Job status is forward-only.** States: `queued`, `downloading`, `transcribing`, `scoring`, `clipping`, `completed`, `failed`. `transition_job()` is the only writer of the non-failed states. `fail_job()` is the only writer of `failed` and of `jobs.error`, and it is a no-op if the row is already terminal. Nothing leaves `completed` or `failed`. `started_at` is set only when entering `downloading`, not on every leave from `queued`.

6. **One bad clip cannot fail the job.** Each clip is its own `try/except`. The job completes even if every clip export fails or the scorer finds nothing. A timeout or a download/transcription failure fails the job. Clips already exported stay visible under a failed job.

7. **Whisper audio is split into 10-minute WAV chunks.** A 240-minute 16 kHz mono WAV is ~440 MB and the Whisper API rejects bodies over 25 MB. The `Transcriber` interface still takes one path and returns `Word[]`; chunking is inside the Whisper implementation.

8. **Transcript lives in `transcript.json` on the job directory, not in Postgres.** It is deleted with the raw VOD at terminal state. Clip rows keep the editorial result. This avoids a multi-megabyte JSONB column that MVP has no way to delete later.

9. **Two ffmpeg passes per clip, as the blueprint specifies, plus loudness normalization on the first pass.** Pass 1: re-encode cut, even center-crop, scale to 1080×1920, `loudnorm`. Pass 2: burn ASS, `-c:a copy`. ASS times are relative to the file produced by `-ss`, not to the VOD.

10. **Celery results are ignored, and a kill resumes.** `process_job.delay(job_id)` passes only the id. Phase 0 already sets `backend=settings.redis_url`, so `task_ignore_result=True` stays on even though nothing reads results; dropping it would persist return values in Redis. Domain failures are caught and acked. A SIGKILL is not caught: late acks requeue. `jobs.attempt` stops the loop after 3 executions. Redis `broker_transport_options.visibility_timeout` is 7200 seconds, greater than `task_time_limit` (2760). The Redis default of 3600 is only 840 seconds above that hard limit and would redeliver a task that is still running. The worker disposes the SQLAlchemy pool inside `worker_process_init` after prefork so children do not inherit the parent's sockets.

11. **No new backend libraries.** HS256 verify is the ordered procedure in [Phase 5 — Auth](#auth): padding restored, raw 32-byte digest, `hmac.compare_digest`, UTF-8 key bytes (not hex-decoded). A `jose`-signed fixture locks it. PyJWT was considered and rejected: the procedure is short, and a library would not remove the need for that fixture. Passwords are `hashlib.scrypt` with `n=2**17` (OWASP's interactive scrypt choice; `n=16384` is too cheap to state without a reason). Email is a small explicit check, not `EmailStr`, so `email-validator` is not added.

12. **Captions use Liberation Sans, not Arial.** `python:3.11-slim` has no Arial, and libass silently drops missing fonts. Phase 4 installs `fonts-liberation` and sets `Fontname` to `Liberation Sans`. Size, outline, alignment, and `MarginV` stay as specified in `docs/ARCHITECTURE.md` §3.4.

13. **`AGENTS.md` §11's one-kill acceptance line is overridden by the architecture failure table and by Phase 0's Celery flags.** §11 says one kill marks the job `failed` and cleans disk. §7 says a crashed worker requeues and fails on max retries (3). Phase 0 already set `task_acks_late` and `task_reject_on_worker_lost`, which requeue. This design follows §7: one kill resumes when `vod.mp4` or `transcript.json` or `windows.json` is valid; disk is removed only at a terminal state; the job fails when `attempt` exceeds 3, when the worker catches `SoftTimeLimitExceeded`, or when the reaper sees no live lock and `started_at` older than 45 minutes. The README tells QA not to expect failure after a single kill. Every `subprocess.run` passes `start_new_session=False` (the default, stated so nobody "fixes" it). `start_new_session=True` would let ffmpeg survive `docker compose kill`.

14. **The access token is readable by page JavaScript on purpose, and it lasts 24 hours.** JSON stays a browser call to FastAPI because Phase 0 already shipped that CORS allow-list. Clip bytes still go through the Next media proxy, because a `<video>` tag cannot set `Authorization`. `AUTH_SECRET` (Auth.js cookie encryption) and `JWT_SECRET` (API HMAC) are different secrets. Sharing them would make one leak forge both the cookie and the API token. Every threat-table row uses that 24-hour lifetime. A 7-day token is rejected (`exp - iat` above 26 hours).

---

## Proposed Design

### System context

```mermaid
flowchart LR
  subgraph vercel [Vercel]
    Web[Next.js App Router]
    AuthJs[Auth.js Credentials]
    MediaProxy["/api/media/clips/id"]
  end
  subgraph vps [VPS compose behind Caddy]
    Api["FastAPI :8000 /api/v1"]
    Worker[Celery worker concurrency 1]
    Beat[Celery beat]
    Pg[(PostgreSQL 16)]
    Redis[(Redis 7)]
    Disk["/storage volume"]
  end
  Web -->|Bearer HS256 JSON| Api
  AuthJs -->|login or register| Api
  MediaProxy -->|Bearer plus Range| Api
  Web --> MediaProxy
  Api -->|process_job.delay job_id| Redis
  Redis --> Worker
  Beat --> Redis
  Api --> Pg
  Worker --> Pg
  Worker --> Disk
  Api -->|FileResponse clips only| Disk
  Worker --> YtDlp[yt-dlp]
  Worker --> Whisper[Whisper API]
  Worker --> Llm[User OpenAI-compatible endpoint]
  Worker --> Ffmpeg[ffmpeg]
```

Browser JSON goes straight to FastAPI. CORS in `backend/app/main.py` already allows the frontend origin plus `Authorization`. A `<video src>` cannot set `Authorization`, so clip bytes are same-origin through the Next route handler, which attaches the bearer token and forwards `Range`.

### Job state machine

```mermaid
stateDiagram-v2
  [*] --> queued
  queued --> downloading
  downloading --> transcribing
  transcribing --> scoring
  scoring --> clipping
  clipping --> completed
  queued --> failed
  downloading --> failed
  transcribing --> failed
  scoring --> failed
  clipping --> failed
  completed --> [*]
  failed --> [*]
```

`queued → failed` covers enqueue failure after the row exists, and the attempt cap. There is no backward edge and no extra status for "downloaded" or "partial". Each phase's temporary stub is deleted by the next phase (see the PR plan).

`transition_job()` writes every non-failed status. `fail_job()` is the only function that sets `failed` or `jobs.error`. Progress updates exist for the dashboard. They do not hold the reaper off, and they do not extend the 45-minute cap. `updated_at` moving during a chunk is not a liveness signal. Liveness is the Redis lock in [Phase 6](#phase-6--hardening).

```python
ORDER = ("queued", "downloading", "transcribing", "scoring", "clipping", "completed")

def transition_job(session: Session, job: Job, new_status: str, message: str) -> None:
    if new_status == "failed":
        raise InvalidTransition(job.status, new_status)
    if job.status in {"completed", "failed"}:
        raise InvalidTransition(job.status, new_status)
    if ORDER.index(new_status) <= ORDER.index(job.status):
        raise InvalidTransition(job.status, new_status)
    job.status = new_status
    job.progress_message = message
    job.updated_at = datetime.now(timezone.utc)
    if new_status == "downloading" and job.started_at is None:
        job.started_at = job.updated_at
    if new_status == "completed":
        job.finished_at = job.updated_at

def fail_job(job_id: UUID, error_code: str, user_message: str) -> None:
    with session_scope() as session:
        job = session.get(Job, job_id, with_for_update=True)
        if job is None or job.status in {"completed", "failed"}:
            return
        now = datetime.now(timezone.utc)
        job.status = "failed"
        job.error = redact(user_message)[:500]
        job.finished_at = now
        job.updated_at = now
        platform = job.platform
        origin = job.started_at or job.created_at
        elapsed_s = round((now - origin).total_seconds(), 2) if origin else 0.0
    log.info("job_failed", job_id=str(job_id), error_code=error_code, platform=platform, elapsed_s=elapsed_s)
```

Progress strings the UI prints verbatim:

| When | `progress_message` |
|---|---|
| Row created | `Queued` |
| Download starts | `Downloading the VOD` |
| Metadata known | `Downloading · {title} · {minutes} min` |
| Whisper chunk | `Transcribing speech ({n}/{chunks})` |
| Scoring starts | `Scoring transcript for highlights` |
| Score chunk | `Scoring highlights ({n}/{chunks})` |
| Each clip | `Cutting clip {i} of {n}` |
| Success with clips | `Ready · {ready} clips` |
| Success, model returned nothing | `No clip-worthy moments in this VOD.` |
| Success, every export failed | `Finished, but every clip failed to export.` |

`jobs.error` is the failure banner. It is `NULL` on success. `progress_message` stays on the last step when a job fails so the detail page can show both.

### End-to-end sequence

```mermaid
sequenceDiagram
  participant UI as Dashboard
  participant API as FastAPI
  participant DB as Postgres
  participant Q as Redis
  participant W as process_job

  UI->>API: POST /api/v1/jobs {url} Bearer
  API->>API: detect_platform, key, rate, disk
  API->>DB: INSERT status=queued
  API->>Q: delay(job_id) only
  API-->>UI: 202 {job_id}
  W->>DB: attempt += 1, status=downloading
  W->>W: yt-dlp probe then download
  alt too long, live, sub-only, or yt-dlp error
    W->>DB: status=failed, error set
    W->>W: cleanup raw files
  else downloaded
    W->>DB: status=transcribing
    W->>W: ffmpeg wav, Whisper chunks, transcript.json
    W->>DB: status=scoring
    W->>W: decrypt key, LLM or heuristic
    W->>DB: status=clipping
    loop each kept window
      W->>W: try cut, crop, loudnorm, ASS, burn
      W->>DB: INSERT clip ready or failed
    end
    W->>DB: status=completed
    W->>W: delete vod, wav, transcript, work/
  end
```

### Rejection split (POST vs worker)

`docs/ARCHITECTURE.md` §3.1 lists three user-facing rejections (bad platform, too long, Twitch sub-only) under `services/downloader.py`. §7 says a too-long VOD is rejected at `POST /jobs` with no row. §5 says POST validates the URL and that an active key exists, inserts `queued`, enqueues, and returns 202. `AGENTS.md` phase 1 puts those rejections in `downloader.py` and golden rule 2 forbids long work on the HTTP path. A yt-dlp `--dump-single-json` probe is a network call: commonly 2–15s, with a long tail when the extractor hangs. That cannot run inside POST.

**Rejected before a job row exists** (no yt-dlp, target p99 under 100 ms excluding client RTT):

| Check | Where | HTTP | Row |
|---|---|---|---|
| Missing, bad, or expired JWT | `get_current_user` | 401 `Sign in required.` | no |
| No `api_keys` row | `api/jobs.py` | 400 `Add an API key before generating clips.` | no |
| URL not https, has userinfo (`user:pass@host`), is an IP or localhost, or `detect_platform` fails | `downloader.detect_platform` called from `api/jobs.py` | 400 `Only Twitch VODs and YouTube videos are supported.` | no |
| 3 jobs in `queued\|downloading\|transcribing\|scoring\|clipping` | `api/jobs.py`, under `pg_advisory_xact_lock` | 429 `You already have 3 jobs generating. Wait for one to finish.` | no |
| 10 jobs with `created_at` since 00:00 UTC | same transaction and lock as the row above | 429 `Daily limit reached (10 jobs). Try again tomorrow.` | no |
| `shutil.disk_usage(STORAGE_DIR).free < 8 GiB` | `api/jobs.py` | 503 `The server cannot store this VOD right now. Try again later.` | no |
| Redis `delay()` throws after insert | `api/jobs.py` | 503, and the row is then marked `failed` | yes, terminal |

The count-and-insert runs in one transaction. Before counting, take `pg_advisory_xact_lock(hashtext(user_id::text))` so two parallel POSTs cannot both pass the cap. `get_session` commits when a normal route returns, which is enough for register and for a rejected POST that inserted nothing. `POST /api/v1/jobs` cannot wait for that final commit, and `fail_job` opens its own session, so an uncommitted insert is invisible to it:

1. Insert the `queued` row (`platform` set, `enqueued_at` NULL) and `session.commit()` before `delay()`. That releases the advisory lock and makes the row durable.
2. Call `delay(job_id)`. If it raises, `fail_job(..., "enqueue_failed", "Could not start the job. Try again in a moment.")` and return 503. Do not also write that row from the request session.
3. If `delay()` returns, set `enqueued_at` in a short `session_scope()` and return 202.

A crash after that commit and before `delay()` leaves `queued` with `enqueued_at` NULL. After 3 minutes, the reaper checks Redis (`joblock:{id}`, `unacked`, and the `celery` queue) and only fails the row with `enqueue_failed` if the message is genuinely absent from the broker. It does not use the 45-minute timeout copy. A queued job whose message is present in Redis is waiting on a worker and is left alone regardless of age.

**Failed by the worker after a row exists** (same copy as the architecture doc, interpolated max minutes so the default still renders the §3.1 sentence exactly):

| Condition | `error_code` | `jobs.error` |
|---|---|---|
| `duration_s > MAX_VOD_MINUTES * 60` | `vod_too_long` | `VOD is too long (max {max_vod_minutes} min in this build).` The default setting renders `max 240 min`. |
| Duration missing or `is_live` / `live_status == "is_live"` | `live_or_unknown_length` | `Live streams aren't supported. Paste the VOD after the stream ends.` when live; `Could not read the video length, so it was not downloaded.` when duration is missing |
| Probe or download stderr matches subscriber-only | `sub_only` | `This VOD is subscriber-only — ask the streamer or use a public VOD.` |
| Private, removed, geo, age-gate, extractor error | `download_failed` | `Could not download this video. It may be private, removed, or blocked.` |
| `subprocess.TimeoutExpired` on probe or download | `download_failed` | `The download timed out. Try again.` A timeout is not a non-zero yt-dlp exit and must not fall through to the generic internal sentence. |
| Probe says the extractor is not YouTube or a Twitch VOD | `unsupported_url` | `Only Twitch VODs and YouTube videos are supported.` |

Duration is checked on the probe JSON **before** the media download, so a 10-hour URL never starts a multi-gigabyte fetch. Sub-only is often only visible once the download starts; that path deletes any partial file and fails the job. Platform is checked again in the worker as defense in depth, but the HTTP check means a Vimeo URL never creates a row.

Disk-full is a `stat` and stays on POST. It is not a yt-dlp call.

A two-second probe budget inside POST was considered and rejected: a timeout would 503 URLs that would have succeeded, and a success past the budget would violate the millisecond return. The dashboard shows the worker's `error` on the new job within one poll after the probe (typically a few seconds), which is the graceful UI. The log line is `job_failed` with `error_code`, `job_id`, and `platform` — not the raw URL.

### Storage layout

All paths are under `settings.storage_dir` (`STORAGE_DIR=/storage`, bind `./storage:/storage`). Ids are UUIDs. No user-controlled path segment is ever concatenated.

```
/storage/jobs/{job_id}/vod.mp4          # yt-dlp output, deleted at terminal state
/storage/jobs/{job_id}/audio.wav         # 16 kHz mono, deleted at end of transcribe
/storage/jobs/{job_id}/chunks/{n}.wav    # whisper slices, deleted per chunk
/storage/jobs/{job_id}/transcript.json   # Word[], deleted at terminal state
/storage/jobs/{job_id}/windows.json      # ScoredWindow[], no key, deleted at terminal state
/storage/jobs/{job_id}/work/{clip_id}.ass
/storage/jobs/{job_id}/work/{clip_id}_raw.mp4
/storage/jobs/{job_id}/clips/{clip_id}.mp4   # kept
```

`clips.file_path` stores the relative POSIX path `jobs/{job_id}/clips/{clip_id}.mp4`. Absolute paths are not stored. `resolve_under_storage()` does `Path(storage_dir, relative).resolve()` and rejects anything whose resolved path is not under `Path(storage_dir).resolve()`. The video route refuses to open `vod.mp4` even if a row were poisoned: it only serves a clip row's `file_path` after the ownership check.

Peak disk for one job, concurrency 1: a 2-hour 1080p VOD is typically 1.5–3 GB, a full 240-minute file can reach ~6 GB, the WAV is ~440 MB and is removed before scoring, eight final clips are on the order of 8 × 30–80 MB. Require **8 GiB free** before insert. Operator volume recommendation: **20 GB** minimum on the VPS. `--max-filesize 8G` on yt-dlp is a backstop, not the product limit.

End-state `cleanup_raw(job_id)` deletes `vod.*`, `audio.wav`, `chunks/`, `transcript.json`, `windows.json`, and `work/`. It keeps `clips/`. It is idempotent. It runs after the terminal status **commits**, and only when this worker holds the job lock or the reaper has confirmed the lock is absent. A cleanup exception is logged as `cleanup_failed` and does not change a `completed` job back to `failed`. The beat task retries cleanup for terminal jobs that still have a `vod.mp4`. Until PR 9 the function body is `return`. Earlier PRs still call it from `finally`, so the call site does not change when deletion is turned on. Do not delete files out from under a lock that is still held.

### Pipeline task

`backend/app/workers/pipeline.py`, imported by Celery (not autodiscovered by magic). Extend the existing config in `backend/app/workers/celery_app.py`; do not weaken the Phase 0 flags.

This is the end-state config. Do not copy later keys into earlier PRs. PR 4 adds `task_ignore_result`, `worker_hijack_root_logger`, `worker_concurrency`, `visibility_timeout`, `task_routes`, and `imports`. PR 5 adds the soft and hard time limits. PR 9 adds `worker_max_tasks_per_child`, `worker_max_memory_per_child`, and `beat_schedule`. The four Phase 0 flags stay as they are in `backend/app/workers/celery_app.py`.

```python
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_ignore_result=True,  # result backend is already configured; do not drop this
    worker_hijack_root_logger=False,
    worker_concurrency=1,
    worker_max_tasks_per_child=1,  # PR 9
    worker_max_memory_per_child=2_500_000,  # KB, about 2.4 GiB, PR 9
    task_soft_time_limit=2700,  # PR 5
    task_time_limit=2760,  # PR 5
    broker_transport_options={"visibility_timeout": 7200},  # PR 4, must exceed task_time_limit
    task_routes={  # PR 4 (reap_stale_jobs added in PR 9)
        "process_job": {"queue": "celery"},
        "reap_stale_jobs": {"queue": "reaper"},
    },
    imports=["app.workers.pipeline"],  # PR 4
)
```

`backend/app/db.py` builds `engine` at import. Celery's default pool is prefork, and the parent imports that engine before it forks. In `worker_process_init`, after `validate_runtime_secrets()`:

```python
engine.dispose(close=False)
engine = make_engine(settings.database_url, pool_size=2, max_overflow=0)
SessionLocal.configure(bind=engine)
```

`dispose(close=False)` drops inherited sockets without closing the parent's connections. The parent must not check out a connection before fork (no query at import). The API process is not forked and keeps `pool_size=5`, `max_overflow=5`. Do not construct a second worker engine at import time.

Worker command (set in PR 4, not only in phase 6):

```text
celery -A app.workers.celery_app:celery_app worker --loglevel=INFO --concurrency=1 -Q celery
```

```python
@celery_app.task(name="process_job", max_retries=0)
def process_job(job_id: str) -> None:
    decision, lock_token = begin_attempt(job_id)
    if decision == "skip":
        return
    stop_event: threading.Event | None = None
    refresh_thread: threading.Thread | None = None
    try:
        if decision == "stop":
            return  # finally cleans up raw files and releases the lock before ack
        stop_event, refresh_thread = start_lock_refresher(job_id, lock_token)
        run_pipeline(job_id, lock_token)
    except SoftTimeLimitExceeded:
        cancel_score_pool()  # shutdown(wait=False, cancel_futures=True); in-flight HTTP may finish
        fail_job(job_id, "timeout", "This job exceeded the 45 minute limit and was stopped.")
    except ClipForgeError as exc:
        fail_job(job_id, exc.error_code, exc.user_message)
    except Exception:
        log.exception("job_crashed", job_id=job_id)
        fail_job(job_id, "internal", "Something went wrong while processing this VOD.")
    finally:
        if stop_event is not None and refresh_thread is not None:
            stop_event.set()
            refresh_thread.join(timeout=5.0)
        try:
            if is_terminal(job_id) and holds_job_lock(job_id, lock_token):
                cleanup_raw(job_id)
        finally:
            release_job_lock(job_id, lock_token)
```

`max_retries=0` because domain failures must not be retried by Celery. Crash retries come from `task_reject_on_worker_lost`, not from `self.retry`.

`begin_attempt` executes in this exact order:

1. **Terminal check:** read the `jobs` row. If missing or `status` is `completed` or `failed`, return `("skip", None)` without touching the lock or incrementing `attempt` (duplicate delivery after a normal ack).
2. **Acquire lock or requeue:** try `SET joblock:{job_id} {hostname}:{pid}:{instance_id} NX EX 60`, where `instance_id` is generated once per worker child process in `worker_process_init`.
   - If `SET NX` fails, `GET joblock:{job_id}`:
     - If missing (expired in the race), loop once and retry `SET NX EX 60`.
     - Parse `{holder_host}:{holder_pid}:{holder_instance}` with `rsplit(":", 2)`. If `holder_host == socket.gethostname()` and either `holder_pid` is dead on this host (not alive in `/proc` / `os.kill(pid, 0)` or zombie) or (`holder_pid == os.getpid()` and `holder_instance != instance_id`, e.g. container restart reusing the same PID), run a Lua compare-and-delete (`if redis.call("get", KEYS[1]) == ARGV[1] then return redis.call("del", KEYS[1]) else return 0 end`) and immediately retry `SET NX EX 60`.
     - Otherwise (held by another live process or another host), schedule `process_job.apply_async(args=[job_id], countdown=15)` so the task yields the concurrency-1 slot without tight-looping and wakes well before the 60s TTL, then return `("skip", None)` to ack the current delivery without incrementing `attempt`.
3. **Increment `attempt`:** inside a short `session_scope()` taking `SELECT ... FOR UPDATE`, re-check terminal status (if terminal, release the lock and return `("skip", None)`), increment `jobs.attempt += 1`, update `updated_at`, and commit **before** any download.
4. **Enforce attempt cap:** if `attempt > 3`, call `fail_job(job_id, "worker_lost", "The worker restarted while this job was running. Please try again.")` and return `("stop", lock_token)`. The `try`/`finally` block in `process_job` runs `cleanup_raw(job_id)` and releases the lock before acking.
5. Otherwise return `("run", lock_token)`. Three executions get to do work; the fourth delivery only records the failure and cleans up. That matches `docs/ARCHITECTURE.md` §7's "max retries (3)" as three runs, then stop.

While running, `start_lock_refresher` spawns a daemon thread that refreshes `joblock:{job_id}` every 20 seconds (and `run_pipeline` also refreshes on stage changes) using a Lua compare-and-set expire (`if redis.call("get", KEYS[1]) == ARGV[1] then return redis.call("expire", KEYS[1], 60) else return 0 end`). Because `EXPIRE` never creates a missing key, a late tick cannot recreate a deleted lock. Before `release_job_lock` runs, `finally` sets `stop_event` and joins the refresh thread (`timeout=5.0`). `release_job_lock` uses Lua compare-and-delete matching `lock_token` so an expired holder never deletes a newer worker's lock. The Redis lock is not a substitute for the database row lock; the row lock is held only for short transactions.

Resume rules when a redelivery sees a non-terminal job (OOM or deploy mid-job). Because `transition_job` rejects non-advancing transitions, `run_pipeline` only calls `transition_job` when `ORDER.index(target_status) > ORDER.index(job.status)` (a resumed job may already be in `transcribing`, `scoring`, or `clipping`). A retry does **not** call the user's LLM again when `windows.json` validates:

- If `vod.mp4` exists and `ffprobe` reports a duration within 2s of `jobs.duration_s`, skip download.
- If `transcript.json` parses, skip transcription.
- If `windows.json` parses as a list of `ScoredWindow` (including a valid empty list), skip scoring. Missing or invalid `windows.json` scores again and spends the user key. That second spend is allowed and is capped by the 3 executions.
- At the start of clipping, delete existing clip rows for that job and files under `clips/` and `work/` so a partial export is not duplicated. Then clip again.
- Do not delete the VOD, the transcript, or `windows.json` at the start of a retry.

`SoftTimeLimitExceeded` is caught, so the task acks and is **not** requeued. The soft-limit handler is the timeout owner for a job this process is running. An OOM SIGKILL is not caught, the message is rejected, and `attempt` advances. Those two paths must stay distinct. The scoring thread pool does not outlive the handler: `shutdown(wait=False, cancel_futures=True)` runs before `fail_job`. Worker threads must not touch a SQLAlchemy session. They return chunk results; the main thread writes `progress_message` as each future completes.

Sessions are short. Open a `session_scope()` around each transition and close it. Do not hold one SQLAlchemy session across Whisper or ffmpeg.

### Domain errors

`backend/app/errors.py`:

```python
class ClipForgeError(Exception):
    error_code: str
    user_message: str

class UnsupportedUrl(ClipForgeError): ...
class VodTooLong(ClipForgeError): ...
class SubscriberOnly(ClipForgeError): ...
class DownloadFailed(ClipForgeError): ...
class TranscriptionFailed(ClipForgeError): ...
class NoSpeech(ClipForgeError): ...
class InvalidStoredKey(ClipForgeError): ...
```

Services raise these. They do not catch them. `user_message` is safe to store on `jobs.error` after `redact()`. Tracebacks go to logs only. Unhandled exceptions use the generic internal sentence so a library message cannot echo a key into the dashboard.

Stable `error_code` values: `unsupported_url`, `vod_too_long`, `live_or_unknown_length`, `sub_only`, `download_failed`, `transcribe_failed`, `no_speech`, `no_word_timestamps`, `timeout`, `worker_lost`, `enqueue_failed`, `disk_full`, `key_decrypt_failed`, `internal`.

### External-call retries

`AGENTS.md` §10: tenacity, max 2 retries, exponential backoff, only where retryable. Interpret "retry ×2" as one initial attempt plus two retries (`stop_after_attempt(3)`), `wait_exponential(multiplier=1, min=1, max=8)`.

| Call | Retry | Do not retry |
|---|---|---|
| yt-dlp probe or download | non-zero exit that is not classified as sub-only, private, too long, or unsupported. `TimeoutExpired` is not retried; it is `download_failed` with `The download timed out. Try again.` | classified failures and timeouts |
| Whisper HTTP | timeout, 429, 5xx | 401, 400 |
| LLM transport | timeout, 429, 5xx, then that chunk becomes heuristic | n/a — never fails the job |
| LLM JSON shape | exactly one extra call with the corrective sentence, then heuristic | a validated empty highlight list, including a bare `[]` accepted as `highlights` |
| ffmpeg | no tenacity retry | one failure fails that clip only |

OpenAI SDK clients are constructed with `max_retries=0` so SDK retries do not multiply tenacity.

---

### Phase 1 — Ingestion

**Exit check:** a public URL creates a job in milliseconds, the worker writes `/storage/jobs/{id}/vod.mp4` plus metadata columns, bad platforms never insert, an over-long probe fails the job before a media download, and the task arguments are exactly `[job_id]`.

#### `detect_platform(url: str) -> Literal["twitch", "youtube"]`

Pure function in `backend/app/services/downloader.py`. Parse with `urllib.parse`. Require scheme `https`. Reject userinfo, reject hosts that are IP literals, reject `localhost`. Strip one trailing slash from the path before matching.

Accept:

- YouTube host `youtube.com`, `www.youtube.com`, `m.youtube.com`, `youtu.be`, `www.youtu.be`, `music.youtube.com`.
  - `youtu.be/{id}` where `id` matches `[A-Za-z0-9_-]{11}`.
  - `/watch` with `v=` of the same shape.
  - `/shorts/{id}` and `/live/{id}` of the same shape.
  - Reject bare channels, `/playlist` without a video id, `/@handle`, `/feed`, and `/embed/{id}`. Embed URLs are rejected on purpose; the paste target is a watch, short, live, or youtu.be link.
- Twitch host `www.twitch.tv`, `twitch.tv`, or `m.twitch.tv`, path `/videos/{digits}` after the trailing-slash strip. Query strings including `t=` are allowed and do not change the decision. Reject `clips.twitch.tv`, `/clip/`, and `twitch.tv/{channel}`.

`POST /api/v1/jobs` writes `jobs.platform` from `detect_platform` in the INSERT. The column is `NOT NULL`. Title and duration stay `NULL` until the worker probe. Anything else raises `UnsupportedUrl` with the architecture sentence. Unit-test this table, including a trailing slash, `m.twitch.tv`, and a rejected `/embed/` URL. Do not hit the network.

#### `probe_and_download(url: str, job_dir: Path) -> DownloadResult`

```python
@dataclass(frozen=True)
class DownloadResult:
    path: Path
    platform: Literal["twitch", "youtube"]
    title: str
    duration_s: float
    thumbnail_url: str | None
```

Probe argv (no shell, timeout 60s):

```text
yt-dlp --skip-download --dump-single-json --no-playlist --no-warnings --no-progress <url>
```

Parse stdout as JSON. Read `duration` (seconds), `title`, `thumbnail`, `is_live`, `live_status`, `extractor` / `extractor_key`. Apply the worker rejection table before any media fetch. Title stored on the job is stripped and truncated to 300 characters. Thumbnail URL must be `https` or it is stored as `NULL`.

Download argv (timeout 2400s; the 45-minute task limit is the real backstop):

```text
yt-dlp --no-playlist --no-warnings --no-progress --newline --restrict-filenames
  --merge-output-format mp4
  --format bv*[height<=1080]+ba/bv*+ba/b
  --max-filesize 8G
  --output /storage/jobs/{job_id}/vod.%(ext)s
  <url>
```

That format string is the one in `docs/ARCHITECTURE.md` §3.1. If the merge yields `vod.mkv` or `vod.webm`, remux once with ffmpeg (`-c copy`) to `vod.mp4` and delete the other file. If the file is still not an mp4, raise `DownloadFailed`. Run `ffprobe` afterwards and persist `duration_s` from the container if it disagrees with the probe by more than 2 seconds; keep the probe value for the length cap decision so a container lie cannot bypass the cap after a full download. If ffprobe duration exceeds the cap, delete the file and raise `VodTooLong`.

Subscriber-only classification: case-insensitive stderr contains `subscriber-only`, `subscribers only`, or `sub-only`. Do that classification on both probe and download.

Do not pass `--cookies`, `--username`, `--password`, or a netrc. Do not log argv (it contains the URL). Log `download_done` with `job_id`, `platform`, `duration_s`, `bytes`.

#### Task wiring and the phase-1 stub

`POST` already stored `platform`. `run_pipeline` in phase 1 calls `probe_and_download`, writes `title`, `duration_s`, and `thumbnail_url`, then hits a stub that phase 2 deletes:

```python
# PHASE 1 ONLY. Deleted in phase 2 when transcribe is real.
STUB_AFTER_DOWNLOAD = True

if STUB_AFTER_DOWNLOAD:
    raise ClipForgeError(
        "internal",
        "Download finished. Later stages are not included in this build yet.",
    )
```

Leaving the job in `downloading` would poll forever. Marking it `completed` would poison the state machine. A `failed` row with that exact sentence is the verifiable checkpoint, and the dashboard already knows how to show `jobs.error`. Phase 2 removes the constant and the test that asserts it.

Metadata columns are written **before** the stub raises, inside a committed transaction, so a test can assert them on the failed row.

#### Phase 1 tests

`backend/tests/test_downloader.py`, `test_jobs_api.py`, `test_pipeline.py`. No network. Monkeypatch `subprocess.run`.

- Platform table: the accept and reject URLs above, plus `javascript:`, `http://`, `https://user:pass@youtube.com/watch?v=...`, `https://127.0.0.1/`.
- Probe JSON with `duration: 241 * 60` raises `VodTooLong` and the download argv was not used.
- Probe JSON with `is_live: true` raises before download.
- Stderr `This VOD is subscriber-only` maps to `SubscriberOnly` and the partial file is gone.
- Happy path: fake yt-dlp writes a tiny file; result fields match.
- `POST /api/v1/jobs` with a mocked `delay` returns 202, inserts `queued`, and `delay` was called once with the uuid string only. A second test asserts yt-dlp was not started on the request (the monkeypatch fails the test if called).
- Unsupported URL returns 400 and `SELECT count(*) FROM jobs` is unchanged.
- Missing key returns 400 and does not enqueue.
- Fourth concurrent active job returns 429.
- `delay` raising marks the row `failed` and returns 503.
- `test_process_job_stops_after_download` runs the task in-process against a fake yt-dlp and asserts the stub error, the mp4 path, and `attempt == 1`.

---

### Phase 2 — Transcription

**Exit check:** after a successful download the job enters `transcribing`, `WhisperApiTranscriber.transcribe(path)` returns `list[Word]`, a second chunk's timestamps are shifted by the chunk offset, empty speech fails the job, and the OpenAI client receives the user's key when the stored host is `api.openai.com`. This phase then fails the job on `STUB_AFTER_TRANSCRIBE` so the row does not stay in `transcribing`.

#### Interface

`backend/app/services/transcribe.py`:

```python
@dataclass(frozen=True)
class Word:
    word: str
    start: float
    end: float

class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> list[Word]: ...

class WhisperApiTranscriber:
    def __init__(self, api_key: str, language: str = "en") -> None: ...
    def transcribe(self, audio_path: Path) -> list[Word]: ...
```

The protocol takes the media path the pipeline already has. The Whisper implementation extracts WAV itself so a future `FasterWhisperTranscriber` can read the mp4 or the wav without the pipeline changing. Callers depend on `Transcriber` and `Word` only. Do not import `openai` from `pipeline.py`.

#### WAV and the 25 MB limit

```text
ffmpeg -hide_banner -nostdin -loglevel error -y -i vod.mp4 -vn -ac 1 -ar 16000 -c:a pcm_s16le audio.wav
```

Timeout 600s. 16 kHz mono s16le is 32,000 bytes/s. A 10-minute slice is ~19.2 MB, under the Whisper 25 MB body limit. A 240-minute WAV is ~440 MB, so it is never uploaded whole.

Slice with stream copy, not a re-encode:

```text
ffmpeg -y -ss {offset} -t 600 -i audio.wav -c copy chunks/{n}.wav
```

Offsets are `0, 600, 1200, ...` until the ffprobe WAV duration is covered. After each successful Whisper call, delete that chunk file. After the last chunk, delete `audio.wav`. Keep `transcript.json`.

For each slice, add `offset` to every returned `start` and `end`. Drop words where `word.strip()` is empty or `end < start`. Force `end = max(end, start)`. Sort by `start`. Do not invent timestamps from segments. If the API returns no `words` array, raise `TranscriptionFailed` with `error_code=no_word_timestamps` and user message `Transcription did not return word timestamps.` Captions cannot be faked from segment ranges.

Whisper request, per chunk:

```python
http_client = httpx.Client(
    follow_redirects=False, timeout=httpx.Timeout(120.0, connect=5.0)
)
client = OpenAI(
    api_key=whisper_key,
    base_url="https://api.openai.com/v1",
    http_client=http_client,
    max_retries=0,
)
with chunk.open("rb") as handle:
    result = client.audio.transcriptions.create(
        model="whisper-1",
        file=handle,
        response_format="verbose_json",
        timestamp_granularities=["word"],
        language=whisper_language,  # omitted entirely when the setting is ""
    )
```

`WHISPER_LANGUAGE` defaults to `en`. pydantic-settings treats an unset variable as that default, not as "omit". Only an explicit empty value (`WHISPER_LANGUAGE=` in the environment) omits the `language` argument and lets the API detect. Any other non-empty value is passed as the hint. There is no per-job language picker. `.env.example` says: leave unset for English; set empty to auto-detect.

`model` stays `whisper-1`. Do not switch to `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, or `gpt-transcribe` in this phase. As of this design's date, word timestamps via `timestamp_granularities` are documented for `whisper-1` only, and that model is scheduled to leave the API on 2027-02-26 (see Risks). A replacement needs a fixture that actually contains word-level `start`/`end` before anyone changes this call.

Which key, in order. This matches `.env.example`: user BYOK is primary, `OPENAI_API_KEY` is the fallback.

1. If the user's stored provider host is `api.openai.com`, decrypt the user key inside the worker and pass it only as the Whisper client `api_key`. Register it with `push_secret` for the redaction processor. An operator key that is also set is not used.
2. Else if `settings.openai_api_key` is non-empty, use that operator key. It is never written to the job, the task, or a log. Register it with `push_secret` too.
3. Else `fail_job` `transcribe_failed` with `Transcription needs an OpenAI Whisper key on the server, or an OpenAI key saved in your account. Other providers can score clips but cannot transcribe in this build.`

Scoring never uses `OPENAI_API_KEY`. A Groq or Together key is not sent to `api.openai.com`. The Whisper client uses `follow_redirects=False` like every other outbound SDK client.

Tenacity around the create() call as specified above. After retries, `Transcription failed. Try again in a few minutes.` If the merged word list is empty, `No speech was detected in this VOD.` (`no_speech`). Both fail the job before scoring, so the user is not billed a few hundred LLM calls for silence.

`transcript.json` is a JSON list of `{word, start, end}`. Pretty-print is unnecessary. On resume, if the file validates against that shape, do not call Whisper again.

Progress is updated at the start of each chunk so the dashboard can show `Transcribing speech ({n}/{chunks})`. That write does not keep the reaper away and does not extend the 45-minute cap. The cap is `started_at` plus the worker soft limit, or the reaper when the Redis lock is gone (phase 6). Refresh the Redis lock on the same cadence; refreshing the lock is not a database heartbeat.

Write `transcript.json` and commit that status before the phase-2 stub. Phase 3 deletes the constant.

```python
# PHASE 2 ONLY. Deleted in phase 3 when scoring is real.
STUB_AFTER_TRANSCRIBE = True

if STUB_AFTER_TRANSCRIBE:
    raise ClipForgeError(
        "internal",
        "Transcription finished. Later stages are not included in this build yet.",
    )
```

#### Phase 2 tests

- Fixture verbose JSON for two chunks. Second chunk words are shifted by 600s. Empty tokens dropped. Out-of-order words sorted.
- Builder asserts the ffmpeg wav argv contains `-ac`, `1`, `-ar`, `16000`, and does not contain a shell metacharacter.
- Chunk count for a 1500s wav is 3 (600+600+300).
- Missing `words` raises `no_word_timestamps` and does not build a `Word` from `segments`.
- Empty list raises `NoSpeech`.
- Key selection: user OpenAI key is used even when `OPENAI_API_KEY` is also set; operator key is used only when the stored host is not `api.openai.com`; Groq user key with an empty operator key fails without constructing a client (factory spy).
- Pipeline test: download stub removed, fake transcriber, status sequence includes `transcribing`, `transcript.json` contents match, `audio.wav` is gone, and the job is `failed` with `Transcription finished. Later stages are not included in this build yet.`
- Canary: the Whisper factory receives the canary and a mocked exception whose message includes the canary. `jobs.error` and the log processor output do not contain the contiguous canary.

---

### Phase 3 — Scoring

**Exit check:** chunks follow the 45s/40s grid, the system prompt matches `docs/ARCHITECTURE.md` §3.3, the model is never given timestamps, pydantic rejects a seconds-based payload, one bad JSON reply is retried once, the second failure tags heuristic clips, overlaps collapse to the higher score, and at most `MAX_CLIPS_PER_JOB` windows remain. Windows are padded ±8s and do not cross a dead-air gap.

#### Chunking

```python
WINDOW_S = 45.0
OVERLAP_S = 5.0
STEP_S = WINDOW_S - OVERLAP_S  # 40s
MIN_WORDS = 8

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
```

A word belongs to a chunk by its `start`, so a 5-second band is in two chunks when speech is continuous. Skipping chunks with fewer than 8 words is the dead-air saving. It does not change the contract for chunks that are sent. A 4-hour VOD of continuous speech is `240 * 60 / 40 = 360` chunks. Real VODs are lower because of silence. Score with a thread pool of 4 (`SCORE_CONCURRENCY`, constant in `scorer.py`) so 360 calls at ~1.5s each land near 2–3 minutes instead of ~9. The user's rate limit is the constraint; 429s go through the tenacity backoff. Do not raise concurrency above 4 in MVP.

#### Prompt and schema

System prompt, copied verbatim from `docs/ARCHITECTURE.md` §3.3 (v1 asset; do not rewrite it in this phase):

> You are a short-form content strategist specializing in GTA 6 streaming content. Identify moments with viral clip potential: insane stunts, heist fails, rage moments, funny RP interactions, big reactions, unexpected chaos. Avoid: walking/traveling, menus, dead air, generic commentary. For each candidate return: chunk_word_start, chunk_word_end (indices within the provided chunk), score 0-100, title (max 60 chars, clickbait-y but accurate), hook (one sentence on why it works). Respond with JSON matching the provided schema. If nothing is clip-worthy, return an empty list.

User message, no timestamps and no key. The example object is what makes "return an empty list" land in the schema without editing the system prompt:

```text
Words are numbered from 0. chunk_word_start and chunk_word_end are inclusive indices into this list only. Do not return seconds.
Respond with a JSON object of this shape, and no other keys: {"highlights":[]}
An empty highlights array means nothing in this chunk is clip-worthy.

[0] hello
[1] chat
[2] we
```

JSON schema in the same user message, because many compatible servers ignore `response_format`:

```python
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
```

`extra="forbid"` is what makes a payload of `{start_seconds, end_seconds}` fail validation instead of being silently ignored. There is no `max_length` on `title` or `hook`. In Pydantic v2, `max_length` runs before `mode="after"` validators, so truncation has to happen in `mode="before"` or the 61st character fails the chunk and burns the JSON retry. A float score is rounded and then clamped into 0–100 in that same `before` validator (`99.6` becomes `100`, `100.6` becomes `100`). Missing fields still fail. An empty string after stripping still fails `min_length=1`.

Call:

```python
client.chat.completions.create(
    model=model,
    temperature=0.2,
    max_tokens=800,
    response_format={"type": "json_object"},
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ],
)
```

If the provider returns a 400 that mentions `response_format`, retry that attempt once without the parameter. That retry does not consume the JSON-corrective retry.

Parse `choice.message.content`. Strip a single ```json fence if present. `json.loads` it. If the value is a list, wrap it as `{"highlights": value}` before validation. That is how a model that obeys the system line and returns `[]` becomes zero windows instead of a heuristic chunk. Then validate with `HighlightResponse`. On failure, call once more with a second user message whose last line is exactly `your last reply was not valid JSON`. Do not attach more than 500 characters of the bad reply. On the second failure, or after transport retries are exhausted, score **that chunk** with the heuristic. Other chunks are unaffected.

A validated empty highlight list, either `[]` or `{"highlights":[]}`, is a real answer. Do not run the heuristic for that chunk. `response_format: json_object` does not by itself fix a model that returns a bare list; the wrap does.

Index check happens after pydantic. A candidate whose indices fall outside the chunk is dropped. Its siblings are kept. If every candidate in a validated payload is out of range, the chunk contributes nothing (it is not a JSON failure).

Map local index `k` through `chunk[k]` to the global word list. `start = words[g0].start`, `end = words[g1].end`. Never ask the model for either number.

Worked example, locked as a unit test. `MIN_WORDS` stays 8. Do not patch it for this fixture.

| global | start | word |
|---|---|---|
| 0–5 | 0.0, 1.0, 2.0, 3.0, 10.0, 20.0 | `a` `b` `c` `d` `e` `f` |
| 6 | 40.0 | `g` |
| 7 | 44.0 | `h` |
| 8–13 | 46.0, 50.0, 55.0, 60.0, 70.0, 80.0 | `i` `j` `k` `l` `m` `n` |

- Chunk window `[0, 45)` is global indexes 0 through 7.
- Chunk window `[40, 85)` is global indexes 6 through 13.
- A candidate `chunk_word_start=1` on the second chunk maps to global index 7 (`h` at 44.0), not to global word 1.

#### Merge, pad, clamp

```python
@dataclass(frozen=True)
class ScoredWindow:
    start_s: float
    end_s: float
    score: int
    title: str
    hook: str
    score_source: Literal["llm", "heuristic"]
    word_start: int  # global index, inclusive, for captions
    word_end: int
```

Pad and clamp first, then suppress. Doing suppression on the unpadded intervals keeps `[0, 10]` and `[10, 20]` (they only touch) and the ±8s pad then turns them into overlapping clips of the same moment.

1. Map indexes to the raw word range.
2. `extend_window` (below). Drop `None` and anything shorter than 2 seconds.
3. Sort by `score` descending, then padded `start_s` ascending, then `title` ascending.
4. Keep a window when it does not overlap an already kept window. Overlap means `intersection / min(len_a, len_b) >= 0.5` on the **padded** intervals. Touching edges (`intersection == 0`) are not an overlap.
5. Stop at `settings.max_clips_per_job` (default 8).

The model's index range is preserved even if a short pause sits inside it. Only the ±8s pad is forbidden from crossing dead air. Locked test: unpadded `[0, 10]` and `[10, 20]`, duration long, no dead air, scores 90 and 80. After pad they are `[0, 18]` and `[2, 28]`. Intersection 16 over the shorter length 18 is at least 0.5, so only the score-90 window remains.

```python
PAD_S = 8.0
DEAD_AIR_S = 1.5

def extend_window(words: list[Word], i0: int, i1: int, duration_s: float) -> tuple[float, float] | None:
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
    if end - start < 2.0:
        return None
    return start, end
```

Example locked as a test: words end at 50.0 and the next word starts at 70.0. A candidate on the later word with an 8s left pad would begin at 62, inside the gap. The clamp moves `start` to 70.0. A 1.0s gap is not dead air and the pad may cross it.

Drop windows the clamp shrinks below 2 seconds. If nothing remains, the pipeline still completes with zero clips and `No clip-worthy moments in this VOD.`

#### Heuristic

Used only when a chunk's LLM call fails, not when the model returns a valid empty list. The failed chunk's index list is the segment. Deterministic, no RNG.

`docs/ARCHITECTURE.md` §3.3 names "keyword density + caps-ratio + short-pause-bounded segments." MVP implements keyword density and caps on that already-bounded chunk. It does **not** cut a second pause-bounded span inside the chunk. The chunker already skipped windows with fewer than 8 words. A later change can add pause spans; this phase must not pretend it did.

Tokenize on whitespace. Casefold. Strip leading and trailing punctuation from each token. Match **whole tokens only**. `chat` does not match inside `chatting`. Multi-word phrases match consecutive tokens.

Strong tokens, at least one required or the chunk scores 0 and is dropped: `crash`, `wreck`, `heist`, `fail`, `failed`, `police`, `busted`, `wanted`, `shot`, `shoot`, `dead`, `died`, `explode`, `explosion`, `glitch`, `bug`, `insane`, `crazy`, `rage`, `scream`.

Weak tokens add points only after a strong hit: `omg`, `bro`, `dude`, `chat`, `wait`, `run`, `money`, `stars`, plus the phrase pairs `no way`, `oh my god`, `let's go`, `lets go`, `clip that`. `chat` plus `wait` with no strong token is not a clip.

```text
score = 0 if strong_hits == 0 else min(100, (strong_hits + weak_hits) * 12 + caps_words * 8 + exclaim_words * 6)
```

`caps_words`: tokens with length > 2 and `token.isupper()` before casefold. `exclaim_words`: tokens containing `!`. Keep the chunk only if `score >= 24` (which already implies a strong hit). Title: the six tokens centered on the first strong hit, collapsed whitespace, truncated to 60 characters. Hook is exactly `Heuristic pick (keyword density). LLM scoring was unavailable.` `score_source="heuristic"`.

After scoring, write `windows.json` (the `ScoredWindow` list, including a valid empty list) and commit before the phase-3 stub. No API key and no raw completion go in that file. The resume rule above skips scoring when the file validates, so a clipping crash does not re-bill the key. Phase 4 deletes the constant.

```python
# PHASE 3 ONLY. Deleted in phase 4 when clipping is real.
STUB_AFTER_SCORE = True

if STUB_AFTER_SCORE:
    raise ClipForgeError(
        "internal",
        "Scoring finished. Later stages are not included in this build yet.",
    )
```

A job can mix sources. Each clip stores its own `score_source`. Default for LLM successes is `llm`. If the stored key decrypts but every chunk falls back, the job still completes. Progress message on a mixed or fallback job stays the normal ready line; the badge on each clip tells the truth. Decrypt failure is different: raise `InvalidStoredKey` with `The saved API key could not be decrypted. Enter it again.` That fails the job, because there is no key to retry with and the heuristic would hide a broken Fernet configuration.

Decrypt happens at the start of scoring, in a local variable, wrapped by `push_secret` / `secret_scope` and cleared in `finally: del token`. For OpenAI-compatible providers (`openai`, `groq`, `together`, `ollama`, `compatible`), `scorer.py` calls `build_openai_client()` in `backend/app/services/providers/client.py`, which runs `validate_outbound_url` / `check_outbound_url` before constructing:

```python
http_client = httpx.Client(follow_redirects=False, timeout=httpx.Timeout(30.0, connect=5.0))
client = OpenAI(
    api_key=token,
    base_url=base_url,
    http_client=http_client,
    max_retries=0,
)
```

For `provider="antigravity"`, `scorer.py` builds an `AntigravityProvider` (`CompletionProvider` protocol) via `build_antigravity_provider()`, using the stored credential (`"local"` sentinel for local CLI/bridge/OAuth, or an explicit OAuth token/JSON bundle). Transient Antigravity errors fall back to the heuristic per chunk just like OpenAI-compatible transport failures.

`base_url` and `model` come from the `api_keys` row (resolved presets in [API keys](#api-keys)). If the user deletes the row between download and scoring, fail the job with `Add an API key before generating clips.` Do not crash and do not switch to `OPENAI_API_KEY`.

Log `score_chunk` with `job_id`, `index`, `total`, `source` (`llm` or `heuristic`). Do not log the prompt, the transcript, or the completion text.

#### Phase 3 tests

- Chunk grid: words at 0, 39, 44, 46. With `MIN_WORDS` patched to 1, the word at 44 is in two chunks and the word at 46 is only in the second. Production `MIN_WORDS` stays 8; the pad fixture has enough words.
- System prompt file constant equals the architecture paragraph (snapshot equality).
- User prompt for a fixture contains `[0]` and does not contain a timestamp pattern `\\d+\\.\\d+`.
- Payload `{highlights: [{start_seconds: 1, end_seconds: 2, ...}]}` fails pydantic.
- A title longer than 60 characters and a score of `99.6` validate, the stored title is 60 characters, the score is 100, and `score_source` stays `llm` (no retry, no heuristic).
- First reply `not json`, second reply valid → one retry, `score_source=llm`, the corrective sentence was in the second call only.
- Two bad replies → heuristic, `score_source=heuristic`, hook is the fixed sentence.
- Both `[]` and `{"highlights":[]}` produce zero windows and zero heuristic calls.
- `chat` + `wait` with no strong token does not produce a heuristic window. A chunk whose tokens include `crash` does.
- Padded overlap: unpadded `[0, 10]` score 90 and `[10, 20]` score 80 become one kept window, the 90. A 10s window score 40 that shares 80% of the shorter padded length with a score-90 window is dropped.
- Ninth window is dropped when `max_clips_per_job` is 8.
- Dead-air clamp example above, and a sub-1.5s gap that the pad is allowed to cross.
- Index mapping worked example.
- Canary key is the client `api_key` and also injected into a raised `APIStatusError`. Logs and `jobs.error` do not contain it. Task kwargs are empty.
- Pipeline test: transcribe stub removed, `windows.json` validates, and the job is `failed` with `Scoring finished. Later stages are not included in this build yet.`

---

### Phase 4 — Clipping and captions

**Exit check:** each surviving window becomes one clip row. The ffmpeg crop is a centered 9:16 scaled to 1080×1920 with even dimensions. Audio is loudness-normalized. ASS is karaoke, 1080×1920, fontsize 68, outline 4, bottom-center, `MarginV` 420. One raised ffmpeg does not stop the next clip. `caption_status` is `burned` or `failed` with the clip.

#### Per-clip isolation

`render_clip()` in `backend/app/services/video.py` raises on failure. `run_pipeline` catches around each window:

```python
for index, window in enumerate(windows, start=1):
    set_progress(job_id, "clipping", f"Cutting clip {index} of {len(windows)}")
    try:
        path = render_clip(vod, window, words, work_dir, out_path)
        insert_clip(..., status="ready", caption_status="burned", file_path=rel, error=None)
    except Exception as exc:
        log.exception("clip_failed", job_id=job_id, clip_index=index)
        insert_clip(..., status="failed", caption_status="failed",
                    file_path=None, error=clip_error_message(exc))
```

`clip_error_message` maps timeouts to `This clip took too long to export.` and everything else to `This clip could not be exported.` It does not include ffmpeg stderr in the user string (paths and filter graphs are noise). The log gets stderr truncated to 500 characters after `redact()`.

Insert the row only after success or caught failure, so a SIGKILL mid-ffmpeg leaves no `ready` row. `clips.status` default in the schema remains `ready` as in the architecture doc; the failure path sets `failed` explicitly. `caption_status` default is `pending` so a partial insert cannot look burned. The pipeline never inserts `pending`.

If zero windows: do not enter a long clipping loop; transition `completed` with the empty-state progress string. If at least one window and every clip fails: still `completed`, progress `Finished, but every clip failed to export.`

#### ffmpeg

Probe once per job. Do not scrape ffprobe's default writer:

```text
ffprobe -v error -print_format json -show_entries stream=width,height -select_streams v:0 vod.mp4
```

Read `streams[0].width` and `streams[0].height` from that JSON. Crop size is the nearest even width of `crop_h * 9 / 16`, and the height is floored to even when the source height is odd. Floor-to-even of `int(1080 * 9 / 16)` is 606 and is the wrong rule. 1080 × 9 / 16 is 607.5, and the nearest even is 608.

```python
def even_floor(value: int) -> int:
    return value if value % 2 == 0 else value - 1

crop_h = even_floor(height)
nearest_even = ((int(crop_h * 9 / 16) + 1) // 2) * 2
crop_w = min(even_floor(width), nearest_even)
if crop_w < 2 or crop_h < 2:
    raise ClipExportError("bad dimensions")
```

Even dimensions are required for `libx264` yuv420. ffmpeg's default crop x/y is centered, so no `x`/`y` expression is passed. The pass-1 filter is `crop={crop_w}:{crop_h}`, not the raw height.

Pass 1, timeouts 600s, from `docs/ARCHITECTURE.md` §3.4 plus the loudness requirement in `AGENTS.md` phase 4. `-ss` stays before `-i` as specified. Clamp the slack so the arguments are never negative and never past `duration_s`:

```python
ff_ss = max(0.0, window.start_s - 0.5)
ff_to = min(duration_s, window.end_s + 0.5)
```

```text
ffmpeg -hide_banner -nostdin -loglevel error -y
  -ss {ff_ss} -to {ff_to} -i vod.mp4
  -vf crop={crop_w}:{crop_h},scale=1080:1920:flags=lanczos
  -af loudnorm=I=-14:TP=-1.5:LRA=11
  -c:v libx264 -crf 20 -preset fast -threads 2
  -c:a aac -b:a 160k -movflags +faststart
  work/{clip_id}_raw.mp4
```

`loudnorm` is single-pass. Two-pass loudnorm would double encode time and is not required for MVP. Target −14 LUFS matches the usual Shorts/TikTok ballpark.

If the source is already narrower than `ih*9/16` (portrait input), `crop_w` becomes the full width via the `min()`. The scale step then stretches to 1080×1920. That is acceptable for MVP center-crop; face-aware reframing is a non-goal. Do not add a second filter graph.

Pass 2, cwd set to `work/` so the filter path is relative and colon-escaping is not required:

```text
ffmpeg -hide_banner -nostdin -loglevel error -y
  -i {clip_id}_raw.mp4
  -vf ass={clip_id}.ass
  -c:a copy -c:v libx264 -crf 20 -preset fast -threads 2 -movflags +faststart
  ../clips/{clip_id}.mp4
```

`-c:a copy` matches the blueprint's burn step. Delete the raw and the ASS in a `finally` inside `render_clip` after a successful burn. On failure, delete all three so a retry does not upload a partial. `cleanup_raw` is the backstop.

`subprocess.run([...], check=True, capture_output=True, timeout=..., cwd=..., start_new_session=False)`. Never `shell=True`. Never set `start_new_session=True` (Key Decision 13). The same `start_new_session=False` applies to yt-dlp and to the wav ffmpeg calls.

#### ASS

`PlayResX: 1080`, `PlayResY: 1920`, `WrapStyle: 0`. Style name `Karaoke`. `Fontname: Liberation Sans` (see Key Decision 12). `Fontsize: 68`. `Bold: -1`. `PrimaryColour` yellow `&H0000FFFF` (the karaoke fill). `SecondaryColour` white `&H00FFFFFF` (not yet sung). `OutlineColour` `&H00000000`. `Outline: 4`. `Shadow: 0`. `Alignment: 2` (bottom-center). `MarginL`/`MarginR` 80. `MarginV: 420` so the line clears typical TikTok UI. `BorderStyle: 1`.

libass `\k` fills from secondary to primary and keeps the filled syllable in primary. That is the "current word yellow, the rest of the cue white, spoken words stay yellow" behavior. Durations are centiseconds: `max(1, round((word.end - word.start) * 100))`.

Cue grouping, max 2 lines, ~28 characters per line, at most 8 words:

- Walk the words whose global indices lie in `[word_start, word_end]` and whose timestamps overlap `[window.start_s, window.end_s]`.
- Greedy-pack words. A wrap helper breaks on spaces at 28 characters. If adding the next word would make a third line or the 9th word, flush the cue.
- Cue event start is `max(0, first.start - ff_ss)`, end is `max(start, last.end - ff_ss)`. **Zero on the ASS clock is `ff_ss`, the `-ss` of pass 1, not the VOD start and not `window.start_s`.** Getting this wrong shifts every caption by minutes. A unit test feeds a word at VOD t=100 with `ff_ss=90` and expects a dialogue start of `0:00:10.00`.
- Inside the cue, prefix each word with `{\k<cs>}`. Join words with spaces. Insert `\N` at the wrap point, never more than one `\N`.
- Escape ASS overrides in the spoken text: replace `{` and `}` with lookalikes or strip them. Do not pass raw transcript braces into the event.

`caption_status=burned` only after pass 2 exits 0 and the output file size is greater than 0. If pass 1 succeeds and pass 2 fails, the clip row is `failed` / `caption_status=failed`. Do not publish the uncaptioned raw. The product requires burned captions.

#### Clip row

| Column | Value |
|---|---|
| `start_s`, `end_s` | window times on the VOD clock, not `ff_ss` |
| `score`, `title`, `hook`, `score_source` | from the window |
| `file_path` | relative path or NULL |
| `status` | `ready` or `failed` |
| `caption_status` | `burned` or `failed` |
| `error` | NULL or the short user string |

`error` is an additive column. The architecture sketch has no per-clip message, but golden rule 4 requires one the UI can show. It is not a second status machine.

#### Phase 4 tests

- ASS snapshot: `PlayResX: 1080`, `PlayResY: 1920`, `Fontsize,68`, `MarginV,420`, `Outline,4`, a `\k` tag, PrimaryColour yellow, and no `{` from a fixture word `hel{lo`.
- ASS timebase test described above.
- Crop math: 1920×1080 → `crop_w=608`, `crop_h=1080`, and the pass-1 argv contains the substring `crop=608:1080`. 1080×1920 portrait → `crop_w=1080`, `crop_h=1920`. Odd height 1920×1081 floors to `crop_h=1080` and the same 608 width. The formula is nearest-even, `((int(crop_h * 9 / 16) + 1) // 2) * 2`, not floor-to-even 606.
- Argv test: pass 1 contains `loudnorm=I=-14:TP=-1.5:LRA=11`, `scale=1080:1920`, `crop=608:1080` for the 1920×1080 fixture, `-crf`, `20`, and `-ss` before `-i`. Pass 2 contains `ass=` and `copy` for audio. `shell` is false. `start_new_session` is false.
- Two windows, first `subprocess.run` raises, second returns 0. Two rows: one `failed`, one `ready`. Job status `completed`. `score_source` preserved on both.
- Output path escapes `STORAGE_DIR` if `clip_id` were `../` — the UUID type makes this a `resolve_under_storage` unit test rather than a real id.

---

### Phase 5 — Dashboard

**Exit check:** email and password sign-in, one key saved without ever coming back to the browser, jobs list polling at 2.5s only while a status is active, job detail shows score, title copy, player, download, and the actual `jobs.error` / clip `error`.

#### Auth

FastAPI stores users. Auth.js does not.

`POST /api/v1/auth/register` `{email, password}` → `201 {id, email}`.

`POST /api/v1/auth/login` `{email, password}` → `200 {id, email}`.

`GET /api/v1/auth/config` → `200 {"guest_enabled": bool}` (reflects `ALLOW_GUEST_AUTH`, default `false`; `.env.example` enables `true` for local-first clones).

`POST /api/v1/auth/guest` → `200 {id, email}` when `ALLOW_GUEST_AUTH=true` (returns `403 Guest access is disabled.` otherwise). Creates or loads the local workspace user `local@clipforge.local` with an unusable password hash (`"!"`) so password login never accepts it. Also enforces the Redis IP rate limit below.

These auth routes are the only authenticated-API exceptions besides `GET /api/v1/health`. They do not set cookies and do not return a token. `AGENTS.md` §8 said every non-health route requires a bearer token; issuing that token requires auth routes. The token is still minted only in Auth.js.

Password: 10 to 128 characters. Store `scrypt$131072$8$1${salt_b64}${hash_b64}` in `users.password_hash`. `hashlib.scrypt(..., n=2**17, r=8, p=1, dklen=32)`, 16-byte salt. `n=16384` is below the OWASP interactive choice and is not used. Verify with `hmac.compare_digest` on the derived key. Login of an unknown email (or `local@clipforge.local` with unusable hash) still runs scrypt against a fixed dummy hash so the missing-user path is not obviously faster. Login failure, registration of the reserved guest email, and duplicate registration all return `400` with `Invalid email or password.` A short password on register is a different `400` (`Password must be at least 10 characters.`) because that is not an existence check. Email is trimmed and lowercased, must contain one `@`, no spaces, length 3–254. No verification mail.

Rate limit login, register, and guest login with Redis: `INCR` key `rl:auth:{ip}` with 60s TTL, 10 attempts, then `429 Too many sign-in attempts. Wait a minute.` Client IP:

- `TRUST_PROXY_COUNT` defaults to `0`. Use the socket peer and ignore `X-Forwarded-For`.
- When the count is `N > 0`, split the header on commas, strip whitespace, and take index `-N` (from the right). Caddy appends the peer it saw, so with `TRUST_PROXY_COUNT=1` the rightmost hop is that peer. The leftmost hop is the value the client can forge (`forged, real` must limit `real`).
- If the header has fewer than `N` hops, use the socket peer.
- Production sets `TRUST_PROXY_COUNT=1` and publishes the API on `127.0.0.1:8000` only, so the socket peer of the API is Caddy. Dev compose keeps `8000:8000` so the host Next.js process can reach it, and leaves `TRUST_PROXY_COUNT=0`.

Do not log the password. The redaction processor drops fields named `password`.

JWT minted in `frontend/auth.ts` (building on Edge-safe `frontend/auth.config.ts`). The callback must assign `token.accessToken` and `token.accessTokenExpiresAt` and return `token` (returning `null` once `isTokenExpired(token)` is true). A local variable that is never stored is not a session.

```typescript
callbacks: {
  ...authConfig.callbacks,
  async jwt({ token, user }) {
    if (user) {
      const now = Math.floor(Date.now() / 1000);
      token.sub = user.id;
      token.accessTokenExpiresAt = now + SESSION_MAX_AGE_S;
      token.accessToken = await new SignJWT({ sub: user.id })
        .setProtectedHeader({ alg: "HS256" })
        .setIssuer("clipforge")
        .setAudience("clipforge-api")
        .setIssuedAt(now)
        .setExpirationTime(now + SESSION_MAX_AGE_S)
        .sign(new TextEncoder().encode(secret));
      return token;
    }
    if (isTokenExpired(token)) {
      return null;
    }
    return token;
  },
},
```

Claims are only `sub`, `iss`, `aud`, `iat`, `exp`. No email, no role, no key. 24 hours, no refresh (Key Decision 14). Session strategy `jwt`. The media proxy reads the token server-side via `auth()` and does not need the client copy. TanStack Query does, which is why `session.accessToken` exists. The HMAC key is the UTF-8 bytes of the secret string. `start.sh` stores `secrets.token_hex(32)` as characters. Do not hex-decode it. `jose` and Python must both use those characters as bytes.

FastAPI `decode_jwt` in `backend/app/security.py`, in this order. Any failure is 401 `Sign in required.` Never 500.

1. Require a `str`. Split on `.` into exactly three non-empty segments: `h_b64`, `p_b64`, `s_b64`.
2. Base64url-decode the header and the payload with padding restored: `segment += "=" * (-len(segment) % 4)`, then `base64.urlsafe_b64decode`. Reject if either decode raises.
3. Header must be a JSON object. `alg` must be exactly the string `HS256`. Reject missing `alg`, reject `none`, reject any other algorithm. Reject the token if the key `crit` is present.
4. Base64url-decode `s_b64` the same way. The result must be exactly 32 bytes. Any other length is 401.
5. Key bytes are `settings.jwt_secret.encode("utf-8")`. MAC input is the original ASCII segments joined by a dot: `f"{h_b64}.{p_b64}".encode("ascii")`. `mac = hmac.new(key, signing_input, hashlib.sha256).digest()`.
6. `hmac.compare_digest(mac, signature)`. Compare the raw digests, not the encoded segment. If the lengths differ, `compare_digest` raises `TypeError`; catch it and return 401. Do not use `==`.
7. Payload must be a JSON object. `iss` must equal the string `clipforge`. `aud` must equal the string `clipforge-api`. An array `aud` is rejected. `sub` must be a UUID string. `iat` and `exp` must be numbers.
8. Let `now` be UTC unix seconds. Accept when `exp >= now - 30` (a token may be up to 30 seconds past expiry; the skew is on the past side of `exp` only) and `iat <= now + 30`. Require `exp - iat <= 26 * 60 * 60` so a 7-day token cannot be smuggled in after the 24-hour decision.
9. Load `User` by `sub`. Unknown user is the same 401 as a bad signature.
10. Authorization uses `sub` only.

PR 2 commits `backend/tests/fixtures/jwt_jose.json` produced by the same `SignJWT` call, with a fixed `iat` of `1700000000`, `exp` of `iat + 86400`, `sub` `00000000-0000-4000-8000-000000000001`, and a 32+ character secret recorded next to the compact token. The Python test freezes time at `iat + 60` and accepts that token. The same file's tests cover `alg=none` (a hand-built token) and the jose token signed with the wrong secret. If the test runner cannot import `jose`, the committed compact token is the vector; do not re-sign it in Python and call that a jose test.

`get_current_user` reads `Authorization: Bearer`. Missing header is 401.

Frontend files:

- `frontend/auth.ts` — Auth.js v5 config (`next-auth` 5.x, not v4) with `credentials` and `guest` providers; server fetches use `API_URL ?? NEXT_PUBLIC_API_URL`.
- `frontend/auth.config.ts` — Edge-safe Auth.js config for middleware and expiry checks.
- `frontend/app/api/auth/[...nextauth]/route.ts` — export `handlers`.
- `frontend/app/api/media/clips/[id]/route.ts` — streaming proxy.
- `frontend/app/login/page.tsx`, `frontend/app/login/login-form.tsx`, `frontend/app/register/page.tsx`, `frontend/app/register/register-form.tsx`, `frontend/app/skip-signin-button.tsx`.
- `frontend/app/dashboard/page.tsx`, `frontend/app/jobs/[id]/page.tsx`.
- `frontend/app/providers.tsx` — `SessionProvider` and `QueryClientProvider`.
- `frontend/lib/auth-mode.ts`, `frontend/lib/api.ts`, `frontend/lib/jobs.ts`.
- `frontend/middleware.ts` — unauthenticated visits to `/dashboard` and `/jobs` redirect to `/login`.

`authorize()` POSTs to `/api/v1/auth/login` (or `/api/v1/auth/guest` for the `guest` provider) and returns `{id, email}` or `null`. Register page POSTs to `/api/v1/auth/register`, then `signIn("credentials", ...)`.

Landing page (`frontend/app/page.tsx`) keeps its layout. Header gains a link: `Sign in` or `Dashboard` from the session, plus `Skip sign-in` when `!session && guestEnabled`. Footer line `Dashboard and sign-in land with the job pipeline.` is replaced with a link to `/dashboard`. Do not restyle the page.

`JWT_SECRET` on Vercel must equal `JWT_SECRET` on the VPS. It is a server env var, never `NEXT_PUBLIC_`. `AUTH_SECRET` is a different value, generated the same way, used only by Auth.js to encrypt its session cookie. `AUTH_URL` is the Vercel origin in production and `http://localhost:3000` in dev.

#### API keys & Provider Catalog

`POST /api/v1/keys`

```json
{ "provider": "openai", "api_key": "sk-...", "base_url": null, "model": null }
```

`provider` is one of `openai`, `groq`, `together`, `ollama`, `compatible`, `antigravity`.

| provider | default base_url | default model |
|---|---|---|
| `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` |
| `groq` | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
| `together` | `https://api.together.xyz/v1` | `meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo` |
| `ollama` | `http://host.docker.internal:11434/v1` | `llama3.1` |
| `compatible` | required | required |
| `antigravity` | `https://daily-cloudcode-pa.googleapis.com` | `gemini-4-hydrogen` (or first live `gemini-*` model when connected) |

The OpenAI-compatible preset table lives in `backend/app/services/providers/presets.py` as data, not as scattered strings, while the `antigravity` non-OpenAI adapter lives in `backend/app/services/providers/antigravity/` behind `CompletionProvider` (`base.py`). `model` on the request overrides the default model (`antigravity` auto-picks an accepted live model if `model` is omitted while connected). `base_url` overrides the preset only for `ollama` and `compatible`. `openai`, `groq`, `together`, and `antigravity` are locked to their origin; a client-supplied `base_url` for those four is 400 `That provider uses a fixed base URL.` `compatible` without both `base_url` and `model` is 400 `Provider "compatible" needs a base URL and a model.`

`api_key` is `SecretStr | None` (`max_length=4096`). For all OpenAI-compatible providers, an empty `api_key` returns `400 API key cannot be empty.` For `antigravity`, omitting `api_key` (or sending `"local"`, `"cli"`, `"oauth"`, `"antigravity-local"`) stores `"local"` and uses local `agy` CLI (`ANTIGRAVITY_CLI_BIN`), the host bridge (`scripts/antigravity_bridge.py` via `ANTIGRAVITY_BRIDGE_URL`, auto-started by `start.sh`), or local OAuth credentials (`ANTIGRAVITY_OAUTH_CREDS_PATH` / `~/.gemini/oauth_creds.json`); callers may also supply an OAuth access token or OAuth JSON bundle directly.

`check_outbound_url` / `validate_outbound_url` (`backend/app/services/providers/url_policy.py`) runs in the API process before the validation call and again in the worker before scoring:

- Reject userinfo, query strings, and fragments.
- `ollama` (`OLLAMA_LOCAL_HTTP_POLICY`) may use `http` only, host exactly `host.docker.internal`, `localhost`, or `127.0.0.1`, optional port, path empty or ending in `/v1`.
- `antigravity` bridge URLs (`ANTIGRAVITY_BRIDGE_HTTP_POLICY`) may use `http` strictly for `host.docker.internal`, `localhost`, or `127.0.0.1` at the root path (non-local bridge URLs must use `https` under `DEFAULT_HTTPS_POLICY`).
- `compatible` (`DEFAULT_HTTPS_POLICY`) must be `https`, and the host must be a DNS name, not an IP literal. Resolve every A and AAAA record immediately before the call. Reject the URL if any address is loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, `fe80::/10`, which includes `169.254.169.254`), private (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`), `0.0.0.0/8`, or multicast. Also reject the names `metadata.google.internal` and `metadata.google.com` without resolving them. If resolution fails, reject.
- `https://169.254.169.254/` and `https://10.0.0.5/` fail this check. They are not saved by being `https`.

Every OpenAI SDK client, including Whisper and the validation call, is built via `build_openai_client()` on `httpx.Client(follow_redirects=False)`. A 301 or 302 is a failure. Do not open a second connection to the `Location`. The key is sent only as the `Authorization` header by the SDK, never interpolated into the URL. Residual DNS rebinding between the check and the connect is accepted for MVP only because redirects are off and the check is immediately before the call; do not "fix" it by turning redirects on.

Validation (allowed on the HTTP path because it is one short RPC, not a VOD):
- OpenAI-compatible providers: `POST {base}/chat/completions` with `max_tokens=1`, timeout 8s, a single user message `ping`. On non-2xx or on a redirect, `400 That key was rejected by the provider.`
- `antigravity`: `AntigravityProvider.validate_credentials()` verifies the CLI, host bridge, or OAuth session with the same 8s HTTP timeout (15s CLI timeout).
- Do not copy upstream bodies into the response or log. On success, encrypt and upsert. Wrap the plaintext in `secret_scope()` (`push_secret`) for the rest of the request so a later exception cannot log it.

Upsert: `INSERT ... ON CONFLICT (user_id) DO UPDATE` in one transaction. Unique constraint on `api_keys.user_id` prevents multiple rows. Response `200 {"configured": true, "provider": "openai", "model": "gpt-4o-mini"}`. The response has no `api_key` field. The request handler must not bind the key into a structlog call.

`GET /api/v1/keys` → `{"configured": false}` or `{"configured": true, "provider", "model", "base_url"}` with userinfo stripped. Not in the original route table; the onboarding screen cannot function without a metadata read. It does not return ciphertext.

`DELETE /api/v1/keys` → 204. Idempotent if no row.

Provider catalog & model discovery (`backend/app/api/providers.py`, `backend/app/services/providers/catalog.py`), all bearer-authenticated:

- `GET /api/v1/providers` → `{"providers": [...]}` returning metadata, defaults, `requires_api_key`, `locked_base_url`, `requires_custom_config`, `connected`, `model_source`, and `models` for all 6 providers (live-probing `ollama` via `/api/tags` and `antigravity` via CLI/bridge/OAuth, falling back to curated static lists otherwise).
- `GET /api/v1/providers/{provider}/models` → `{"provider", "connected", "model_source", "default_model", "models"}`.
- `POST /api/v1/providers/antigravity/models` `{api_key?}` → probes live `antigravity` models for a supplied OAuth token/JSON (or `"local"`) inside `secret_scope()` without saving it.

Fernet: `Fernet(settings.encryption_key.encode())`. Store the token bytes in `encrypted_key`. Decrypt only in the worker. A bad token raises `InvalidStoredKey`.

Startup: API lifespan and the Celery `worker_process_init` signal call `validate_runtime_secrets()`. Empty `ENCRYPTION_KEY`, a key Fernet rejects, or `JWT_SECRET` shorter than 32 characters raises. The process should not serve. This is a behavior change from Phase 0, which allowed blanks. `tests/conftest.py` sets a valid Fernet key and a 32+ character JWT secret at import time, before `app.main` is imported, so `backend/tests/test_health.py` keeps passing. Do not skip the check when `TESTING=1`.

#### Jobs and clips HTTP

All routes depend on `get_current_user`. Queries filter `user_id`. Another user's id is **404** `Not found.`, not 403.

`POST /api/v1/jobs` `{url: str}` → `202 {"job_id": "<uuid>"}`. Behavior is the rejection split above. Body over 2 KB is 422.

`GET /api/v1/jobs` → list ordered by `created_at` descending, cap 50:

```json
[{ "id", "source_url", "status", "created_at", "progress_message", "error" }]
```

`progress_message` and `error` are additive to the §8 example. The list is what polls; without them the user only sees an enum. Do not include clips in the list.

`GET /api/v1/jobs/{id}`:

```json
{
  "id": "...",
  "source_url": "...",
  "platform": "youtube",
  "title": "...",
  "duration_s": 3600,
  "thumbnail_url": null,
  "status": "scoring",
  "progress_message": "Scoring highlights (12/80)",
  "error": null,
  "created_at": "...",
  "updated_at": "...",
  "clips": [
    {
      "id": "...",
      "start_s": 12.3,
      "end_s": 41.0,
      "score": 91,
      "title": "...",
      "hook": "...",
      "score_source": "llm",
      "caption_status": "burned",
      "status": "ready",
      "error": null,
      "created_at": "..."
    }
  ]
}
```

Clips ordered by `score` descending, `start_s` ascending. **Never return `file_path`.** Title, duration, platform, and `thumbnail_url` are additive fields. The detail page renders `thumbnail_url` as an image above the status line when it is non-null, and renders nothing when it is null. Do not return the field if the UI does not show it; this page shows it.

`GET /api/v1/clips/{id}/video`:

- 404 if the clip is not the caller's, or `status != ready`, or the file is missing.
- Starlette `FileResponse` (FastAPI 0.115 / current Starlette honors `Range` and returns 206). If a test shows it does not, replace with a 20-line range reader; do not buffer the file.
- `media_type=video/mp4`, `X-Content-Type-Options: nosniff`, `Cache-Control: private, max-age=3600`.
- Without `download=1`: `Content-Disposition: inline; filename="clip.mp4"`.
- With `?download=1`: `attachment` and `filename="clipforge-{slug}.mp4"`. Slug is ASCII `[A-Za-z0-9_-]{1,60}` from the title, else `clip`. Strip CR, LF, and quotes so the header cannot be injected.

#### Dashboard UI

Match the landing tokens already in `frontend/app/globals.css` and `frontend/tailwind.config.ts`: `bg-bg`, `bg-raise`, `text-ink`, `text-muted`, `bg-acid`, `font-mono` for statuses, `rounded-[20px]`, `shadow-card`. No new color system and no component library.

Dashboard (`/dashboard`):

- Header: wordmark, existing `HealthPill`, sign out.
- If `GET /api/v1/keys` has `configured: false`, show a panel that blocks Generate: provider `<select>` (populated from `GET /api/v1/providers` so `openai`, `groq`, `together`, `ollama`, `compatible`, and `antigravity` plus discovered models/connection state are available), key `<input type="password" autoComplete="off">` (optional when `requires_api_key === false` for `antigravity`), optional base URL and model selector/input, submit `Save key`. After save, clear the input state, show provider and model, and offer `Replace` and `Delete`. Never put the key in the query string, localStorage, or a toast.
- Generate form: URL input, button `Generate`, disabled while the mutation is in flight. Helper text: max 240 minutes, up to 8 clips, and the architecture hint that Twitch VODs expire after 14 days (60 for Partners).
- Job list: status pill, truncated URL, relative time, `progress_message`, and if `error` is set an alert with that exact string. Each row links to `/jobs/{id}`.
- TanStack Query key `["jobs"]`. `refetchInterval` is `listRefetchInterval` of the row statuses. Also `refetchOnWindowFocus: true`. `enabled` is `Boolean(session.accessToken)`. Do not fire the request with an empty bearer while the session is still resolving, or the client will stick on 401.

`frontend/lib/jobs.ts`:

```typescript
export const ACTIVE_STATUSES = new Set([
  "queued",
  "downloading",
  "transcribing",
  "scoring",
  "clipping",
]);

export function listRefetchInterval(statuses: readonly string[]): number | false {
  return statuses.some((status) => ACTIVE_STATUSES.has(status)) ? 2500 : false;
}
```

Detail (`/jobs/[id]`):

- Query key `["jobs", id]`. `refetchInterval` is `listRefetchInterval([job.status])` once `job` is loaded, and `false` while it is not. The same helper covers the list and the detail page. Leaving the list must not freeze the player on the first response. `enabled` is `Boolean(session.accessToken)` here too.
- If `thumbnail_url` is set, show it above the status line (`<img alt="">`).
- Status line is `progress_message`.
- If `error` is non-null, a `role="alert"` box contains that string and nothing else. This includes a `failed` job that still has ready clips: show the banner **and** the grid.
- Clip grid, responsive, no editor. Card: score as an acid pill (the landing sample uses this treatment), title, `Copy title` button (`navigator.clipboard.writeText`), hook, `score_source` in mono (`llm` or `heuristic`), native `<video controls playsInline preload="metadata">`.
- Video `src` is `/api/media/clips/{id}` (same origin). Download is `/api/media/clips/{id}?download=1`.
- A `failed` clip has no `<video>`. It shows `error` or `This clip could not be exported.`
- Completed with zero clips shows `progress_message` only.

The media route forwards status, `Content-Type`, `Content-Length`, `Content-Range`, `Accept-Ranges`, and `Content-Disposition`. It forwards the request `Range` header. It streams `upstream.body` and does not buffer. Non-UUID ids are 404 without calling the API. Because the frontend is Next.js 15, `GET` receives `context.params: Promise<{ id: string }>` and must `await context.params`.

#### Phase 5 tests

Backend, in pytest, against Postgres (see [Tests and fixtures](#tests-and-fixtures)):

- Register, login, wrong password, duplicate email (same `detail` as a wrong password), short password (different `detail`). A second session after register still sees the user (commit-on-success).
- JWT: the committed `jose` fixture verifies; `alg=none`; wrong secret; expired by more than 30 seconds; `aud` as an array; `exp - iat` of 7 days; unknown `sub`. Other user's job and clip are 404.
- Auth limit: `TRUST_PROXY_COUNT=1` and `X-Forwarded-For: forged, real` increments the bucket for `real`, not `forged`.
- Keys: ciphertext bytes are not the canary, do not contain `sk-` or `gsk_`, and `Fernet.decrypt` inside the test returns the canary. Fernet tokens are ASCII (`gAAAA…`) and are valid UTF-8; do not assert otherwise. `GET` body does not contain either canary. A 302 to `http://169.254.169.254/` during validation records one outbound request and returns 400. Second POST does not create a second row. DELETE then GET is `configured: false`. Two parallel job POSTs at the cap insert only one row.
- Video route: owner gets 200 and `Range: bytes=0-3` gets 206; other user gets 404; `download=1` sets `attachment`; a title containing `\"\r\n` cannot break the header.
- List omits `file_path`. Detail omits `file_path`.

Frontend, no new test framework. `frontend/lib/jobs.ts` and a small slug helper are plain TypeScript. `frontend/tsconfig.json` has `noEmit: true` and `module: esnext`, and `package.json` has no `"type": "module"`, so the test project must not inherit that emit. `tsconfig.test.json` sets `noEmit: false`, `module: CommonJS`, `moduleResolution: Node`, and `outDir: dist-test`. `npm test` is `tsc -p tsconfig.test.json && node --test dist-test`. PR 8 runs that command once. Cases: active status polls at 2500; `completed`, `failed`, and an empty list return `false`; a mixed list returns 2500; `listRefetchInterval([job.status])` is what the detail page uses, so a single active status returns 2500 and `"completed"` returns `false`. There is no GitHub Actions workflow in Phase 0. PR 8 adds the frontend job. Do not write "stays in CI" until that file exists. Do not add Vitest, Playwright, or React Testing Library in MVP.

Manual pass before calling phase 5 done: register, save a key, refresh and confirm the input is empty, submit a URL, watch the status change, open a failed job and read the error, copy a title, play a clip, download it.

---

### Phase 6 — Hardening

**Exit check:** a terminal job has no `vod.mp4`, no wav, no transcript, and still has clip files. A job running past 45 minutes becomes `failed` with the timeout sentence. A worker killed mid-job does not loop forever and does not leave a zombie ffmpeg. `scripts/smoke.sh` drives URL → terminal job. README matches what the binary actually does.

#### Cleanup

Call `cleanup_raw` from the task `finally` only when the row is terminal, as specified above. Also call it from the reaper when the reaper itself flips a row to `failed`.

Beat service in `docker-compose.yml`, same image and env, no published ports. Dev mounts `./storage` and the code tree, like the other backend services. The prod override mounts only `./storage` on api, worker, and beat. To prevent `reap_stale_jobs` from sitting behind long `process_job` runs on the concurrency-1 `worker`, `beat` runs embedded beat + a single-concurrency consumer on the dedicated `reaper` queue (`-s /tmp/celerybeat-schedule` avoids writing schedule files into the bind-mounted `./backend` directory), while `worker` consumes only `-Q celery`:

```text
celery -A app.workers.celery_app:celery_app worker -B -Q reaper --concurrency=1 --loglevel=INFO -s /tmp/celerybeat-schedule
```

Schedule `@celery_app.task(name="reap_stale_jobs")` every 60s on queue `reaper` (the explicit `name` matches `task_routes`). The worker's `SoftTimeLimitExceeded` handler is the timeout owner for a job it is running. The reaper must not delete `vod.mp4` under a live ffmpeg. `FOR UPDATE SKIP LOCKED` only covers the reaper's short transaction; the worker does not hold the row across ffmpeg. The Redis lock is the live claim.

`is_job_in_broker(redis_client, job_id)` returns `True` if `joblock:{job_id}` exists OR any Celery envelope in Redis hash `"unacked"` (`HVALS unacked`) or Redis list `"celery"` (`LRANGE celery 0 -1`) is a `process_job` task whose first argument equals `str(job_id)`. Before treating any `queued` job as lost, wait 1 second and re-check both `is_job_in_broker` and the DB row so a message mid-transition from `BRPOP` to `SET joblock` is never misclassified.

Reaper passes:

- Skip any job whose `joblock:{id}` key exists. Do not `fail_job` it and do not delete its files.
- **Running-timeout pass:** if `status` is not terminal, `started_at` is set and older than 45 minutes, and `joblock:{id}` is absent: `fail_job(..., "timeout", "This job exceeded the 45 minute limit and was stopped.")`. Check the lock again; if still absent, `cleanup_raw`.
- **Lost-enqueue pass (`enqueued_at IS NULL`):** if `status=queued`, `attempt=0`, `enqueued_at IS NULL`, and `created_at` is older than 3 minutes: call `is_job_in_broker` (plus the 1s re-check if initially false). If the message is present in Redis, leave the row alone. Only when genuinely absent from Redis, `fail_job(..., "enqueue_failed", "Could not start the job. Try again in a moment.")`. Do not use the timeout sentence. There are no media files yet.
- **Lost-queued pass (`enqueued_at` set):** never fail a `queued` job merely because `enqueued_at` is old — up to 3 active jobs can legally wait >45 minutes each behind `--concurrency=1`. Only when `status=queued`, `enqueued_at` is set and older than 3 minutes, and `is_job_in_broker` is false on both checks 1s apart (after re-checking DB `status == 'queued'`): if `SET reaper:redriven:{id} 1 NX EX 86400` succeeds, re-drive `process_job.delay(str(job.id))`, update `enqueued_at = now`, and log `job_redriven`; if `reaper:redriven:{id}` is already set, `fail_job(..., "enqueue_failed", "Could not start the job. Try again in a moment.")`.
- `updated_at` is not an input to any of these decisions.
- **Orphan-media pass:** terminal jobs that still have `jobs/{id}/vod.mp4` and no lock. `cleanup_raw` only. Do not change status.
- `fail_job` on an already terminal row is a no-op, so a reaper and a worker finishing in the same second cannot flip a `completed` job to `failed`.

Killing the worker (`docker compose kill -s KILL worker`) mid-job requeues the message (Key Decision 13). One kill does **not** mark the job failed and does **not** delete the VOD. `begin_attempt` resumes when the artifacts validate, or on the fourth delivery marks `failed` with the worker-lost sentence and then cleans up (including `cleanup_raw` and releasing `joblock:{id}` in `finally`). ffmpeg stays in the worker process group because `start_new_session` is false. `worker_max_tasks_per_child=1` drops Python memory after each successful task. The README smoke notes say this in one sentence so QA does not reject a build that survives a single kill. Compose on the worker, in the prod override (dev Desktop may ignore `mem_limit`):

```yaml
mem_limit: 4g
memswap_limit: 4g
```

`mem_limit` is enforced on the Linux VPS. Docker Desktop on macOS may ignore it; the Celery memory setting and concurrency 1 still apply. ffmpeg is passed `-threads 2` (a cap, not a package pin). The pipeline never reads the VOD into a Python `bytes`.

Do not scale the `beat` service.

#### `scripts/smoke.sh`

Operator script, not run in unit tests. Executable, `set -euo pipefail`.

Usage:

```text
SMOKE_EMAIL=... SMOKE_PASSWORD=... SMOKE_URL=https://www.youtube.com/watch?v=...
SMOKE_API_KEY=... SMOKE_PROVIDER=openai ./scripts/smoke.sh
```

Steps:

1. Refuse to run if `SMOKE_API_KEY` is empty. Do not echo it.
2. `POST /api/v1/auth/register`. On 400 duplicate, continue.
3. `POST /api/v1/auth/login` and read `id`.
4. Sign an HS256 JWT locally with `JWT_SECRET` from `.env` (`iss`, `aud`, `sub`, `exp` = now+1h) using a short Python snippet in the script. This is a second signer and must use the claim set in Key Decision 2. It is not a server endpoint.
5. `POST /api/v1/keys`, then `POST /api/v1/jobs` with `SMOKE_URL`.
6. Poll `GET /api/v1/jobs/{id}` every 5s, up to 45 minutes.
7. Exit 0 if `status=completed` and at least one clip is `ready`. Exit 1 otherwise, printing `status`, `error`, and `progress_message` only.

Smoke is the definition-of-done path for a real URL. Whisper is billed to the user's OpenAI key when `SMOKE_PROVIDER=openai`, and to `OPENAI_API_KEY` only when the saved key is not an OpenAI host. Scoring is always billed to the user key. Document that in the script header. Also document that killing the worker once resumes the job; failure and disk cleanup happen at the attempt cap, the soft limit, or the reaper.

#### README

Update `README.md` so a new engineer does not need this design doc to boot the system. Keep it short. Include:

- Prerequisites: Docker, Node 18+, the backend image already contains ffmpeg.
- `cp .env.example .env && ./start.sh`, and that start fills blank secrets without printing them.
- New env vars from this design (timeouts, rate limits, `WHISPER_LANGUAGE`) in `.env.example` with the same comments.
- `JWT_SECRET` must match across API, worker, and Vercel. `AUTH_SECRET` is a different Vercel-only value. `OPENAI_API_KEY` is the optional Whisper fallback, used only when the user did not save an OpenAI key.
- Limits: 240 min VOD, 8 clips, 3 concurrent jobs, 10 jobs per UTC day, 45 min hard stop, typical 1–2 hour job ~15 min. One worker kill resumes; it does not fail the job by itself.
- Twitch expiry hint.
- Tests: `docker compose up -d db redis api && docker compose exec -T api pytest`. Do not tell the reader to run `cd backend && pytest` against `@db`, which does not resolve on the host, and do not publish Postgres or Redis to make that work. Smoke command.
- Deploy: frontend on Vercel with `NEXT_PUBLIC_API_URL`. VPS uses `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` plus a Caddy block `reverse_proxy 127.0.0.1:8000`. `TRUST_PROXY_COUNT=1`. Do not publish Postgres or Redis. Do not use the dev compose file on the VPS: it bind-mounts `./backend` and runs `uvicorn --reload`.
- Explicit non-goals, one sentence each, so the next contributor does not add them.

`start.sh` and `start.ps1` grow a migrate step in PR 1, not in PR 4. Register cannot wait for a later PR to create `users`. After `docker compose up -d --wait` (the API healthcheck does not need the tables):

```bash
ok=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if docker compose exec -T api alembic upgrade head; then
    ok=1
    break
  fi
  sleep 3
done
if [[ "$ok" -ne 1 ]]; then
  echo "alembic upgrade head failed" >&2
  exit 1
fi
```

`set -e` does not fire on a failure inside `if`, so the script must exit 1 itself. `start.ps1` uses the same 10-try loop and `exit 1` when every attempt fails. The API container entrypoint does **not** migrate. One worker and one api would race. Start scripts and the deploy note run `alembic upgrade head` once.

#### Phase 6 tests

- `cleanup_raw` removes vod, wav, chunks, transcript, and `work/`, and leaves `clips/{id}.mp4`.
- Called twice, no exception.
- A relative path that resolves outside `STORAGE_DIR` raises and deletes nothing.
- Reaper fixture: `started_at` 46 minutes ago, status `scoring`, lock absent, expects `failed`, the timeout sentence, and `vod.mp4` gone.
- The same row with the Redis lock held is not failed and `vod.mp4` is still there.
- `status=queued`, `attempt=0`, `enqueued_at` null, `created_at` 4 minutes ago, and absent from Redis (`joblock`, `unacked`, `celery`) → `enqueue_failed` and the "Could not start" sentence, not the timeout sentence.
- The same `enqueued_at` null row when its `process_job` message is present in Redis `"celery"` or `"unacked"` is left `queued`.
- `status=queued`, `enqueued_at` 90 minutes ago, message present in Redis `"celery"` → left `queued` (legitimate concurrency-1 wait is never failed).
- `status=queued`, `enqueued_at` 10 minutes ago, message absent from Redis → re-drives once on first miss (`reaper:redriven:{id}` set), fails `enqueue_failed` on second miss.
- A job with `updated_at` old but `started_at` 1 minute ago is not reaped.
- `begin_attempt` on a redelivery where `joblock:{id}` holds a dead local PID deletes the stale lock, acquires the lock, and resumes immediately without waiting 60s.
- `begin_attempt` when `joblock:{id}` holds a live PID or another host schedules `process_job.apply_async(args=[job_id], countdown=15)` and returns `("skip", None)` without incrementing `attempt`.
- `begin_attempt` on a row with `attempt == 3` sets `attempt == 4`, marks `failed`, and `process_job` runs `cleanup_raw` and releases `joblock:{id}` in `finally` before acking, without calling the downloader.
- Lock refresher uses CAS `EXPIRE` (does not recreate a deleted key) and is stopped and joined before `release_job_lock`.
- Soft-time-limit path: raising `SoftTimeLimitExceeded` inside a fake pipeline marks `failed` and does not increment attempt a second time beyond the one `begin_attempt` already did.
- README mentions `/api/v1/health`, `MAX_VOD_MINUTES`, and smoke. A grep test asserts the contiguous canary string is absent from the repo (the test builds it by concatenation).

---

## API / Interface Changes

Phase 0 has only `GET /api/v1/health`. Everything else is new. Errors stay `{"detail": "<human-readable message>"}`. CORS methods already include `GET`, `POST`, `DELETE`, `OPTIONS`. No new method is required.

| Method | Path | Auth | Success |
|---|---|---|---|
| `GET` | `/api/v1/health` | no | `200 {"ok": true}` unchanged |
| `GET` | `/api/v1/auth/config` | no | `200 {"guest_enabled": bool}` |
| `POST` | `/api/v1/auth/register` | no | `201 {id, email}` |
| `POST` | `/api/v1/auth/login` | no | `200 {id, email}` |
| `POST` | `/api/v1/auth/guest` | no | `200 {id, email}` (when `ALLOW_GUEST_AUTH=true`) |
| `GET` | `/api/v1/providers` | bearer | `200 {providers}` |
| `GET` | `/api/v1/providers/{provider}/models` | bearer | `200 {provider, connected, model_source, default_model, models}` |
| `POST` | `/api/v1/providers/antigravity/models` | bearer | `200 {provider, connected, model_source, default_model, models}` |
| `POST` | `/api/v1/keys` | bearer | `200 {configured, provider, model}` |
| `GET` | `/api/v1/keys` | bearer | `200` metadata only |
| `DELETE` | `/api/v1/keys` | bearer | `204` |
| `GET` | `/api/v1/jobs` | bearer | `200` list, newest first, max 50 |
| `POST` | `/api/v1/jobs` | bearer | `202 {job_id}` |
| `GET` | `/api/v1/jobs/{id}` | bearer | `200` job plus clips |
| `GET` | `/api/v1/clips/{id}/video` | bearer | `200`/`206` `video/mp4` |

`GET /api/v1/keys`, the `/api/v1/auth/{config,guest}` routes, the `/api/v1/providers*` catalog routes, and the extra job fields are expansions of the `AGENTS.md` §8 baseline. They are additive. `file_path` is intentionally not in any response.

Python interfaces the rest of the pipeline codes against:

```python
def detect_platform(url: str) -> Literal["twitch", "youtube"]: ...
def probe_and_download(url: str, job_dir: Path) -> DownloadResult: ...

class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> list[Word]: ...

class CompletionProvider(Protocol):
    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str: ...

def score_transcript(
    words: list[Word],
    *,
    duration_s: float,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
    provider: str = "compatible",
    max_clips: int = 8,
    job_id: str | None = None,
    client: Any | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
) -> list[ScoredWindow]: ...

def render_clip(vod: Path, window: ScoredWindow, words: list[Word], work_dir: Path, out_path: Path) -> None: ...
def cleanup_raw(job_id: UUID | str) -> None: ...
def transition_job(session: Session, job: Job, new_status: str, message: str) -> None: ...
```

Frontend fetch wrapper `frontend/lib/api.ts` sets `Authorization: Bearer ${session.accessToken}`, `Content-Type: application/json`, and throws an `Error` whose message is `detail` when the body is a string. It does not log bodies.

---

## Data Model Changes

No tables exist in Phase 0 (`backend/app/db.py` defines `Base` only). `backend/alembic/versions/0001_initial.py` creates the four core tables, and `backend/alembic/versions/0002_add_antigravity_provider.py` extends `ck_api_keys_provider` to allow `'antigravity'`. `backend/alembic/env.py` reads `settings.database_url` and `target_metadata = Base.metadata`. `backend/alembic.ini` points `script_location` at `alembic`.

UUIDs are `sqlalchemy.Uuid(as_uuid=True)` with `uuid4` defaults. Timestamps are `DateTime(timezone=True)`, UTC, `server_default=func.now()`.

### `users`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `email` | TEXT NOT NULL UNIQUE | stored lowercased |
| `password_hash` | TEXT NOT NULL | scrypt string (or `"!"` for local guest), never a bare hash |
| `created_at` | timestamptz | |

### `api_keys`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID NOT NULL UNIQUE FK → `users.id` ON DELETE CASCADE | one active key |
| `provider` | TEXT NOT NULL | check in `('openai', 'groq', 'together', 'ollama', 'compatible', 'antigravity')` |
| `encrypted_key` | BYTEA NOT NULL | Fernet token |
| `base_url` | TEXT NOT NULL | no userinfo |
| `model` | TEXT NOT NULL | |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | |

There is no plaintext column, no last-4, and no second row.

### `jobs`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID NOT NULL FK → `users.id` ON DELETE CASCADE | |
| `source_url` | TEXT NOT NULL | |
| `platform` | TEXT NOT NULL | `twitch` or `youtube` |
| `status` | TEXT NOT NULL | the seven states |
| `progress_message` | TEXT NOT NULL DEFAULT `''` | |
| `error` | TEXT NULL | user-facing |
| `title` | TEXT NULL | filled after probe |
| `duration_s` | DOUBLE PRECISION NULL | |
| `thumbnail_url` | TEXT NULL | https only |
| `attempt` | INTEGER NOT NULL DEFAULT 0 | |
| `enqueued_at` | timestamptz NULL | set after `delay()` returns; NULL after 3 min triggers a Redis broker check before failing `enqueue_failed` |
| `started_at` | timestamptz NULL | set only when entering `downloading` |
| `finished_at` | timestamptz NULL | set on terminal |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | |

Indexes:

- `jobs (user_id, created_at DESC)` as in the architecture doc.
- Partial `jobs (started_at) WHERE status NOT IN ('completed', 'failed')` for the reaper.

No `vod_path` and no transcript column. Both are derived from `id`.

### `clips`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `job_id` | UUID NOT NULL FK → `jobs.id` ON DELETE CASCADE | |
| `start_s` | DOUBLE PRECISION NOT NULL | VOD clock |
| `end_s` | DOUBLE PRECISION NOT NULL | |
| `score` | INTEGER NOT NULL | 0–100 check |
| `title` | TEXT NOT NULL | |
| `hook` | TEXT NOT NULL | |
| `score_source` | TEXT NOT NULL DEFAULT `'llm'` | `llm` or `heuristic` |
| `file_path` | TEXT NULL | relative, NULL when failed |
| `caption_status` | TEXT NOT NULL DEFAULT `'pending'` | `pending`, `burned`, `failed` |
| `status` | TEXT NOT NULL DEFAULT `'ready'` | `ready` or `failed` |
| `error` | TEXT NULL | additive, user-facing |
| `created_at` | timestamptz | |

Index: `clips (job_id)`.

Check constraints enforce the enums above, `score BETWEEN 0 AND 100`, and `end_s > start_s`.

### Migration and rollback

Forward: `docker compose exec -T api alembic upgrade head` from `start.sh`, `start.ps1`, and the deploy note. Empty database in Phase 0, with `0001_initial` creating the schema and `0002_add_antigravity_provider` updating `ck_api_keys_provider`.

Rollback of `0001` drops all four tables. Allowed on a dev database only. Do not write a data migration; there is no production data in Phase 0. Later revisions must be additive (new nullable columns, new indexes) so a code rollback does not require a down revision.

`backend/app/db.py` keeps `pool_pre_ping=True`. The API engine uses `pool_size=5` and `max_overflow=5`. The worker does not build a second engine at import. After fork it disposes and rebinds with `pool_size=2`, `max_overflow=0` (see the pipeline section).

`get_session()` today yields and closes with no commit, so a register handler that only `session.add`s would roll the row back. Change it, and use the same helper for the worker. Do not leave routes on a non-committing dependency. `fail_job` already uses `with session_scope()`. That requires `@contextmanager`. `yield from session_scope()` does not work once the function is a context manager, so `get_session` enters it and yields the session to FastAPI.

```python
from contextlib import contextmanager

@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

def get_session() -> Generator[Session, None, None]:
    with session_scope() as session:
        yield session
```

Routes keep `Depends(get_session)`. The worker uses `with session_scope()`. One test registers a user and reads that email back from a second session.

---

## Alternatives Considered

### 1. Probe duration inside `POST /api/v1/jobs` and insert no row when the VOD is too long

This is the literal `docs/ARCHITECTURE.md` §7 row. It was rejected. yt-dlp's metadata probe is a network call with a multi-second typical latency and a hung-extractor tail. Uvicorn would hold a worker for that long, which breaks golden rule 2 and the "job id in milliseconds" rule in `AGENTS.md`. A deadline on the probe creates false 503s. Platform rejection stays on POST because it is a parse, which preserves the "no row for a bad URL" outcome for the check that can actually be cheap. The too-long string still reaches the user via `jobs.error` before the media download starts.

### 2. Auth.js Prisma adapter writing `users` from Vercel

Rejected. It adds Prisma, which is a new framework, and it gives the frontend a Postgres connection string. The deploy split is Vercel plus a VPS. Two writers for `password_hash` will drift. FastAPI remains the only database client. Auth.js is the JWT issuer and the session cookie, which is what the stack lock asked for.

### 3. Store the transcript in `jobs.transcript JSONB`

Rejected for MVP. A 4-hour VOD at ~150 words/minute is on the order of 30–40k words, roughly 2–4 MB of JSON. That is fine for Postgres technically and bad for this product: there is no delete-account or delete-job API, so speech text would live forever, and the worker would pull a multi-megabyte row to clip. `transcript.json` under `STORAGE_DIR` is deleted with the raw VOD, survives worker retry for the life of the job, and is not in `pg_dump` of user speech after completion. Clip titles and scores remain in Postgres because they are the product.

### 4. One ffmpeg pass that burns ASS while cropping

Rejected as the implementation, even though it is faster. The blueprint's burn step is a second invocation with `-c:a copy`, and the ASS file has to exist first anyway. Two passes match `docs/ARCHITECTURE.md` §3.4 and keep the loudnorm encode separate from subtitle failures, so a bad ASS does not force a re-download. The cost is one extra 1080×1920 encode per clip, about a few seconds to a couple of minutes, inside the 45-minute budget for eight clips.

### 5. Mint the JWT in FastAPI and treat Auth.js as a dumb form

Closer to a typical SPA, and easier for `scripts/smoke.sh`. Rejected as the primary design because the stack lock says Auth.js issues the token and FastAPI verifies it. Smoke signs the same claims locally. A FastAPI `issue_jwt()` used by login would become a second production issuer and would drift.

---

## Security & Privacy Considerations

### Threat model

| Threat | Severity | Mitigation |
|---|---|---|
| DB dump leaks LLM keys | High | Fernet at rest. `ENCRYPTION_KEY` is not in the database and not in git. Decryption only inside the worker process, only in a local, for scoring or for Whisper when the stored host is `api.openai.com`. |
| Key appears in logs, Redis, tracebacks, or `jobs.error` | High | `task_ignore_result=True`. Task argument is `job_id` only. structlog redaction processor. `redact()` on every stored `error`. OpenAI and httpx loggers at WARNING. Tests with a concatenated canary. |
| Stolen `JWT_SECRET` forges any user | High | Secret length ≥ 32, not committed, shared only by API, worker, and Vercel server env. 24-hour expiry, no refresh (Key Decision 14). `alg` pinned to HS256. |
| Auth.js default JWE used as the bearer token | High | Custom `SignJWT` HS256 in the `jwt` callback. FastAPI rejects other `alg` values including `none`. |
| XSS steals `session.accessToken` | Medium | Intentional: JSON calls send the bearer from page JS (Key Decision 14). Expiry is 24 hours, not 7 days. React escapes text. No `dangerouslySetInnerHTML`. No markdown renderer. The media route does not put the token in a URL. `AUTH_SECRET` ≠ `JWT_SECRET`. CSP is a follow-up, not this MVP. |
| User A reads user B's job or clip | High | Every query filters `user_id`. Mismatch is 404. Video route uses the same check. Tests cover it. |
| SSRF via yt-dlp or `base_url` | Medium | URL allow-list before yt-dlp. No cookies. Preset providers and `antigravity` are locked to their origins. `compatible` is https plus a public resolved address. Ollama (`host.docker.internal`, `localhost`, `127.0.0.1` with `/v1` or empty path) and local Antigravity bridge URLs (`ANTIGRAVITY_BRIDGE_HTTP_POLICY`) are the only http exceptions. `follow_redirects=False` on every HTTP client. Userinfo rejected. |
| Prompt injection in the transcript asks the model to print the key | Low | The key is the SDK header, never a prompt field. Worst case is garbage JSON and a heuristic fallback. Completions are not logged. |
| Malicious media hitting ffmpeg | Medium | ffprobe before transcode. Subprocess timeouts. Container `mem_limit` 4g. No shell. Image stays on Debian ffmpeg from `python:3.11-slim`; do not curl a static build. |
| Disk fill / job flood | Medium | 8 GiB free check, 3 concurrent, 10/day, 240 min cap, `--max-filesize 8G`, concurrency 1. |
| Login brute force | Medium | Redis 10/minute per client IP, hop `-TRUST_PROXY_COUNT`, and scrypt `n=2**17`. Dummy verify on unknown email. Duplicate register uses the same error string as a bad login. Prod API is bound to loopback so a direct client cannot spoof the forwarded hop. |
| Worker OOM loop | Medium | `attempt` cap at 3 executions, late ack, `prefetch_multiplier` already 1. |
| Fernet key rotation leaves undecryptable rows | Low | Out of scope to rotate in place. Decrypt failure tells the user to enter the key again. Document that changing `ENCRYPTION_KEY` invalidates stored keys. |

### Redaction

`configure_logging()` moves from `backend/app/main.py` to `backend/app/logging.py` and is called from both the API and `celery_app.py`. Processor order is level filter, `add_log_level`, timestamp, `format_exc_info`, **then** `redact_processor`, then `JSONRenderer`. Redaction that runs before `format_exc_info` never sees the traceback, which is where an SDK exception prints a Groq key that was not under a field named `api_key`.

The processor walks every string in the event, including the formatted exception:

- Replace the value when the key is in `api_key`, `authorization`, `encrypted_key`, `openai_api_key`, `password`, `jwt`, `access_token`, `refresh_token`, `id_token`, `antigravity_bridge_token`, `encryption_key`, `jwt_secret`, `secret`.
- Replace `sk-[A-Za-z0-9_-]{8,}`, `ya29\.[A-Za-z0-9._-]{8,}`, `Bearer [A-Za-z0-9._-]+`, and Fernet tokens (`gAAAA` prefix).
- Replace any secret registered with `push_secret` / `secret_scope()` (a `ContextVar` of strings whose length is at least 8, isolated per task via `secret_boundary()`). Wrap the plaintext user key in `secret_scope()` in the keys/antigravity-models routes and again when the worker decrypts it, and `OPENAI_API_KEY` when Whisper uses it. This is what catches `gsk_…` and other non-`sk-` tokens inside `exception`.

Set the `openai` and `httpx` loggers to `WARNING` at startup. Apply `redact()` to `jobs.error` and `clips.error` before insert.

Two canaries, both concatenated at runtime so a repo grep for the contiguous string stays clean:

- `"sk-canary-" + "DO-NOT-LEAK-" + "0123456789abcdef"`
- `"gsk_" + "canary-" + "DO-NOT-LEAK-" + "abcdef0123456789"`

```bash
! grep -R "sk-canary-DO-NOT-LEAK-0123456789abcdef" --exclude-dir=.git .
! grep -R "gsk_canary-DO-NOT-LEAK-abcdef0123456789" --exclude-dir=.git .
```

The Groq-shaped canary must take the exception path, not only the `sk-` one. A test that only uses `sk-` stays green while `gsk_` is printed.

### Secrets that must never appear

- User LLM key, in any of: structlog output, uvicorn access log body, Celery args or Redis result, `jobs.error`, `clips.error`, HTTP response, frontend toast, browser URL, localStorage, transcript file, ASS file, ffmpeg argv.
- `ENCRYPTION_KEY` and `JWT_SECRET`: not logged, not returned, not echoed by `start.sh` (already true).
- `OPENAI_API_KEY`: same treatment as a user key even though it is the operator's. It is not a clip attribute.
- Password: hashed only. Login request is not logged.

Request logging middleware must not be added "for debugging" without a body-size-zero default. Do not enable the OpenAI SDK debug logger.

### Authz of media

`FileResponse` only after ownership and `resolve_under_storage`. `Content-Disposition` filenames are slug-checked. `Cache-Control: private` so a shared proxy does not store the mp4 as a public object. Caddy on the VPS terminates TLS for the API. The browser loads clip bytes from the Next origin, which is already HTTPS on Vercel.

---

## Observability

No Prometheus and no new datastore. structlog JSON to stdout is the telemetry. Operators grep `docker compose logs`.

| Event | Fields (never the key, the transcript, or the source URL) |
|---|---|
| `api_start` | already logged in `main.py` |
| `job_enqueued` | `job_id`, `user_id`, `platform` |
| `job_transition` | `job_id`, `from`, `to` |
| `download_done` | `job_id`, `platform`, `duration_s`, `bytes` |
| `transcribe_done` | `job_id`, `word_count`, `chunks` |
| `score_chunk` | `job_id`, `index`, `total`, `source` |
| `score_done` | `job_id`, `window_count` |
| `clip_failed` | `job_id`, `clip_index`, `stderr` |
| `clip_done` | `job_id`, `ready`, `failed` |
| `job_failed` | `job_id`, `error_code`, `platform`, `elapsed_s` |
| `cleanup_failed` | `job_id` |
| `enqueue_failed` | `job_id` |
| `job_redriven` | `job_id` |

`error_code` is the alert key. Suggested VPS alerts, documented in the README and not built as software:

- More than 5 `job_failed` with `error_code=internal` in 15 minutes.
- Worker container restart loop (OOM kill is visible as Docker exit 137).
- Disk free under 10 GB on the storage volume.
- `reap_stale_jobs` marking more than 0 timeouts in a quiet period — a page, not a ticket system.

Health stays process-local. `GET /api/v1/health` does not touch Postgres or Redis, so the compose healthcheck in `docker-compose.yml` does not flap when the database is migrating. Real dependencies fail the request that needs them, with a user-facing `detail`.

Latency budgets:

| Path | Budget |
|---|---|
| `GET /api/v1/health` | p99 < 20 ms |
| `POST /api/v1/jobs` | p99 < 100 ms server time, no outbound HTTP |
| `POST /api/v1/keys` validation | p99 < 10 s, dominated by the provider |
| Dashboard poll | every 2.5 s while that query's statuses are active, on both the list and the job detail page; list payload well under 50 KB |
| End-to-end typical 1–2 h VOD | ~15 min |
| Hard stop | 2700 s from `started_at` |

Rough stage budget inside the 45 minutes: download often 1–5 minutes for a 30–120 minute VOD and up to ~20 minutes for a long file; Whisper ~24 chunks for 240 minutes at roughly 10–20 s each; scoring at concurrency 4 is a few minutes unless the provider is slow; eight encodes are a few minutes. A 240-minute VOD can still trip the timeout. That is accepted. The README says so. Do not silently raise the cap.

---

## Rollout Plan

MVP is one deployable stack, not a flagged subset of users. Phases land as the PRs at the bottom. Each merged PR boots:

- PR 1 migrates. Register in PR 2 has a `users` table.
- After PR 4, `GET /api/v1/jobs` and `GET /api/v1/jobs/{id}` exist (clips empty). Generate downloads and the job fails with the download stub sentence.
- PR 5 removes that stub, transcribes, and fails with `Transcription finished. Later stages are not included in this build yet.`
- PR 6 removes that stub, writes `windows.json`, and fails with `Scoring finished. Later stages are not included in this build yet.` The row does not sit in `scoring` until a reaper exists.
- PR 7 removes that stub and is the first revision that can `completed` with a clip. `cleanup_raw` is still a no-op, so the VOD remains on disk until PR 9. That is intentional.
- PR 8 is the first revision a non-curl user can operate. List and detail both poll.
- PR 9 turns cleanup on, adds the reaper and the memory cap, and adds the prod compose file.

Dev compose stays the Phase 0 file: `./backend` bind-mounted, `uvicorn --reload`, `8000:8000`, so the host frontend can call the API. That file is not the VPS deploy. `docker-compose.prod.yml` is an override applied as `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`. Compose merges sequence fields by appending, so the override must reset them:

```yaml
services:
  api:
    volumes: !override
      - ./storage:/storage
    ports: !override
      - "127.0.0.1:8000:8000"
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
  worker:
    volumes: !override
      - ./storage:/storage
    mem_limit: 4g
    memswap_limit: 4g
  beat:
    volumes: !override
      - ./storage:/storage
```

No `--reload` and no source mount. Python on the VPS is the image. Apt packages (ffmpeg, `fonts-liberation`) are the image too.

Deploy order on the VPS:

1. Set env, including a real Fernet key, `JWT_SECRET`, a different `AUTH_SECRET` on Vercel, `TRUST_PROXY_COUNT=1`, and optional `OPENAI_API_KEY`. Confirm `CORS_ORIGINS` is the Vercel origin, never `*`.
2. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d db redis api`.
3. `docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T api alembic upgrade head`.
4. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d worker beat`.
5. Point Caddy at `127.0.0.1:8000`. Set Caddy's read timeout above 60s so a Range download of a clip is not cut. JSON APIs stay fast; the long timeout is for the video route.
6. Deploy the frontend to Vercel with `NEXT_PUBLIC_API_URL`, `JWT_SECRET`, `AUTH_SECRET`, `AUTH_URL`.

Rollback while a deploy is still the dev compose file (source bind mount): `git checkout` the previous tree and restart the containers. Redeploying an image does not change the Python that is mounted from the host. The image still has to be redeployed when the rollback needs older apt packages. After `docker-compose.prod.yml` is in use, rollback is redeploy the previous image and restart. Do not down-migrate `0001` if any real user rows exist. Rolling forward again is `upgrade head`, which is a no-op if the revision is present.

Worker and API share one image and one `./storage` volume. Clips written by the worker are readable by the API. Do not give the frontend container that volume.

There is no feature flag. Each `STUB_AFTER_*` constant is deleted by the next PR. None of them is an env var.

---

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Whisper body limit aborts long VODs | High | 10-minute WAV slices inside `WhisperApiTranscriber`. Interface unchanged. |
| `whisper-1` leaves the API on 2027-02-26 | High | Announced shutdown, about three months after this design's date (2026-11-15). `timestamp_granularities=word` is documented for `whisper-1` only. `gpt-4o-transcribe` and `gpt-4o-mini-transcribe` are on the same shutdown list and are limited to `response_format=json`, so they are not a drop-in. `gpt-transcribe` has no confirmed word-timestamp fixture as of this writing. Ship `whisper-1`. Do not switch models in Phase 2. Before 2027-02-26, confirm whether any hosted replacement returns word `start`/`end`. If none does, captions move to a local `Transcriber` behind the same interface. Do not implement faster-whisper in this scope unless launch will miss that date. Hosted captions end on that date if no replacement is confirmed. |
| HS256 vs Auth.js JWE mismatch, every API call 401 | High | Specified encoder, issuer, audience, 24-hour expiry, and a backend test that a `jose`-signed fixture verifies. Called out in Key Decisions. |
| yt-dlp breakage or YouTube bot wall | High | Bound `yt-dlp>=2025.1.15,<2027.1.1`. That is a floor plus a ceiling, not a pin. Raising the ceiling is a deliberate PR. No cookie feature. User sees `download_failed`. |
| API key leaked via exception text | High | Redaction processor plus canary tests on the Whisper and LLM failure paths. |
| 45-minute cap kills 4-hour scoring | Medium | Concurrency 4, skip chunks under 8 words, cap is still real and documented. |
| Missing font, empty captions | Medium | `fonts-liberation` and `Liberation Sans`. |
| Odd crop width, libx264 failure | Medium | Nearest even `crop_w` (608 on 1920×1080) and even `crop_h`. The pass-1 argv test contains `crop=608:1080`. |
| Worker OOM on a long encode | Medium | 4g limit, concurrency 1, threads 2, attempt cap, late ack. |
| Duplicate clip rows after retry | Medium | Clipping deletes prior rows and files for that job before re-export. Terminal deliveries no-op. |
| `FileResponse` Range behavior differs across Starlette | Low | One test for 206. Fallback range reader is specified if it fails. |
| Provider default model names go stale | Medium | Single table in `services/providers/presets.py`. Validation call fails closed with a clear 400. |
| Operator ships without `OPENAI_API_KEY` and users only have Groq keys | Medium | Job fails at transcribe with the explicit sentence. README says Whisper is OpenAI. |

---

## Open Questions

1. **Default chat models** for Groq and Together will rot. The table in `services/providers/presets.py` is a starting point for 2026-11 and was not verified against those vendors on this date. Replace the strings when a provider 404s a model; do not add a model browser UI. Who pays for Whisper is not open: the user OpenAI key wins, and `OPENAI_API_KEY` is only the fallback (Key Decision 3).

2. **Ollama from the compose network.** `host.docker.internal` works on Docker Desktop. On a Linux VPS it needs `extra_hosts: ["host.docker.internal:host-gateway"]` on `api` and `worker`, and Ollama listening on the host. Not added by default because most MVP users will use a hosted key. Add the `extra_hosts` line when the first Ollama user needs it. Do not build an Ollama-in-compose service (that is a new moving part).

No other product decisions are open. Password reset, email verification, clip delete, and R2 are non-goals, not questions. The `whisper-1` shutdown is a dated risk with a decision (stay on `whisper-1` until a word-timestamp fixture exists), not an open product question.

---

## Tests and fixtures

There is no GitHub Actions workflow in the repo today. PR 1 adds `.github/workflows/ci.yml` that runs the command below. Do not claim a check "stays in CI" before that file exists.

`db` and `redis` publish no ports, and `DATABASE_URL` defaults to `@db:5432`, which resolves only on the compose network. Run pytest inside the API container:

```text
docker compose up -d db redis api
docker compose exec -T api pytest
```

`backend/tests/conftest.py` sets, before any `app` import, `ENCRYPTION_KEY` (a real Fernet key), `JWT_SECRET` (32+ chars), `DATABASE_URL=postgresql+psycopg://clipforge:clipforge@db:5432/clipforge_test`, and `REDIS_URL=redis://redis:6379/1`. It connects to the maintenance database `clipforge` only long enough to `CREATE DATABASE clipforge_test` if needed, then runs `alembic upgrade head` against `clipforge_test`. The engine singleton is created from that URL at import. Tests truncate or roll back tables in `clipforge_test` only. Never truncate `clipforge`. If the host name `db` does not resolve, fail immediately with `run: docker compose exec -T api pytest`. Do not fall back to the dev database. Do not use SQLite.

Unit tests that do not need Postgres still run in that same container so there is one command. Ruff stays on the existing select list (`E`, `F`, `I`, `UP`).

Canary rule for every phase that touches a key: build both canaries by concatenation, force each into the failure path (including a non-`sk-` key inside an exception), assert both are absent from log capture and from `jobs.error` / response JSON. The ciphertext assertion is decrypt-roundtrip, not "invalid UTF-8".

---

## References

- `AGENTS.md` — operating manual. Golden rules, phase checklist, API §8, data model §7, conventions §10. §11's one-kill line is overridden in Key Decision 13.
- `docs/ARCHITECTURE.md` — pipeline §3, schema §4, failure table §7. The too-long row in §7 is overridden above. The §3.3 prompt is the v1 scoring asset. Pause-bounded heuristic spans inside a chunk are explicitly not implemented.
- `README.md` — current quickstart, to be updated in phase 6.
- Phase 0 code: `backend/app/main.py`, `backend/app/config.py`, `backend/app/db.py`, `backend/app/workers/celery_app.py`, `backend/tests/test_health.py`, `docker-compose.yml`, `.env.example`, `start.sh`, `start.ps1`, `frontend/app/page.tsx`, `frontend/app/globals.css`.
- OpenAI audio transcriptions: `whisper-1`, `response_format=verbose_json`, `timestamp_granularities=["word"]`, 25 MB upload limit. That limit is why phase 2 slices audio. OpenAI's deprecations list schedules `whisper-1` (and the `gpt-4o-transcribe` family) to shut down on 2027-02-26. Word timestamps had no named hosted successor at design time.
- Auth.js v5 Credentials provider and JWT session strategy. HS256 via `jose`, not the default JWE.

---

## PR Plan

### PR 1 — `feat: add schema, secret checks, and redacting logs` (Shipped)

- **Files:** `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/versions/0001_initial.py`, `backend/app/models.py`, `backend/app/schemas.py`, `backend/app/security.py`, `backend/app/logging.py`, `backend/app/errors.py`, `backend/app/db.py`, `backend/app/config.py`, `backend/app/main.py`, `backend/app/workers/celery_app.py`, `backend/tests/conftest.py`, `backend/tests/test_security.py`, `.env.example`, `start.sh`, `start.ps1`, `.github/workflows/ci.yml`.
- **Depends on:** none.
- **Description:** Hand-written initial migration for `users`, `api_keys`, `jobs` (including `enqueued_at`), and `clips` including `score_source`, `caption_status`, and `clips.error`. `get_session` commits. Fernet helpers, scrypt `n=2**17`, the ordered HS256 verifier, structlog redaction after `format_exc_info`, and lifespan / `worker_process_init` secret checks plus `engine.dispose(close=False)`. No HTTP routes yet. Both start scripts run `docker compose up -d --wait` and then `alembic upgrade head`, and exit 1 if all 10 tries fail. CI runs `docker compose exec -T api pytest` against `clipforge_test`. Health tests keep passing because conftest sets secrets and the test database URL before import. New settings have defaults (`max_concurrent_jobs=3`, `max_jobs_per_day=10`, `job_timeout_seconds=2700`, `whisper_language=en`, `trust_proxy_count=0`, `min_free_disk_bytes`).

### PR 2 — `feat: add email/password auth that verifies Auth.js JWTs` (Shipped)

- **Files:** `backend/app/api/auth.py`, `backend/app/api/deps.py`, `backend/app/main.py`, `backend/tests/test_auth.py`, `backend/tests/fixtures/jwt_jose.json`, `frontend/auth.ts`, `frontend/app/api/auth/[...nextauth]/route.ts`, `frontend/app/login/page.tsx`, `frontend/app/register/page.tsx`, `frontend/app/providers.tsx`, `frontend/middleware.ts`, `frontend/package.json`, `frontend/app/layout.tsx`, `frontend/app/page.tsx`.
- **Depends on:** PR 1.
- **Description:** Register and login on FastAPI. Duplicate register uses the same error as a bad password. Auth.js `jwt` callback stores `token.accessToken` from `SignJWT` (24 hours, HS256) and returns `token`. The committed jose fixture is what Python verifies, plus `alg=none` and the wrong secret. Rightmost-forwarded-for rate limit. Landing header links to sign-in. Middleware gates `/dashboard` and `/jobs` even though those pages are still empty. No key or job UI yet.

### PR 3 — `feat: store one encrypted LLM key per user` (Shipped, later refactored in #9)

- **Files:** `backend/app/api/keys.py`, `backend/app/services/providers/` (`__init__.py`, `presets.py`, `url_policy.py`, `client.py`, `errors.py`), `backend/app/main.py`, `backend/tests/test_keys.py`, `backend/tests/test_providers.py`.
- **Depends on:** PR 2.
- **Description:** `POST/GET/DELETE /api/v1/keys`. Preset origins locked except `ollama` and `compatible`. DNS block list, `follow_redirects=False`, 8s validation call, Fernet upsert, unique `user_id`. Response and logs cannot contain either canary. Ciphertext test is a decrypt roundtrip. The 302-to-metadata test records one connection.

### PR 4 — `feat: ingest a VOD on a Celery job` (Shipped)

- **Files:** `backend/app/services/downloader.py`, `backend/app/services/storage.py`, `backend/app/services/jobs.py`, `backend/app/api/jobs.py`, `backend/app/workers/pipeline.py`, `backend/app/workers/celery_app.py`, `backend/app/main.py`, `backend/requirements.txt`, `docker-compose.yml`, `backend/tests/test_downloader.py`, `backend/tests/test_jobs_api.py`, `backend/tests/test_pipeline.py`.
- **Depends on:** PR 3.
- **Description:** `detect_platform` on POST, including trailing slash and `m.twitch.tv`, rejecting `/embed/`. `jobs.platform` is written on insert. Advisory lock around the 3-job and 10/day counts. 202 plus `process_job.delay(job_id)`, then set `enqueued_at`. `GET /api/v1/jobs` and `GET /api/v1/jobs/{id}` return the list and detail shapes with `clips: []` and no `file_path`. Worker probe-then-download, worker-side length, timeout, and sub-only failures. `cleanup_raw` is a no-op. `begin_attempt` implements terminal check → Redis lock acquire/dead-PID eviction/15s countdown requeue → `attempt += 1` → cap-at-3 `stop` cleanup, with CAS `EXPIRE` refresh and CAS `DEL` release. Celery gains `task_ignore_result`, `visibility_timeout=7200`, `task_routes`, the pipeline import, concurrency 1 (`-Q celery` on `worker`), and `worker_hijack_root_logger=False`. yt-dlp range `>=2025.1.15,<2027.1.1`. `start_new_session=False` on subprocesses.

### PR 4b — `feat: allow skipping sign-in in local guest mode` (Shipped, `#5`)

- **Files:** `.env.example`, `README.md`, `backend/app/api/auth.py`, `backend/app/config.py`, `backend/app/schemas.py`, `backend/tests/test_auth.py`, `frontend/auth.ts`, `frontend/auth.config.ts`, `frontend/lib/auth-mode.ts`, `frontend/app/skip-signin-button.tsx`, `frontend/app/login/login-form.tsx`, `frontend/app/register/register-form.tsx`, `frontend/app/page.tsx`.
- **Depends on:** PR 2.
- **Description:** Adds `ALLOW_GUEST_AUTH` (`GET /api/v1/auth/config`, `POST /api/v1/auth/guest` provisioning `local@clipforge.local`), Edge-safe `auth.config.ts` with explicit `accessTokenExpiresAt` expiry checks, and the **Skip sign-in** button on the landing/login/register pages when enabled.

### PR 5 — `feat: transcribe downloads with Whisper` (Shipped, `#6`)

- **Files:** `backend/app/services/transcribe.py`, `backend/app/workers/pipeline.py`, `backend/app/workers/celery_app.py`, `backend/tests/test_transcribe.py`, `backend/tests/test_pipeline.py`.
- **Depends on:** PR 4.
- **Description:** Remove the download stub. `Transcriber` / `Word`, WAV extract, 10-minute slices, timestamp offset, user-key-first Whisper selection, `whisper-1` only, `transcript.json`, status `transcribing`. Empty `WHISPER_LANGUAGE` omits the hint. Empty speech and missing word timestamps fail the job. Adds `task_soft_time_limit=2700` and `task_time_limit=2760`.

### PR 6 — `feat: score transcripts into highlight windows` (Shipped, `#7`)

- **Files:** `backend/app/services/scorer.py`, `backend/app/workers/pipeline.py`, `backend/tests/test_scorer.py`.
- **Depends on:** PR 5.
- **Description:** Remove the transcribe stub. 45s/40s chunks, architecture §3.3 prompt left verbatim, user message example `{"highlights":[]}`, bare-list wrap, pydantic `before` truncation and score clamp, one JSON retry, whole-token heuristic, pad-then-suppress, top `MAX_CLIPS_PER_JOB`, dead-air clamp. Persist `windows.json`. Decrypt the user key for scoring here. Whisper decryption stays in the transcribe stage when the stored host is `api.openai.com`. Main thread writes progress (`Scoring transcript for highlights` / `Scoring highlights ({done}/{total})`).

### PR 7 — `feat: cut, crop, caption, and stream clips` (Shipped, `#8`)

- **Files:** `backend/app/services/video.py`, `backend/app/api/clips.py`, `backend/app/workers/pipeline.py`, `backend/Dockerfile`, `backend/tests/test_video.py`, `backend/tests/test_clips_api.py`.
- **Depends on:** PR 6.
- **Description:** Remove the score stub. Two-pass ffmpeg, nearest-even crop (`crop=608:1080` on 1920×1080), loudnorm, ASS karaoke with Liberation Sans, per-clip try/except, clip rows. Detail `clips` gain their objects and still omit `file_path`. `GET /api/v1/clips/{id}/video` with Range and download. Dockerfile installs `fonts-liberation`. One failing clip does not fail the job. `cleanup_raw` stays a no-op until PR 9.

### PR 7b — `feat: add provider catalog, model discovery, and Antigravity adapter` (`#10`)

- **Files:** `backend/alembic/versions/0002_add_antigravity_provider.py`, `backend/app/api/providers.py`, `backend/app/api/keys.py`, `backend/app/models.py`, `backend/app/schemas.py`, `backend/app/config.py`, `backend/app/logging.py`, `backend/app/main.py`, `backend/app/services/providers/base.py`, `backend/app/services/providers/catalog.py`, `backend/app/services/providers/url_policy.py`, `backend/app/services/providers/antigravity/`, `backend/app/services/scorer.py`, `backend/tests/test_antigravity.py`, `scripts/antigravity_bridge.py`, `start.sh`, `.env.example`.
- **Depends on:** PR 3, PR 6.
- **Description:** Adds `CompletionProvider` protocol, `GET /api/v1/providers`, `GET /api/v1/providers/{provider}/models`, `POST /api/v1/providers/antigravity/models`, and the `antigravity` non-OpenAI provider (CLI `agy`, host bridge `scripts/antigravity_bridge.py` auto-started by `start.sh`, and Google CloudCode OAuth). Makes `api_key` optional for `antigravity` and adds `ya29.` / bridge token redaction.

### PR 8 — `feat: add the dashboard, key onboarding, and job detail` (Shipped)

- **Files:** `frontend/app/workspace-header.tsx`, `frontend/app/dashboard/page.tsx`, `frontend/app/dashboard/dashboard-client.tsx`, `frontend/app/jobs/[id]/page.tsx`, `frontend/app/jobs/[id]/job-detail-client.tsx`, `frontend/app/api/media/clips/[id]/route.ts`, `frontend/lib/api.ts`, `frontend/lib/jobs.ts`, `frontend/lib/jobs.test.ts`, `frontend/tsconfig.test.json`, `frontend/tsconfig.json`, `frontend/package.json`, `.github/workflows/ci.yml`.
- **Depends on:** PR 2, PR 3, PR 7, PR 7b.
- **Description:** TanStack Query polls the list and the detail query every 2.5s while that query's statuses are active, and only after `session.accessToken` is set. Key panel populated from `GET /api/v1/providers` (supporting optional key for `antigravity`, live model check, and model selection), generate form, Twitch expiry hint, thumbnail on the detail page, clip grid with score, copy, player, download, and literal error text. Same-origin media proxy forwards Range and `Authorization` (using Next.js 15 `await context.params`). `npm test` emits CommonJS and runs `node --test`. CI gains that frontend job. Visual language matches the existing landing tokens.

### PR 9 — `feat: clean storage, time out jobs, and document smoke` (Shipped)

- **Files:** `backend/app/services/storage.py`, `backend/app/workers/pipeline.py`, `backend/app/workers/celery_app.py`, `docker-compose.yml`, `docker-compose.prod.yml`, `scripts/smoke.sh`, `README.md`, `.env.example`, `backend/tests/test_cleanup.py`.
- **Depends on:** PR 7 and PR 8.
- **Description:** Replace the `cleanup_raw` no-op with real deletion, including `windows.json`. `beat` service runs `worker -B -Q reaper --concurrency=1 -s /tmp/celerybeat-schedule`; reaper uses the Redis lock and `started_at` for running timeouts, and verifies Redis broker absence (`joblock`, `unacked`, `celery`) before redriving or failing a `queued` job with `enqueue_failed`. `worker_max_tasks_per_child=1`, `worker_max_memory_per_child`, worker `mem_limit` on the prod override. Prod compose drops the source mount and `--reload`, and binds `127.0.0.1:8000:8000`. `scripts/smoke.sh` registers, signs a JWT, submits a URL, and waits for a ready clip. README states the pytest command, the Whisper key order, the one-kill resume rule, and the non-goals.
