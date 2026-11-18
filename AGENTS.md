# AGENTS.md — ClipForge

> This file is the operating manual for any AI coding agent (Claude Code, Cursor, Aider, etc.)
> working on this repository. Read it fully before writing any code.

---

## 1. Mission

Build **ClipForge** — a BYOK (Bring Your Own Key) web app that turns Twitch/YouTube VODs
into vertical, captioned clips for TikTok / YouTube Shorts, targeted at GTA 6 streamers.

Core user flow (MVP — do NOT expand scope):
1. User lands, signs in, pastes a Twitch or YouTube VOD URL.
2. User provides their own LLM API key (OpenAI-compatible: OpenAI, Groq, Together, Ollama, etc.).
3. User hits **Generate**. The system downloads the VOD, transcribes it (word-level
   timestamps), scores segments with the user's LLM for "viral moment" potential,
   cuts the top clips, crops them to 9:16, burns in word-highlighted captions.
4. A dashboard shows the finished clips with scores + auto-titles. User can view and
   download. **No in-browser editing in MVP.**

---

## 2. Golden Rules (non-negotiable)

1. **Never log, print, persist, or echo user API keys.** They are encrypted at rest
   (Fernet/AES-GCM) and only decrypted in-memory inside the worker at job time.
2. **Never process long jobs in the HTTP request path.** Everything after "Generate"
   goes through Celery. The API returns a `job_id` in milliseconds.
3. **One bad clip must never kill a job.** Wrap every clip's extraction in try/except,
   mark it failed, continue with the rest.
4. **Fail loudly in logs, gracefully in the UI.** Every failure path has a user-facing
   error message in the dashboard.
5. **Do not add scope.** No live-stream monitoring, no chat/danmu ingestion, no
   auto-posting to social platforms, no in-browser clip editor, no face-tracking crop.
   These are v2. If you think something is a good idea and it's not in this file or
   docs/ARCHITECTURE.md, ask the human first.
6. **Match the stack.** Do not introduce new frameworks, languages, or databases.
   Exceptions require explicit human approval.

---

## 3. Tech Stack (LOCKED)

| Layer | Choice |
|---|---|
| Frontend | Next.js 14+ (App Router) + Tailwind CSS + TanStack Query |
| Backend API | Python 3.11 + FastAPI |
| Job queue | Celery 5 + Redis (broker + result backend) |
| DB | PostgreSQL 16 + SQLAlchemy 2 + Alembic |
| Downloads | yt-dlp |
| Transcription | OpenAI Whisper API (MVP default) — abstract behind an interface so `faster-whisper` local can be added later |
| LLM scoring | `openai` Python SDK (OpenAI-compatible endpoint covers OpenAI/Groq/Together/Ollama/vLLM) |
| Video processing | ffmpeg (via `ffmpeg-python` or direct subprocess — subprocess preferred for control) |
| Auth | Auth.js (NextAuth) on frontend, JWT verified in FastAPI |
| Encryption | `cryptography` (Fernet) for API keys at rest |
| Local dev | Docker Compose |
| Deploy | Frontend: Vercel. Backend: single VPS (Docker Compose) behind Caddy (auto-HTTPS) |

---

## 4. Monorepo Layout

```
clipforge/
├── AGENTS.md                 # this file
├── README.md                 # human quickstart
├── start.sh                  # one-command local boot (unix)
├── start.ps1                 # one-command local boot (windows)
├── docker-compose.yml        # api, worker, db, redis (dev + prod same file)
├── .env.example              # every env var, documented
├── scripts/
│   └── antigravity_bridge.py # optional host bridge for local agy CLI sessions
├── docs/
│   ├── README.md             # documentation index
│   ├── ARCHITECTURE.md       # system blueprint — read before backend work
│   ├── DESIGN.md             # MVP phases 1–6 design contract
│   ├── internals/            # architectural decisions, provider design, pipeline invariants
│   └── user/                 # user-facing guides
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/              # migrations (0001_initial, 0002_add_antigravity_provider)
│   ├── app/
│   │   ├── main.py           # FastAPI app + routes
│   │   ├── config.py         # pydantic-settings
│   │   ├── db.py             # engine, session, session_scope
│   │   ├── errors.py         # domain errors
│   │   ├── logging.py        # structlog JSON + secret_scope/redaction
│   │   ├── models.py         # SQLAlchemy models
│   │   ├── schemas.py        # pydantic request/response schemas
│   │   ├── security.py       # key encryption/decryption, scrypt, JWT verify
│   │   ├── workers/
│   │   │   ├── celery_app.py
│   │   │   └── pipeline.py   # THE core: download → transcribe → score → clip
│   │   ├── services/
│   │   │   ├── providers/    # base, presets, catalog, url_policy, client, errors, antigravity/
│   │   │   ├── downloader.py # yt-dlp wrapper
│   │   │   ├── jobs.py       # state transitions & fail_job
│   │   │   ├── storage.py    # path resolution, disk check, cleanup_raw
│   │   │   ├── transcribe.py # Whisper API behind `Transcriber` interface
│   │   │   ├── scorer.py     # LLM highlight scoring + prompt + JSON schema
│   │   │   └── video.py      # ffmpeg cut, 9:16 crop, ASS captions, burn-in
│   │   └── api/
│   │       ├── auth.py       # register, login, guest, auth config
│   │       ├── deps.py       # get_current_user bearer dependency
│   │       ├── jobs.py       # create/list/get job
│   │       ├── clips.py      # stream/download clip
│   │       ├── keys.py       # store/read/delete user API key
│   │       └── providers.py  # provider catalog & model discovery
│   └── tests/
├── frontend/
│   ├── Dockerfile            # (prod only; dev runs via npm)
│   ├── package.json
│   ├── auth.ts               # Auth.js v5 config (credentials + guest, HS256 JWT)
│   ├── auth.config.ts        # Edge-safe Auth.js callbacks & expiry
│   ├── middleware.ts         # gates /dashboard and /jobs/*
│   ├── lib/                  # auth-mode, api, jobs helpers
│   └── app/                  # landing, login, register, dashboard, jobs/[id], api/
└── storage/                  # gitignored; local VOD + clip storage (volume-mounted)
```

