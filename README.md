# ClipForge

Turn public Twitch VODs and YouTube videos into scored, captioned 9:16 vertical clips — BYOK (bring your own API key), no per-clip fees, your keys, your machine.

Built for GTA 6 streamers. Paste a VOD URL, configure an LLM provider (`openai`, `groq`, `together`, `ollama`, `compatible`, or `antigravity`), and generate TikTok / YouTube Shorts clips with burned-in word-level karaoke captions.

## Prerequisites

- Docker & Docker Compose
- Node 18+ (for the local Next.js dev server)
- `ffmpeg` and `fonts-liberation` are already bundled in the backend Docker image

## Quickstart (local dev)

```bash
git clone <repo> && cd clipforge
cp .env.example .env
./start.sh
# open http://localhost:3000 (API on http://localhost:8000, health check GET /api/v1/health)
```

Windows: `.\start.ps1`

`./start.sh` fills blank `ENCRYPTION_KEY`, `JWT_SECRET`, and `AUTH_SECRET` values in `.env` without printing them, waits for Compose, runs `alembic upgrade head`, optionally starts `scripts/antigravity_bridge.py` if `agy` is installed, and launches Next.js.

## Configuration & Secrets

- **`JWT_SECRET`** must be identical across the API, worker, and frontend (`frontend` / Vercel). It signs short-lived HS256 bearer tokens.
- **`AUTH_SECRET`** is a separate secret used only by Auth.js on the frontend to encrypt its session cookie. Never reuse `JWT_SECRET`.
- **`OPENAI_API_KEY`** is an optional server-side Whisper fallback, used only when the user did not save an OpenAI key (`api.openai.com`). Scoring always uses the user's configured provider.
- **`ALLOW_GUEST_AUTH`** defaults to `true` in `.env.example` for local **Skip sign-in** workspaces (`false` in `backend/app/config.py` and for public/multi-user deployments).
- See [.env.example](.env.example) for `MAX_VOD_MINUTES`, `MAX_CLIPS_PER_JOB`, `MAX_CONCURRENT_JOBS`, `MAX_JOBS_PER_DAY`, `JOB_TIMEOUT_SECONDS`, `MIN_FREE_DISK_BYTES`, `WHISPER_LANGUAGE`, `TRUST_PROXY_COUNT`, and `ANTIGRAVITY_*`.

## Limits & Runtime Behavior

- **`MAX_VOD_MINUTES=240`**: VODs longer than 240 minutes are rejected after probe before media download.
- **Clips & quotas**: up to `8` clips per job, `3` concurrent active jobs per user, `10` jobs per UTC day.
- **Timeout & duration**: typical 1–2 hour VODs finish in ~15 minutes; hard stop is `45` minutes (`JOB_TIMEOUT_SECONDS=2700`, enforced by Celery soft time limit and the `beat` reaper).
- **Worker crash recovery**: killing the worker once (`docker compose kill -s KILL worker`) requeues and resumes from valid artifacts (`vod.mp4`, `transcript.json`, `windows.json`); it does not fail the job on a single kill. Failure and raw storage cleanup happen on the 4th attempt, at the 45-minute timeout, or during reaper cleanup.
- **Twitch expiry**: Twitch deletes VODs after 14 days (60 days for Partners).

## Tests & Smoke Check

Run backend tests inside the `api` container (do **not** run `pytest` on the host against `@db`, and do not publish Postgres/Redis ports):

```bash
docker compose up -d db redis api
docker compose exec -T api pytest
```

Frontend unit tests and build check:

```bash
cd frontend && npm test && npm run build
```

End-to-end operator smoke test against a real VOD URL and live stack:

```bash
SMOKE_EMAIL=smoke@example.com SMOKE_PASSWORD=your-password-10chars \
SMOKE_URL="https://www.youtube.com/watch?v=..." \
SMOKE_API_KEY="sk-..." SMOKE_PROVIDER=openai ./scripts/smoke.sh
```

## Production Deploy

- **Frontend (Vercel)**: set `NEXT_PUBLIC_API_URL` (and `API_URL` if different), `JWT_SECRET`, a distinct `AUTH_SECRET`, and `AUTH_URL`.
- **Backend (VPS)**: never run the dev `docker-compose.yml` alone in production (it bind-mounts `./backend` and runs `uvicorn --reload`). Use the production override and keep Postgres and Redis unpublished:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d db redis api
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T api alembic upgrade head
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d worker beat
```

Put Caddy in front of `127.0.0.1:8000` (`reverse_proxy 127.0.0.1:8000`) and set `TRUST_PROXY_COUNT=1`.

## Non-Goals (Out of MVP Scope)

- No live-stream monitoring, chat/danmu ingestion, or auto-posting to social platforms.
- No face-tracking crop, in-browser video editor, trim handles, or per-clip fees.
- No share links, public unauthenticated clip URLs, account deletion, email verification, or password reset.
- No multiple active keys per user, playlist/Twitch-clip/channel downloads, or `yt-dlp` cookie uploads.
- No `faster-whisper` implementation (only the `Transcriber` interface), S3/R2 object storage, or external metrics database.

See [docs/README.md](docs/README.md) for user guides, internal architecture notes, and design specs, and [AGENTS.md](AGENTS.md) for contributor/agent rules.
