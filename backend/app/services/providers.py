import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class ProviderConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderPreset:
    default_base_url: str | None
    default_model: str | None
    locked_base_url: bool


@dataclass(frozen=True)
class ResolvedProviderConfig:
    provider: str
    base_url: str
    model: str


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        locked_base_url=True,
    ),
    "groq": ProviderPreset(
        default_base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        locked_base_url=True,
    ),
    "together": ProviderPreset(
        default_base_url="https://api.together.xyz/v1",
        default_model="meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        locked_base_url=True,
    ),
    "ollama": ProviderPreset(
        default_base_url="http://host.docker.internal:11434/v1",
        default_model="llama3.1",
        locked_base_url=False,
    ),
    "compatible": ProviderPreset(
        default_base_url=None,
        default_model=None,
        locked_base_url=False,
    ),
}

_OLLAMA_ALLOWED_HOSTS = frozenset({"host.docker.internal", "localhost", "127.0.0.1"})
_BLOCKED_HOSTNAMES = frozenset(
    {
        "host.docker.internal",
        "localhost",
        "metadata.google.internal",
        "metadata.google.com",
    }
)
_IPV4_ZERO_NET = ipaddress.ip_network("0.0.0.0/8")
_IPV6_ULA_NET = ipaddress.ip_network("fc00::/7")
_IPV6_LINK_LOCAL_NET = ipaddress.ip_network("fe80::/10")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_blocked_ip(ip.ipv4_mapped)
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _IPV4_ZERO_NET)
        or (
            isinstance(ip, ipaddress.IPv6Address)
            and (ip in _IPV6_ULA_NET or ip in _IPV6_LINK_LOCAL_NET)
        )
    )


def _format_netloc(host: str, port: int | None) -> str:
    formatted_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{formatted_host}:{port}" if port is not None else formatted_host


def strip_url_userinfo(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.username is None and parsed.password is None and "@" not in parsed.netloc:
        return url
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = _format_netloc(host, port)
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def check_outbound_url(base_url: str, *, provider: str = "compatible") -> str:
    url = base_url.strip()
    if not url:
        raise ProviderConfigError("Base URL cannot be empty.")

    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ProviderConfigError("Base URL cannot include credentials.")
    if parsed.query or parsed.fragment:
        raise ProviderConfigError("Base URL cannot include a query string or fragment.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderConfigError("Base URL has an invalid port.") from exc

    scheme = parsed.scheme.lower()
    raw_host = parsed.hostname
    if not raw_host:
        raise ProviderConfigError("Base URL must include a host.")
    host = raw_host.lower().rstrip(".")
    if not host:
        raise ProviderConfigError("Base URL must include a host.")

    if provider == "ollama":
        if scheme != "http":
            raise ProviderConfigError("Ollama base URL must use http.")
        if host not in _OLLAMA_ALLOWED_HOSTS:
            raise ProviderConfigError("Ollama host is not allowed.")
        normalized_path = parsed.path.rstrip("/")
        if normalized_path and not normalized_path.endswith("/v1"):
            raise ProviderConfigError("Ollama path must be empty or end in /v1.")
        return urlunsplit(("http", _format_netloc(host, port), normalized_path, "", ""))

    if scheme != "https":
        raise ProviderConfigError("Base URL must use https.")
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise ProviderConfigError("Base URL host is not allowed.")
    if _is_ip_literal(host):
        raise ProviderConfigError("Base URL must use a DNS hostname, not an IP literal.")

    target_port = port if port is not None else 443
    try:
        addrinfos = socket.getaddrinfo(
            host,
            target_port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ProviderConfigError("Base URL host could not be resolved.") from exc

    if not addrinfos:
        raise ProviderConfigError("Base URL host could not be resolved.")

    for _family, _socktype, _proto, _canonname, sockaddr in addrinfos:
        addr_str = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr_str)
        except ValueError as exc:
            raise ProviderConfigError("Base URL resolved to an invalid address.") from exc
        if _is_blocked_ip(ip):
            raise ProviderConfigError("Base URL resolved to a disallowed address.")

    normalized_path = parsed.path.rstrip("/")
    return urlunsplit(("https", _format_netloc(host, port), normalized_path, "", ""))


def resolve_provider_config(
    provider: str,
    base_url: str | None,
    model: str | None,
) -> ResolvedProviderConfig:
    preset = PROVIDER_PRESETS.get(provider)
    if preset is None:
        raise ProviderConfigError("Unsupported provider.")

    clean_base_url = base_url.strip() if base_url is not None else None
    if clean_base_url == "":
        clean_base_url = None

    clean_model = model.strip() if model is not None else None
    if clean_model == "":
        clean_model = None

    if preset.locked_base_url and clean_base_url is not None:
        raise ProviderConfigError("That provider uses a fixed base URL.")

    if provider == "compatible" and (clean_base_url is None or clean_model is None):
        raise ProviderConfigError('Provider "compatible" needs a base URL and a model.')

    resolved_base_url = clean_base_url if clean_base_url is not None else preset.default_base_url
    resolved_model = clean_model if clean_model is not None else preset.default_model

    if not resolved_base_url or not resolved_model:
        raise ProviderConfigError("Provider base URL and model are required.")

    validated_url = check_outbound_url(resolved_base_url, provider=provider)
    return ResolvedProviderConfig(
        provider=provider,
        base_url=validated_url,
        model=resolved_model,
    )
