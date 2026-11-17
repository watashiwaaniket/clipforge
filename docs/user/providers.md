# LLM Providers & API Keys

ClipForge uses a **Bring Your Own Key (BYOK)** model. Your API key is encrypted at rest and never returned in responses or written to logs.

When you save a key, ClipForge sends a lightweight 1-token test completion to verify the credentials and endpoint before storing it. You can keep one active key per account and replace or delete it at any time.

## Supported Providers

| Provider | Default Base URL | Default Model | Custom Base URL |
|---|---|---|---|
| **OpenAI** | `https://api.openai.com/v1` | `gpt-4o-mini` | Locked |
| **Groq** | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | Locked |
| **Together** | `https://api.together.xyz/v1` | `meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo` | Locked |
| **Ollama** | `http://host.docker.internal:11434/v1` | `llama3.1` | Local HTTP only |
| **Compatible** | Required | Required | HTTPS public hosts only |

- **Model override**: You can override the default model name for any provider.
- **Ollama URL rules**: Ollama accepts `http://` URLs targeting `host.docker.internal`, `localhost`, or `127.0.0.1` with an empty path or one ending in `/v1`.
- **Compatible URL rules**: Custom OpenAI-compatible endpoints must use `https://` with a public DNS hostname (IP addresses, local hostnames, credentials in URLs, query strings, and HTTP redirects are rejected).

## Transcription (Whisper) Note

Highlight scoring works with any supported provider above. Speech-to-text transcription currently uses OpenAI Whisper:

- If you save an **OpenAI** key, ClipForge uses that key for both transcription and highlight scoring.
- If you save a **Groq**, **Together**, **Ollama**, or **Compatible** key, ClipForge uses your key for highlight scoring and falls back to the server's `OPENAI_API_KEY` (if configured) for transcription.
- If the server does not have a fallback `OPENAI_API_KEY` configured, transcription jobs require an OpenAI key on your account.
