import socket

import httpx
import pytest

from app.services import providers
from app.services.providers import (
    DEFAULT_HTTPS_POLICY,
    OLLAMA_LOCAL_HTTP_POLICY,
    PROVIDER_PRESETS,
    OutboundUrlPolicy,
    ProviderConfigError,
    ProviderPreset,
    ResolvedProviderConfig,
    build_openai_client,
    check_outbound_url,
    get_provider_preset,
    resolve_provider_config,
    strip_url_userinfo,
    validate_outbound_url,
)


def _mock_dns(
    monkeypatch: pytest.MonkeyPatch,
    ips: list[str] | None = None,
    *,
    error: OSError | None = None,
) -> None:
    def fake_getaddrinfo(
        host: str,
        port: int | str | None,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ):
        if error is not None:
            raise error
        if not ips:
            return []
        results = []
        for ip in ips:
            af = socket.AF_INET6 if ":" in ip else socket.AF_INET
            target_port = int(port or 443)
            sockaddr = (ip, target_port, 0, 0) if af == socket.AF_INET6 else (ip, target_port)
            results.append((af, socket.SOCK_STREAM, 6, "", sockaddr))
        return results

    monkeypatch.setattr("app.services.providers.url_policy.socket.getaddrinfo", fake_getaddrinfo)


def test_package_exports_expected_symbols() -> None:
    for name in providers.__all__:
        assert hasattr(providers, name)
    assert set(PROVIDER_PRESETS) == {"openai", "groq", "together", "ollama", "compatible"}


@pytest.mark.parametrize(
    ("provider", "expected_url", "expected_model"),
    [
        ("openai", "https://api.openai.com/v1", "gpt-4o-mini"),
        ("groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
        (
            "together",
            "https://api.together.xyz/v1",
            "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        ),
        ("ollama", "http://host.docker.internal:11434/v1", "llama3.1"),
    ],
)
def test_resolve_provider_config_defaults(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    expected_url: str,
    expected_model: str,
) -> None:
    _mock_dns(monkeypatch, ["93.184.216.34"])
    resolved = resolve_provider_config(provider, None, None)
    assert resolved == ResolvedProviderConfig(
        provider=provider,
        base_url=expected_url,
        model=expected_model,
    )


def test_resolve_provider_config_trims_whitespace_and_treats_blank_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_dns(monkeypatch, ["93.184.216.34"])
    resolved = resolve_provider_config("openai", "   ", "  gpt-4o  ")
    assert resolved == ResolvedProviderConfig(
        provider="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4o",
    )


def test_resolve_provider_config_errors() -> None:
    with pytest.raises(ProviderConfigError, match="Unsupported provider."):
        resolve_provider_config("anthropic", None, None)
    assert get_provider_preset("anthropic") is None
    assert isinstance(get_provider_preset("openai"), ProviderPreset)

    for locked in ("openai", "groq", "together"):
        with pytest.raises(ProviderConfigError, match="fixed base URL"):
            resolve_provider_config(locked, "https://custom.example.com/v1", None)

    for base_url, model in ((None, None), ("https://api.example.com/v1", "   "), ("  ", "m")):
        with pytest.raises(
            ProviderConfigError,
            match='Provider "compatible" needs a base URL and a model.',
        ):
            resolve_provider_config("compatible", base_url, model)


def test_resolve_provider_config_compatible_and_ollama_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_dns(monkeypatch, ["93.184.216.34"])
    compat = resolve_provider_config(
        "compatible",
        "  https://API.Example.COM.:8443/v1/ ",
        "  qwen-2.5  ",
    )
    assert compat == ResolvedProviderConfig(
        provider="compatible",
        base_url="https://api.example.com:8443/v1",
        model="qwen-2.5",
    )

    ollama = resolve_provider_config("ollama", "http://127.0.0.1:11434/", "llama3.2")
    assert ollama == ResolvedProviderConfig(
        provider="ollama",
        base_url="http://127.0.0.1:11434",
        model="llama3.2",
    )


def test_strip_url_userinfo_handles_credentials_ipv6_and_plain_urls() -> None:
    assert strip_url_userinfo("https://api.openai.com/v1") == "https://api.openai.com/v1"
    assert (
        strip_url_userinfo("https://user:secret@api.example.com:8443/v1?a=1#frag")
        == "https://api.example.com:8443/v1?a=1#frag"
    )
    assert (
        strip_url_userinfo("https://user:pass@[2001:db8::1]:8443/v1")
        == "https://[2001:db8::1]:8443/v1"
    )
    assert strip_url_userinfo("https://user@api.example.com:/v1") == "https://api.example.com/v1"


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "   ",
        "https://user:pass@api.example.com/v1",
        "https://api.example.com/v1?x=1",
        "https://api.example.com/v1#frag",
        "https://api.example.com:99999/v1",
        "https:///v1",
        "https://./v1",
        "http://api.example.com/v1",
        "https://localhost/v1",
        "https://sub.localhost/v1",
        "https://host.docker.internal/v1",
        "https://metadata.google.internal/v1",
        "https://metadata.google.com/v1",
        "https://1.1.1.1/v1",
        "https://[2606:4700:4700::1111]/v1",
    ],
)
def test_validate_outbound_url_https_rejects_invalid_inputs(bad_url: str) -> None:
    with pytest.raises(ProviderConfigError):
        validate_outbound_url(bad_url, policy=DEFAULT_HTTPS_POLICY)


