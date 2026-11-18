# Adding a Provider

How to add a new LLM provider depends on whether it speaks the OpenAI chat-completions API or uses a different protocol.

## Case 1: OpenAI-Compatible Provider Preset

If the provider supports `/chat/completions` through the `openai` Python SDK, keep it as a preset rather than creating a dedicated folder:

1. **Register the preset** in `backend/app/services/providers/presets.py` (`PROVIDER_PRESETS`), choosing:
   - `default_base_url` and `default_model`
   - `locked_base_url=True` for hosted vendor origins, or `False` for self-hosted/custom endpoints
   - `url_policy` (`DEFAULT_HTTPS_POLICY` for hosted HTTPS endpoints, or an explicit `OutboundUrlPolicy` if local HTTP rules apply)
2. **Allow the provider ID at the API and DB boundaries**:
   - Update the `Literal[...]` on `ApiKeyUpsertRequest.provider` in `backend/app/schemas.py`.
   - Update `ck_api_keys_provider` in `backend/app/models.py` and generate an Alembic migration to alter the `CHECK` constraint.
3. **Add tests**:
   - Cover preset resolution, `base_url` locking/policy, and `build_openai_client()` behavior in `backend/tests/test_providers.py`.
   - Cover `POST /api/v1/keys` persistence in `backend/tests/test_keys.py` if special validation rules apply.
4. **Update user docs**:
   - Document the provider, defaults, and any transcription caveats in [docs/user/providers.md](../user/providers.md).

## Case 2: Non-OpenAI Wire Format (e.g. Anthropic, Gemini, Bedrock)

Do not force non-OpenAI wire formats into `PROVIDER_PRESETS`. Instead:

1. Define or implement the `CompletionProvider` `Protocol` in `backend/app/services/providers/base.py` (analogous to `Transcriber` in `backend/app/services/transcribe.py`).
2. Keep shared OpenAI-compatible presets in `backend/app/services/providers/presets.py`, provider metadata/model listing in `catalog.py`, and shared SSRF/`httpx` helpers in `url_policy.py` and `client.py`.
3. Create a dedicated `backend/app/services/providers/<name>/` package (for example `backend/app/services/providers/antigravity/`) only for a provider with a distinct SDK, authentication scheme, or structured-JSON response contract (`adapter.py`, `errors.py`, tests).
4. Ensure any new client preserves ClipForge's invariants: `follow_redirects=False`, SSRF pre-validation on custom URLs, `secret_scope()` redaction around credentials, and explicit `ProviderConfigError` / domain error mapping.
