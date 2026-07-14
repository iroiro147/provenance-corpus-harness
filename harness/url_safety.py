"""Credential-safe URL handling and public-network resolution checks.

The collector treats every operator-supplied URL as untrusted. URLs must use
HTTP(S), must not contain credentials, and must resolve exclusively to globally
routable addresses before the transport opens a socket.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

Resolver = Callable[[str], Iterable[str]]

_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:^|[_-])(?:access[_-]?token|auth(?:orization)?|api[_-]?key|"
    r"client[_-]?secret|cookie|credential|jwt|password|secret|session|"
    r"signature|signed|sig|token)(?:$|[_-])",
    re.I,
)
_PROVIDER_SIGNATURE_KEY = re.compile(
    r"^(?:x-amz-|x-goog-|awsaccesskeyid$|googleaccessid$|policy$)", re.I
)
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(access[_-]?token|api[_-]?key|client[_-]?secret|cookie|password|"
    r"secret|session|signature|token)\s*[:=]\s*([^\s,;]+)",
    re.I,
)
_AUTH_HEADER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I)

DEFAULT_TRACKING_QUERY_KEYS = (
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
)


class UnsafeUrlError(ValueError):
    """Raised when a URL cannot be fetched or persisted safely."""


@dataclass(frozen=True)
class UrlCredentialInspection:
    has_userinfo: bool
    sensitive_query_keys: tuple[str, ...]


def is_sensitive_query_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    return bool(
        _SENSITIVE_QUERY_KEY.search(normalized) or _PROVIDER_SIGNATURE_KEY.search(normalized)
    )


def inspect_url_credentials(raw_url: str, base_url: str | None = None) -> UrlCredentialInspection:
    parsed = urlsplit(urljoin(base_url, raw_url) if base_url else raw_url)
    sensitive = tuple(
        sorted(
            {
                key
                for key, value in parse_qsl(parsed.query)
                if is_sensitive_query_key(key) or _nested_url_has_credentials(value)
            }
        )
    )
    return UrlCredentialInspection(
        has_userinfo=bool(parsed.username or parsed.password),
        sensitive_query_keys=sensitive,
    )


def assert_safe_public_url(raw_url: str, *, base_url: str | None = None) -> str:
    """Validate syntax and credential safety without opening the network.

    DNS resolution is intentionally separate so tests and transports can inject
    a resolver. :func:`resolve_public_addresses` must run immediately before a
    live socket is opened.
    """

    absolute = urljoin(base_url, raw_url) if base_url else raw_url
    parsed = urlsplit(absolute)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("URL must use http or https")
    if not parsed.hostname:
        raise UnsafeUrlError("URL must include a hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("URL contains an invalid port") from exc
    if port is not None and port <= 0:
        raise UnsafeUrlError("URL port must be positive")

    hostname = _normalize_hostname(parsed.hostname)
    if _blocked_hostname(hostname):
        raise UnsafeUrlError(f"URL hostname is blocked: {hostname}")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise UnsafeUrlError(f"URL address is not globally routable: {hostname}")

    credentials = inspect_url_credentials(absolute)
    if credentials.has_userinfo:
        raise UnsafeUrlError("URL must not contain username/password credentials")
    if credentials.sensitive_query_keys:
        keys = ", ".join(credentials.sensitive_query_keys)
        raise UnsafeUrlError(f"URL must not contain sensitive query parameters: {keys}")
    return urlunsplit(parsed)


def resolve_public_addresses(hostname: str, resolver: Resolver | None = None) -> tuple[str, ...]:
    """Resolve ``hostname`` and fail closed if *any* answer is non-public."""

    normalized = _normalize_hostname(hostname)
    if _blocked_hostname(normalized):
        raise UnsafeUrlError(f"URL hostname is blocked: {normalized}")

    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    raw_addresses = (
        [normalized] if literal is not None else list((resolver or _default_resolver)(normalized))
    )
    if not raw_addresses:
        raise UnsafeUrlError(f"URL hostname resolved to no addresses: {normalized}")

    addresses: list[str] = []
    for raw in raw_addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise UnsafeUrlError(f"URL hostname resolved to an invalid address: {raw}") from exc
        if not address.is_global:
            raise UnsafeUrlError(f"URL hostname resolves to a non-public address: {address}")
        rendered = address.compressed
        if rendered not in addresses:
            addresses.append(rendered)
    return tuple(addresses)


def sanitize_url_for_persistence(raw_url: str, base_url: str | None = None) -> str:
    """Remove userinfo, fragments, and secret-bearing query parameters."""

    absolute = urljoin(base_url, raw_url) if base_url else raw_url
    try:
        parsed = urlsplit(absolute)
    except ValueError:
        return "<redacted-url>"
    if not parsed.hostname:
        if (
            "://" in absolute
            or _SECRET_ASSIGNMENT.search(absolute)
            or _AUTH_HEADER.search(absolute)
        ):
            return "<redacted-url>"
        return raw_url
    try:
        port = parsed.port
    except ValueError:
        return "<redacted-url>"
    host = _format_host(parsed.hostname, port)
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not is_sensitive_query_key(key) and not _nested_url_has_credentials(value)
        ],
        doseq=True,
    )
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", query, ""))


def canonicalize_url(
    raw_url: str,
    *,
    base_url: str | None = None,
    strip_query_params: Iterable[str] = DEFAULT_TRACKING_QUERY_KEYS,
) -> str:
    """Return a stable, credential-free crawl identity."""

    safe = assert_safe_public_url(raw_url, base_url=base_url)
    parsed = urlsplit(safe)
    strip = {key.lower() for key in strip_query_params}
    params = sorted(
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in strip and not is_sensitive_query_key(key)
    )
    scheme = parsed.scheme.lower()
    port = parsed.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    host = _format_host(_normalize_hostname(parsed.hostname or ""), port)
    path = parsed.path or "/"
    return urlunsplit((scheme, host, path, urlencode(params, doseq=True), ""))


def redact_sensitive_text(value: str) -> str:
    def redact_url(match: re.Match[str]) -> str:
        candidate = match.group(0)
        trailing_match = re.search(r"[.,;:!?]+$", candidate)
        trailing = trailing_match.group(0) if trailing_match else ""
        url = candidate[: -len(trailing)] if trailing else candidate
        try:
            return f"{sanitize_url_for_persistence(url)}{trailing}"
        except Exception:  # noqa: BLE001 - redaction must never expose the original
            return "<redacted-url>"

    output = _URL_IN_TEXT.sub(redact_url, value)
    output = _AUTH_HEADER.sub(r"\1 <REDACTED>", output)
    return _SECRET_ASSIGNMENT.sub(r"\1=<REDACTED>", output)


def sanitize_metadata(value, *, key: str = ""):
    """Recursively remove URL credentials and obvious secrets from metadata."""

    if key and is_sensitive_query_key(key):
        return "<REDACTED>"
    if isinstance(value, dict):
        return {str(k): sanitize_metadata(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_metadata(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_metadata(item) for item in value)
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def _default_resolver(hostname: str) -> Iterable[str]:
    try:
        answers = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeUrlError(f"URL hostname could not be resolved safely: {exc}") from exc
    return [answer[4][0] for answer in answers]


def _nested_url_has_credentials(value: str, depth: int = 0) -> bool:
    if not value.lower().startswith(("http://", "https://")):
        return False
    if depth >= 3:
        return True
    try:
        nested = urlsplit(value)
    except ValueError:
        return True
    return bool(
        nested.username
        or nested.password
        or any(
            is_sensitive_query_key(key) or _nested_url_has_credentials(nested_value, depth + 1)
            for key, nested_value in parse_qsl(nested.query)
        )
    )


def _blocked_hostname(hostname: str) -> bool:
    return (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname == "metadata"
        or hostname == "metadata.google.internal"
        or hostname.endswith(".metadata.google.internal")
        or hostname.endswith(".local")
    )


def _normalize_hostname(hostname: str) -> str:
    return hostname.strip().lower().strip("[]").rstrip(".")


def _format_host(hostname: str, port: int | None) -> str:
    rendered = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    return f"{rendered}:{port}" if port else rendered