def test_validate_outbound_url_dns_resolution_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_dns(monkeypatch, error=OSError("nxdomain"))
    with pytest.raises(ProviderConfigError, match="could not be resolved"):
        validate_outbound_url("https://missing.example.com/v1")

    _mock_dns(monkeypatch, [])
    with pytest.raises(ProviderConfigError, match="could not be resolved"):
        validate_outbound_url("https://empty.example.com/v1")

    _mock_dns(monkeypatch, ["not-an-ip"])
    with pytest.raises(ProviderConfigError, match="invalid address"):
        validate_outbound_url("https://weird.example.com/v1")

    for blocked_ip in (
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "0.0.0.1",
        "224.0.0.1",
        "::1",
        "fe80::1%eth0",
        "fc00::1",
        "::ffff:127.0.0.1",
    ):
        _mock_dns(monkeypatch, [blocked_ip])
        with pytest.raises(ProviderConfigError, match="disallowed address"):
            validate_outbound_url("https://rebind.example.com/v1")

    # Multi-record DNS answer must reject if ANY resolved IP is blocked.
    _mock_dns(monkeypatch, ["93.184.216.34", "10.0.0.1"])
    with pytest.raises(ProviderConfigError, match="disallowed address"):
        validate_outbound_url("https://dual.example.com/v1")

    _mock_dns(monkeypatch, ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
    assert validate_outbound_url("https://Example.COM./v1/") == "https://example.com/v1"


def test_validate_outbound_url_local_http_policy() -> None:
    assert (
        validate_outbound_url(
            "http://LocalHost.:11434/v1/",
            policy=OLLAMA_LOCAL_HTTP_POLICY,
        )
        == "http://localhost:11434/v1"
    )
    assert (
        validate_outbound_url(
            "http://127.0.0.1:11434",
            policy=OLLAMA_LOCAL_HTTP_POLICY,
        )
        == "http://127.0.0.1:11434"
    )

    with pytest.raises(ProviderConfigError, match="Ollama base URL must use http."):
        validate_outbound_url("https://localhost:11434/v1", policy=OLLAMA_LOCAL_HTTP_POLICY)

    with pytest.raises(ProviderConfigError, match="Ollama host is not allowed."):
        validate_outbound_url("http://example.com:11434/v1", policy=OLLAMA_LOCAL_HTTP_POLICY)

    with pytest.raises(ProviderConfigError, match="Ollama path must be empty or end in /v1."):
        validate_outbound_url("http://localhost:11434/api", policy=OLLAMA_LOCAL_HTTP_POLICY)

    custom_policy = OutboundUrlPolicy(
        allow_local_http=True,
        allowed_local_hosts=frozenset({"vllm.local"}),
        required_path_suffix="/v1",
        label="vLLM",
    )
    with pytest.raises(ProviderConfigError, match="vLLM host is not allowed."):
        validate_outbound_url("http://localhost:8000/v1", policy=custom_policy)
    assert (
        validate_outbound_url("http://vllm.local:8000/v1", policy=custom_policy)
        == "http://vllm.local:8000/v1"
    )


def test_check_outbound_url_uses_provider_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        check_outbound_url("http://localhost:11434/v1", provider="ollama")
        == "http://localhost:11434/v1"
    )
    _mock_dns(monkeypatch, ["93.184.216.34"])
    assert (
        check_outbound_url("https://api.example.com/v1/", provider="unknown-falls-back-https")
        == "https://api.example.com/v1"
    )


def test_build_openai_client_validates_ssrf_and_configures_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_called = False

    def fail_factory(**_kwargs):
        nonlocal factory_called
        factory_called = True
        raise AssertionError("http_client_factory should not run when SSRF check fails")

    with pytest.raises(ProviderConfigError):
        build_openai_client(
            api_key="sk-test",
            base_url="https://127.0.0.1/v1",
            provider="compatible",
            timeout=httpx.Timeout(5.0),
            http_client_factory=fail_factory,
        )
    assert factory_called is False

    _mock_dns(monkeypatch, ["93.184.216.34"])
    timeout = httpx.Timeout(7.5, connect=2.5)
    http_client, openai_client = build_openai_client(
        api_key="sk-test",
        base_url="https://API.Example.com/v1/",
        provider="compatible",
        timeout=timeout,
    )
    try:
        assert http_client.follow_redirects is False
        assert http_client.timeout == timeout
        assert openai_client.max_retries == 0
        assert str(openai_client.base_url).rstrip("/") == "https://api.example.com/v1"
    finally:
        http_client.close()

    injected = httpx.Client(follow_redirects=False, timeout=timeout)
    try:
        returned_http, injected_openai = build_openai_client(
            api_key="sk-test",
            base_url="http://localhost:11434/v1/",
            provider="ollama",
            timeout=timeout,
            http_client=injected,
        )
        assert returned_http is injected
        assert str(injected_openai.base_url).rstrip("/") == "http://localhost:11434/v1"
    finally:
        injected.close()
