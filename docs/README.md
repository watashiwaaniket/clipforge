# ClipForge Documentation

Documentation is split into user guides (for operating ClipForge) and internals (for architectural reasoning, cross-component constraints, and contributor invariants).

## User Guides

- [Getting Started](user/getting-started.md) — Signing in or using guest mode, submitting a Twitch or YouTube VOD, and downloading clips.
- [LLM Providers & API Keys](user/providers.md) — Configuring OpenAI, Groq, Together, Ollama, or custom OpenAI-compatible endpoints, plus transcription key rules.
- [Troubleshooting](user/troubleshooting.md) — Common errors around VOD limits, subscriber-only streams, Ollama local hosts, and transcription keys.

## Internals

- [Architecture Overview](internals/overview.md) — Deployment boundaries, HTTP vs. worker isolation, state machine, and secret redaction.
- [Provider Architecture](internals/providers.md) — Why `services/providers/` is split by concern, SSRF URL policies, client construction, and Whisper key precedence.
- [Adding a Provider](internals/adding-a-provider.md) — Contract and checklist for adding a new OpenAI-compatible preset or a dedicated non-OpenAI adapter.
- [Pipeline Constraints & Invariants](internals/pipeline.md) — Non-obvious worker, Celery, resume-file, and ffmpeg traps.

## Specifications

- [System Blueprint](ARCHITECTURE.md) — Original v1 system architecture and scoring prompt asset.
- [MVP Implementation Design](DESIGN.md) — Detailed Phases 1–6 design contract and conflict resolutions against `ARCHITECTURE.md`.
