# Architecture — ClipForge

Technical blueprint. Lives in `docs/`. Read alongside AGENTS.md. If they conflict, AGENTS.md wins.

---

## 1. System Overview

```
┌────────────┐   HTTPS    ┌──────────────────┐   enqueue    ┌─────────────────┐
│  Next.js   │ ─────────► │  FastAPI (api)   │ ───────────► │  Redis (broker) │
│  (Vercel)  │ ◄───────── │  :8000           │ ◄── results  │                 │
└────────────┘  job/clip  └───────┬──────────┘              └───────┬─────────┘
                                  │ JWT verify, DB                   │ dequeue
                                  ▼                                  ▼
                          ┌──────────────┐                  ┌─────────────────┐
                          │  PostgreSQL  │ ◄────────────────│ Celery worker   │
                          └──────────────┘   write results  │ (heavy lifting) │
                                                            └───────┬─────────┘
                    ┌──────────────┬──────────────┬────────────────┘
                    ▼              ▼              ▼
                 yt-dlp      Whisper API       ffmpeg
                 (download)  (word timestamps) (cut/crop/caption)
                                    │ LLM (user's BYOK key)
                                    ▼
                              OpenAI-compatible
                              scoring endpoint
```

Two deployables: frontend on Vercel, backend compose stack on a VPS
(Caddy → FastAPI; worker colocated, storage on volume, later R2/S3).

## 2. Job State Machine

```
queued → downloading → transcribing → scoring → clipping → completed
              │              │             │            │
              └──────────────┴──── failed ◄┴────────────┘
```
- Forward-only. Any step can throw the job to `failed` with `error` populated.
- `progress_message` updated at every transition (shown live on the dashboard).

## 3. Pipeline Detail

### 3.1 Ingestion (`services/downloader.py`)
- yt-dlp with `--format "bv*[height<=1080]+ba/bv*+ba/b"` → progressive mp4.
- Return: `local_path, platform, title, duration_s, thumbnail_url`.
- Rejections (user-facing messages):
  - URL not Twitch/YouTube → "Only Twitch VODs and YouTube videos are supported."
  - `duration_s > MAX_VOD_MINUTES` → "VOD is too long (max 240 min in this build)."
  - Twitch sub-only → "This VOD is subscriber-only — ask the streamer or use a public VOD."
- Twitch VODs expire after 14 days (60 for Partners) — surfaced as a hint on the upload form.

### 3.2 Transcription (`services/transcribe.py`)
- Interface: `class Transcriber: def transcribe(audio_path) -> list[Word]` where
  `Word = {word: str, start: float, end: float}`.
- MVP impl: OpenAI `audio/transcriptions` (`whisper-1`, `response_format=verbose_json`,
  `timestamp_granularities[]=word`). Extract 16kHz mono WAV first via ffmpeg.
- Future impl (same interface): faster-whisper large-v3-turbo on GPU. The rest of the
  pipeline must not change when this is swapped.

### 3.3 Highlight Scoring (`services/scorer.py`)
- Build ~45s transcript chunks with ~5s overlap. For each chunk send the LLM:

  System prompt (v1 — tune me):
  > You are a short-form content strategist specializing in GTA 6 streaming content.
  > Identify moments with viral clip potential: insane stunts, heist fails, rage moments,
  > funny RP interactions, big reactions, unexpected chaos. Avoid: walking/traveling,
  > menus, dead air, generic commentary. For each candidate return: chunk_word_start,
  > chunk_word_end (indices within the provided chunk), score 0-100, title (max 60 chars,
  > clickbait-y but accurate), hook (one sentence on why it works). Respond with JSON
  > matching the provided schema. If nothing is clip-worthy, return an empty list.

- Chunk-level candidates → merge/sort globally → keep top `MAX_CLIPS_PER_JOB` by score.
- Extend each candidate ±8s, clamp to segment boundaries where transcript shows dead air.
- **Word indices, never timestamps.** LLMs cannot estimate seconds. Map indices →
  word timestamps from the transcriber output.
