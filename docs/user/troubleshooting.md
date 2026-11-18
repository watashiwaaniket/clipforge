# Troubleshooting

## VOD Download Errors

- **"Only Twitch VODs and YouTube videos are supported."**
  Only direct Twitch VOD links (`/videos/<id>`) and YouTube video links are accepted. Playlists, channel pages, Twitch clips, and live broadcasts are unsupported.
- **"This VOD is subscriber-only — ask the streamer or use a public VOD."**
  ClipForge does not accept stream cookies or subscriber credentials. Use a public VOD.
- **"VOD is too long (max 240 min in this build)."**
  VODs longer than `MAX_VOD_MINUTES` (default 240 minutes) are rejected before media download starts.
- **Twitch VOD expired**
  Twitch deletes VODs automatically after 14 days (or 60 days for Partners). Expired VODs fail to download.

## API Key & Provider Errors

- **"That provider uses a fixed base URL."**
  `openai`, `groq`, and `together` lock their API origin. Leave `base_url` empty when using those presets, or select `compatible` for a custom endpoint.
- **"That key was rejected by the provider."**
  Verify your API key, billing/quota status, and model name. Custom endpoints must return a valid OpenAI-compatible chat completion response without HTTP redirects.
- **Ollama connection failures from Docker**
  Inside Docker Desktop, `http://host.docker.internal:11434/v1` reaches Ollama running on your host machine. Ensure Ollama is listening on the host interface rather than only loopback if running on Linux.
- **"Transcription needs an OpenAI Whisper key on the server, or an OpenAI key saved in your account."**
  You are using a non-OpenAI provider (such as Groq or Ollama) and the server operator has not set `OPENAI_API_KEY`. Switch your saved key to OpenAI or configure `OPENAI_API_KEY` in `.env`.

## Partial Clip Failures

If one clip fails during cutting or caption burn-in, ClipForge marks only that clip as failed and continues exporting the remaining clips in the job.