---

## 5. Commands

```bash
# Full local boot (what contributors run)
./start.sh            # docker compose up --build -d + alembic upgrade head + npm run dev

# Backend only
docker compose up --build -d
docker compose logs -f worker

# Frontend only (expects backend at localhost:8000)
cd frontend && npm install && npm run dev

# Tests (run inside compose network where `db` and `redis` resolve)
docker compose up -d db redis api
docker compose exec -T api pytest

# Migrations
docker compose exec api alembic upgrade head
docker compose exec api alembic revision --autogenerate -m "msg"
```

Frontend talks to the API through `NEXT_PUBLIC_API_URL` (default `http://localhost:8000`).

---

## 6. Environment Variables (`.env.example` must document ALL of these)

```
# Backend
DATABASE_URL=postgresql+psycopg://clipforge:clipforge@db:5432/clipforge
REDIS_URL=redis://redis:6379/0
ENCRYPTION_KEY=            # Fernet key, generated: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
JWT_SECRET=                # HS256 secret shared with Auth.js (min 32 chars)
STORAGE_DIR=/storage
MAX_VOD_MINUTES=240        # reject VODs longer than this (MVP guardrail)
MAX_CLIPS_PER_JOB=8
MAX_CONCURRENT_JOBS=3
MAX_JOBS_PER_DAY=10
JOB_TIMEOUT_SECONDS=2700
MIN_FREE_DISK_BYTES=8589934592
WHISPER_LANGUAGE=en        # empty string omits hint for auto-detect
TRUST_PROXY_COUNT=0        # set 1 behind Caddy in prod
CORS_ORIGINS=http://localhost:3000
ALLOW_GUEST_AUTH=true      # false in public/multi-user deployments

# Whisper (server-side fallback / default provider key — NOT user keys)
OPENAI_API_KEY=            # optional; user BYOK is primary

# Antigravity (optional local CLI / bridge / OAuth overrides)
# ANTIGRAVITY_CLI_BIN=
# ANTIGRAVITY_BRIDGE_URL=http://host.docker.internal:11436
# ANTIGRAVITY_BRIDGE_TOKEN=
# ANTIGRAVITY_OAUTH_CREDS_PATH=
# ANTIGRAVITY_PROJECT_ID=

# Frontend
NEXT_PUBLIC_API_URL=http://localhost:8000
AUTH_SECRET=               # Auth.js cookie encryption secret (distinct from JWT_SECRET)
```

---

## 7. Data Model (simplified — full detail in docs/ARCHITECTURE.md)

- **users**: id, email, password_hash, created_at
- **api_keys**: id, user_id, provider, encrypted_key, base_url, model, created_at, updated_at (one active key per user in MVP)
- **jobs**: id, user_id, source_url, platform (twitch|youtube), status
  (`queued|downloading|transcribing|scoring|clipping|completed|failed`),
  progress_message, error, title, duration_s, thumbnail_url, attempt, enqueued_at, started_at, finished_at, created_at, updated_at
- **clips**: id, job_id, start_s, end_s, score (0-100), title, hook, score_source, file_path,
  caption_status, status (`ready|failed`), error, created_at

Job status is a **state machine** — only forward transitions, `failed` from any state.

---

## 8. API Contract (v1, JSON)

