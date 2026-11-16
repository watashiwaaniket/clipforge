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
├── docs/
│   └── ARCHITECTURE.md       # system design — read before backend work
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py           # FastAPI app + routes
│   │   ├── config.py         # pydantic-settings
│   │   ├── db.py             # engine, session
│   │   ├── models.py         # SQLAlchemy models
│   │   ├── schemas.py        # pydantic request/response schemas
│   │   ├── security.py       # key encryption/decryption, JWT verify
│   │   ├── workers/
│   │   │   ├── celery_app.py
│   │   │   └── pipeline.py   # THE core: download → transcribe → score → clip
│   │   ├── services/
│   │   │   ├── downloader.py # yt-dlp wrapper
│   │   │   ├── transcribe.py # Whisper API behind `Transcriber` interface
│   │   │   ├── scorer.py     # LLM highlight scoring + prompt + JSON schema
│   │   │   └── video.py      # ffmpeg cut, 9:16 crop, ASS captions, burn-in
│   │   └── api/
│   │       ├── jobs.py       # create/list/get job
│   │       ├── clips.py      # list clips for job, stream/download clip
│   │       └── keys.py       # store/validate/delete user API key
│   └── tests/
├── frontend/
│   ├── Dockerfile            # (prod only; dev runs via npm)
│   ├── package.json
│   └── app/                  # landing, dashboard, job/[id] pages
└── storage/                  # gitignored; local VOD + clip storage (volume-mounted)
```

---

## 5. Commands

```bash
# Full local boot (what contributors run)
./start.sh            # docker compose up --build -d + npm install + npm run dev

# Backend only
docker compose up --build -d
docker compose logs -f worker

# Frontend only (expects backend at localhost:8000)
cd frontend && npm install && npm run dev

# Tests
cd backend && pytest

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
JWT_SECRET=
STORAGE_DIR=/storage
MAX_VOD_MINUTES=240        # reject VODs longer than this (MVP guardrail)
MAX_CLIPS_PER_JOB=8

# Whisper (server-side fallback / default provider key — NOT user keys)
OPENAI_API_KEY=            # optional; user BYOK is primary

# Frontend
NEXT_PUBLIC_API_URL=http://localhost:8000
```

---

## 7. Data Model (simplified — full detail in docs/ARCHITECTURE.md)

- **users**: id, email, password_hash, created_at
- **api_keys**: id, user_id, provider, encrypted_key, created_at  (one active key per user in MVP)
- **jobs**: id, user_id, source_url, platform (twitch|youtube), status
  (`queued|downloading|transcribing|scoring|clipping|completed|failed`),
  progress_message, error, created_at, updated_at
- **clips**: id, job_id, start_s, end_s, score (0-100), title, hook, file_path,
  caption_status, status (`ready|failed`), created_at

Job status is a **state machine** — only forward transitions, `failed` from any state.

---

## 8. API Contract (v1, JSON)

```
POST   /api/v1/keys              { provider, api_key }            → validate + encrypt store
DELETE /api/v1/keys
GET    /api/v1/jobs              → [ { id, source_url, status, created_at } ]
POST   /api/v1/jobs              { url }                          → { job_id } (202)
GET    /api/v1/jobs/{id}         → { id, status, progress_message, clips: [clip], error }
GET    /api/v1/clips/{id}/video  → mp4 stream (with Content-Disposition for download)
GET    /api/v1/health            → { ok: true }
```

All endpoints except `/health` require Bearer JWT. Errors: `{"detail": "human-readable message"}`.

---

## 9. Build Order (phases — do them in order, verify each before moving on)

**Phase 0 — Skeleton**
- [ ] Repo layout, docker-compose (api, worker, db, redis), Dockerfiles
- [ ] `.env.example`, `start.sh`, `start.ps1`
- [ ] FastAPI `/health`, Next.js landing page, CI-able `pytest` stub

**Phase 1 — Ingestion**
- [ ] `downloader.py`: yt-dlp → local mp4 + metadata (title, duration, platform detect)
- [ ] Reject: bad URL, unsupported platform, VOD > `MAX_VOD_MINUTES`, sub-only Twitch VOD (clear error message)
- [ ] Celery task `process_job` wired end-to-end (download step only, stub the rest)

**Phase 2 — Transcription**
- [ ] `transcribe.py` with `Transcriber` interface: `transcribe(path) -> Word[]` where `Word = {word, start, end}`
- [ ] Whisper API implementation (MVP). Contract must stay identical for a future local faster-whisper impl.
- [ ] Job status `transcribing` + progress messages

**Phase 3 — Scoring (the brain)**
- [ ] `scorer.py`: chunk transcript → LLM → validated JSON highlights
- [ ] Prompt tuned for GTA 6 stream content (see docs/ARCHITECTURE.md §3.3 — the prompt is a v1 asset, iterate on it)
- [ ] Map LLM-returned word indices/char offsets → timestamps (NEVER ask the LLM for "seconds")
- [ ] Retry once on invalid JSON; on second failure fall back to heuristic scoring (transcript keyword density) and mark `score_source: heuristic`

**Phase 4 — Clipping + Captions**
- [ ] `video.py`: ffmpeg re-encode cut, 9:16 center crop (`crop=ih*9/16:ih,scale=1080:1920`), loudness normalize audio
- [ ] Generate `.ass` from word timestamps: bold white text, current word yellow, safe margins, 1080x1920
- [ ] Burn in with `ffmpeg -vf ass=captions.ass`
- [ ] Per-clip try/except, `clips.status` tracking

**Phase 5 — Dashboard**
- [ ] Auth (email+password MVP), key onboarding screen
- [ ] Jobs list with live status (TanStack Query `refetchInterval` 2.5s while active)
- [ ] Job detail: clip grid, score badge, title + copy button, video player, download
- [ ] Failed states surfaced with the actual error message

**Phase 6 — Hardening**
- [ ] Storage cleanup: delete raw VOD after job completes (keep clips)
- [ ] Job timeout (45 min) + worker OOM guardrails
- [ ] README polish, smoke test script (`scripts/smoke.sh`: URL → completed job)

---

## 10. Coding Conventions

- Python: type hints everywhere, `pydantic` for validation, `ruff` for lint/format, `structlog` for logging (JSON). No `print()`.
- TypeScript: strict mode, no `any` unless unavoidable.
- Commits: conventional commits (`feat:`, `fix:`, `chore:`).
- Every service function takes explicit inputs, returns typed outputs, raises domain exceptions — no silent Nones.
- ffmpeg called via `subprocess.run([...], check=True, capture_output=True)` with parsed errors.
- All external calls (yt-dlp, OpenAI, ffmpeg) wrapped with tenacity retries (max 2, exponential backoff) where retryable.

---

## 11. Definition of Done (whole product)

- `./start.sh` from a fresh clone → dashboard live on :3000, API on :8000
- Paste a real Twitch or YouTube VOD URL + a real OpenAI key → within ~15 min, dashboard shows captioned 9:16 clips with scores and titles
- Killing the worker mid-job → job marked `failed` with clear message, no zombie processes, disk cleaned up
- No API key ever appears in logs, DB dumps, or error traces (grep the repo + logs to prove it)
