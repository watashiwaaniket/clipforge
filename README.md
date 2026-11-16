# ClipForge

Turn your Twitch/YouTube VODs into viral vertical clips — BYOK (bring your own API key),
no per-clip fees, your keys, your machine (or ours).

Built for GTA 6 streamers. Paste a VOD URL, add your LLM API key, hit generate —
get scored, captioned, 9:16 clips ready for TikTok / YouTube Shorts.

## Quickstart (local dev)

```bash
git clone <repo> && cd clipforge
cp .env.example .env          # fill in ENCRYPTION_KEY + JWT_SECRET
./start.sh                    # backend via Docker + frontend hot-reload
# open http://localhost:3000
```

Windows: `.\start.ps1`

Requires: Docker, Node 18+, ffmpeg is bundled in the backend image.

`.env.example` sets `ALLOW_GUEST_AUTH=true` so local clones can skip sign-in and jump straight into a local workspace. Set `ALLOW_GUEST_AUTH=false` for public/multi-user deployments.

## Stack

Next.js + Tailwind · FastAPI · Celery + Redis · PostgreSQL · yt-dlp · Whisper · ffmpeg

## Deploy

- Frontend → Vercel (`NEXT_PUBLIC_API_URL` = your API origin)
- Backend → any VPS with Docker: `docker compose up -d` + Caddy for TLS

See docs/ARCHITECTURE.md for the full blueprint and AGENTS.md for contributor/agent rules.
