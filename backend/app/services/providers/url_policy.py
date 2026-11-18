import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from app.services.providers.errors import ProviderConfigError

OLLAMA_ALLOWED_HOSTS = frozenset({"host.docker.internal", "localhost", "127.0.0.1"})

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


@dataclass(frozen=True)
class OutboundUrlPolicy:
    allow_local_http: bool = False
    allowed_local_hosts: frozenset[str] = frozenset()
    required_path_suffix: str | None = None
    label: str = "Local"


DEFAULT_HTTPS_POLICY = OutboundUrlPolicy()
OLLAMA_LOCAL_HTTP_POLICY = OutboundUrlPolicy(
    allow_local_http=True,
    allowed_local_hosts=OLLAMA_ALLOWED_HOSTS,
    required_path_suffix="/v1",
    label="Ollama",
)


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


def validate_outbound_url(
    base_url: str,
    *,
    policy: OutboundUrlPolicy = DEFAULT_HTTPS_POLICY,
) -> str:
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

    if policy.allow_local_http:
        if scheme != "http":
            raise ProviderConfigError(f"{policy.label} base URL must use http.")
        if host not in policy.allowed_local_hosts:
            raise ProviderConfigError(f"{policy.label} host is not allowed.")
        normalized_path = parsed.path.rstrip("/")
        if policy.required_path_suffix:
            expected_suffix = policy.required_path_suffix.rstrip("/")
            if expected_suffix and not expected_suffix.startswith("/"):
                expected_suffix = f"/{expected_suffix}"
            if (
                normalized_path
                and expected_suffix
                and not normalized_path.endswith(expected_suffix)
            ):
                raise ProviderConfigError(
                    f"{policy.label} path must be empty or end in {expected_suffix}."
                )
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
