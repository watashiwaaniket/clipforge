# Getting Started

ClipForge turns public Twitch VODs and YouTube videos into scored, vertical (9:16), word-captioned clips ready for TikTok and YouTube Shorts.

## 1. Sign In or Use Guest Mode

- In hosted deployments, create an account on `/register` or sign in on `/login`.
- In local development (when `ALLOW_GUEST_AUTH=true`), click **Skip sign-in** to enter a local workspace immediately.

## 2. Add an API Key

Before generating clips, save an LLM API key in your dashboard settings. See [LLM Providers & API Keys](providers.md) for supported providers and defaults.

## 3. Submit a VOD

1. Paste a public **Twitch VOD** (`twitch.tv/videos/...`) or **YouTube video** (`youtube.com/watch?v=...` or `youtu.be/...`) URL.
2. Click **Generate**.
3. Track live progress on the dashboard (`Queued` → `Downloading` → `Transcribing` → `Scoring` → `Cutting clips` → `Ready`).
4. Preview finished clips directly in the dashboard or download the MP4s.

## Current MVP Limits

- **Maximum VOD length**: 240 minutes (4 hours) by default.
- **Clips per job**: Up to 8 highest-scoring non-overlapping clips.
- **Public VODs only**: Subscriber-only Twitch VODs, live streams, playlists, and private videos are not supported.
- **No in-browser trimming**: Clips are exported automatically with 9:16 center crop and burned-in captions.
