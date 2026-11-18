# Provider Architecture

ClipForge supports multiple BYOK LLM endpoints (`openai`, `groq`, `together`, `ollama`, `compatible`) plus local CLI/OAuth `antigravity` through [`backend/app/services/providers/`](../../backend/app/services/providers/__init__.py).

## Why Split by Concern Instead of Per-Provider Folders

Every OpenAI-compatible provider speaks the same chat-completions wire protocol via the `openai` Python SDK. Creating separate subpackages for `openai/`, `groq/`, `together/`, `ollama/`, and `compatible/` would duplicate identical SDK boilerplate for differences that are purely configuration data.

Instead, `services/providers/` isolates the shared responsibilities and keeps non-OpenAI adapters separate:

- **`base.py`**: Shared `CompletionProvider` protocol, `ProviderModelInfo`, and `ResolvedProviderConfig` dataclasses.
- **`presets.py`**: Pure data table (`PROVIDER_PRESETS`) mapping OpenAI-compatible provider IDs to default URLs, default models, whether `base_url` is locked, whether custom fields are required, and which URL safety policy applies.
- **`catalog.py`**: Provider catalog & model discovery (`list_provider_catalog()`, `get_provider_catalog_entry()`) exposed via [`backend/app/api/providers.py`](../../backend/app/api/providers.py) (`GET /api/v1/providers`, `GET /api/v1/providers/{provider}/models`, `POST /api/v1/providers/antigravity/models`). Probes live models for `antigravity` and `ollama` (`/api/tags`) and falls back to curated model lists otherwise.
- **`url_policy.py`**: SSRF defense (`OutboundUrlPolicy`, `validate_outbound_url`, `strip_url_userinfo`) parameterized by policy rather than branching on provider names.
- **`client.py`**: Shared `build_openai_client()` builder that enforces SSRF validation *before* instantiating `httpx.Client(follow_redirects=False)` and `OpenAI(..., max_retries=0)`. Both API key validation (`api/keys.py`) and highlight scoring (`services/scorer.py`) go through this path so redirect and timeout behavior cannot drift.
- **`errors.py`**: Domain validation exception (`ProviderConfigError`).
- **`antigravity/`**: Dedicated non-OpenAI adapter (`AntigravityProvider`) supporting local `agy` CLI execution, the host bridge ([`scripts/antigravity_bridge.py`](../../scripts/antigravity_bridge.py) over `ANTIGRAVITY_BRIDGE_URL`), and OAuth credentials against `https://daily-cloudcode-pa.googleapis.com` (`v1internal:loadCodeAssist`, `v1internal:fetchAvailableModels`, `v1internal:generateContent`). See [Adding a Provider](adding-a-provider.md).

## Outbound URL & SSRF Constraints

Because users can supply custom `base_url` values for `ollama` and `compatible`, validation runs both when saving a key (`POST /api/v1/keys`) and again inside the worker before scoring:

- **No credentials, queries, or fragments** in `base_url`; `GET /api/v1/keys` also calls `strip_url_userinfo()` defensively.
- **No redirects**: Every `httpx.Client` sets `follow_redirects=False` so a `301`/`302` cannot pivot from a public domain to cloud metadata (`169.254.169.254`).
- **Standard HTTPS policy (`DEFAULT_HTTPS_POLICY`)**: Requires `https`, rejects IP literals and metadata/local hostnames, resolves all A/AAAA records via `socket.getaddrinfo`, and fails if *any* returned IP (including IPv4-mapped IPv6 addresses) is loopback, private, link-local, unspecified, `0.0.0.0/8`, ULA, or multicast.
- **Local HTTP policy (`OLLAMA_LOCAL_HTTP_POLICY`)**: Allows `http` strictly for `host.docker.internal`, `localhost`, or `127.0.0.1` with an empty path or one ending in `/v1`.
- **Antigravity bridge policy (`ANTIGRAVITY_BRIDGE_HTTP_POLICY`)**: Allows `http` strictly for `host.docker.internal`, `localhost`, or `127.0.0.1` at the root path (non-local bridge URLs must use `https` under `DEFAULT_HTTPS_POLICY`).

## Scoring vs. Transcription Key Precedence

Highlight scoring always uses the user's stored BYOK key. Transcription uses OpenAI Whisper (`whisper-1`), which non-OpenAI chat providers do not serve:

1. If the user's stored key targets `api.openai.com`, the worker uses that key for Whisper too.
2. Otherwise, the worker falls back to the server-side `OPENAI_API_KEY` environment variable.
3. If neither is available, the job fails at transcription with an explicit error explaining that non-OpenAI providers can score clips but cannot transcribe in this build.