- Schema-validated with pydantic. Invalid JSON → one retry with "your last reply was
  not valid JSON" appended; second failure → heuristic fallback
  (keyword density + caps-ratio + short-pause-bounded segments), tag `score_source`.

### 3.4 Clipping + Captions (`services/video.py`)
For each scored window [start, end]:
1. **Cut**: `ffmpeg -ss {start-0.5} -to {end+0.5} -i vod.mp4 -c:v libx264 -crf 20 -preset fast -c:a aac clip_raw.mp4`
   (re-encode — frame accuracy + crop need it).
2. **Crop**: `-vf "crop=ih*9/16:ih,scale=1080:1920"` (center crop, MVP).
3. **Captions**: build `.ass` from Words in window —
   - Style: bold, white, OutlineColour black w=4, Fontsize ~68 (1080x1920 PlayRes),
     aligned bottom-center, MarginV 420 (clear of TikTok UI).
   - Karaoke effect on the active word (yellow via `\k` override or per-word events).
   - Scale individual word duration from word timestamps; max 2 lines, auto-break.
4. **Burn**: `ffmpeg -i clip_raw.mp4 -vf "ass=captions.ass" -c:a copy clip_final.mp4`.
5. Wrap all of the above in try/except → `clips.status='failed'` on error, job continues.

Cleanup: raw VOD + intermediate files deleted after job `completed`/`failed` (keep final clips).

## 4. Database Schema (Alembic-managed)

```sql
users        (uuid PK, email UNIQUE, password_hash, created_at)
api_keys     (uuid PK, user_id FK, provider, encrypted_key BYTEA, created_at)
jobs         (uuid PK, user_id FK, source_url, platform, status,
              progress_message TEXT, error TEXT, created_at, updated_at)
clips        (uuid PK, job_id FK, start_s REAL, end_s REAL, score INT,
              title TEXT, hook TEXT, score_source TEXT DEFAULT 'llm',
              file_path TEXT, status TEXT DEFAULT 'ready', created_at)
```
Indexes: `jobs(user_id, created_at DESC)`, `clips(job_id)`.

## 5. API Detail (see AGENTS.md §8 for routes)

- `POST /jobs` validates URL + active key exist, creates `jobs` row (status `queued`),
  calls `process_job.delay(job_id)`, returns 202 + `{job_id}`.
- `GET /jobs/{id}` joins clips, redacts nothing (it's the owner's data), returns
  `progress_message` verbatim for the UI status line.
- `GET /clips/{id}/video` streams from storage with `Content-Type: video/mp4`;
  supports `?download=1` → `Content-Disposition: attachment`.
- Auth: Bearer JWT (Auth.js-issued HS256). FastAPI verifies signature + expiry per request.

## 6. Security Model

- User LLM keys: Fernet-encrypted with `ENCRYPTION_KEY`, decrypted only in the worker
  process right before use, never written to logs or result payloads.
- Rate limit `POST /jobs` per user (e.g. 3 concurrent, 10/day MVP) — VOD downloads are
  the expensive resource.
- JWT_SECRET, ENCRYPTION_KEY never committed; `.env` gitignored; `.env.example` has blanks.
- CORS: allow only the frontend origin(s).

## 7. Failure Modes & Recovery

| Failure | Behavior |
|---|---|
| yt-dlp fails (dead URL, sub-only) | job `failed` with parsed yt-dlp error as `error` |
| Whisper API timeout | retry ×2 backoff; then `failed` |
| LLM returns garbage | 1 retry → heuristic fallback (still completes) |
| ffmpeg crash on one clip | that clip `failed`, job continues |
| Worker OOM/crash mid-job | Celery acks late → job re-queued or marked failed on max retries (3) |
| VOD too long | rejected at `POST /jobs`, no job created |

## 8. Scaling Notes (future, NOT MVP)

- Local faster-whisper on GPU worker instead of Whisper API (cut cost/latency).
- Storage → Cloudflare R2 (zero egress fees — streamers download a lot).
- Horizontal: N workers behind same Redis; per-user job routing.
- Face/gameplay-aware crop (MediaPipe) replacing center crop.
- Live-stream monitoring (streamlink) — big feature, its own design doc.