```
GET    /api/v1/health                         → { ok: true }
GET    /api/v1/auth/config                    → { guest_enabled }
POST   /api/v1/auth/register                  { email, password } → { id, email } (201)
POST   /api/v1/auth/login                     { email, password } → { id, email }
POST   /api/v1/auth/guest                     → { id, email } (when ALLOW_GUEST_AUTH=true)
GET    /api/v1/providers                      → { providers: [...] }
GET    /api/v1/providers/{provider}/models    → { provider, connected, model_source, default_model, models }
POST   /api/v1/providers/antigravity/models   { api_key? } → live model probe
GET    /api/v1/keys                           → { configured, provider?, model?, base_url? }
POST   /api/v1/keys                           { provider, api_key?, base_url?, model? } → validate + encrypt store
DELETE /api/v1/keys                           → 204
GET    /api/v1/jobs                           → [ { id, source_url, status, progress_message, error, created_at } ]
POST   /api/v1/jobs                           { url } → { job_id } (202)
GET    /api/v1/jobs/{id}                      → { id, source_url, platform, title, duration_s, thumbnail_url, status, progress_message, error, created_at, updated_at, clips: [clip] }
GET    /api/v1/clips/{id}/video               → mp4 stream (Range + ?download=1 Content-Disposition)
```

All endpoints except `/health` and `/auth/*` require Bearer JWT. Errors: `{"detail": "human-readable message"}`.

---

## 9. Build Order (phases — do them in order, verify each before moving on)

**Phase 0 — Skeleton**
- [x] Repo layout, docker-compose (api, worker, db, redis), Dockerfiles
- [x] `.env.example`, `start.sh`, `start.ps1`
- [x] FastAPI `/health`, Next.js landing page, CI-able `pytest` stub

**Phase 1 — Ingestion**
- [x] `downloader.py`: yt-dlp → local mp4 + metadata (title, duration, platform detect)
- [x] Reject: bad URL, unsupported platform, VOD > `MAX_VOD_MINUTES`, sub-only Twitch VOD (clear error message)
- [x] Celery task `process_job` wired end-to-end

**Phase 2 — Transcription**
- [x] `transcribe.py` with `Transcriber` interface: `transcribe(path) -> Word[]` where `Word = {word, start, end}`
- [x] Whisper API implementation (MVP). Contract must stay identical for a future local faster-whisper impl.
- [x] Job status `transcribing` + progress messages

**Phase 3 — Scoring (the brain)**
- [x] `scorer.py`: chunk transcript → LLM → validated JSON highlights
- [x] Prompt tuned for GTA 6 stream content (see docs/ARCHITECTURE.md §3.3 — the prompt is a v1 asset, iterate on it)
- [x] Map LLM-returned word indices/char offsets → timestamps (NEVER ask the LLM for "seconds")
- [x] Retry once on invalid JSON; on second failure fall back to heuristic scoring (transcript keyword density) and mark `score_source: heuristic`

**Phase 4 — Clipping + Captions**
- [x] `video.py`: ffmpeg re-encode cut, 9:16 center crop (`crop=ih*9/16:ih,scale=1080:1920`), loudness normalize audio
- [x] Generate `.ass` from word timestamps: bold white text, current word yellow, safe margins, 1080x1920
- [x] Burn in with `ffmpeg -vf ass=captions.ass`
- [x] Per-clip try/except, `clips.status` tracking

**Phase 5 — Dashboard**
- [x] Auth (email+password MVP + local guest mode) and key/provider APIs
- [x] Key onboarding screen + provider/model picker on `/dashboard`
- [x] Jobs list with live status (TanStack Query `refetchInterval` 2.5s while active)
- [x] Job detail (`/jobs/[id]`): clip grid, score badge, title + copy button, video player (`/api/media/clips/[id]`), download
- [x] Failed states surfaced with the actual error message

**Phase 6 — Hardening**
- [ ] Storage cleanup: delete raw VOD after job completes (keep clips)
- [ ] Job timeout (45 min) + beat/reaper + worker OOM guardrails + prod compose override
- [ ] README polish, smoke test script (`scripts/smoke.sh`: URL → completed job)

---

## 10. Coding Conventions

- Python: type hints everywhere, `pydantic` for validation, `ruff` for lint/format, `structlog` for logging (JSON). No `print()`.
- TypeScript: strict mode, no `any` unless unavoidable.
- Commits: conventional commits (`feat:`, `fix:`, `chore:`).
- Pull requests: always use a squash commit when merging PRs (e.g., `gh pr merge --squash`) with a conventional commit title/message.
- Every service function takes explicit inputs, returns typed outputs, raises domain exceptions — no silent Nones.
- ffmpeg called via `subprocess.run([...], check=True, capture_output=True)` with parsed errors.
- All external calls (yt-dlp, OpenAI, ffmpeg) wrapped with tenacity retries (max 2, exponential backoff) where retryable.

---

## 11. Definition of Done (whole product)

- `./start.sh` from a fresh clone → dashboard live on :3000, API on :8000
- Paste a real Twitch or YouTube VOD URL + a real OpenAI key → within ~15 min, dashboard shows captioned 9:16 clips with scores and titles
- Killing the worker mid-job → job marked `failed` with clear message, no zombie processes, disk cleaned up
- No API key ever appears in logs, DB dumps, or error traces (grep the repo + logs to prove it)
