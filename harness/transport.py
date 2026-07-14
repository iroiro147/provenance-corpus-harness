"""SSRF-resistant HTTP transport with DNS pinning and bounded responses."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .constants import USER_AGENT
from .url_safety import Resolver, assert_safe_public_url, resolve_public_addresses

_REDIRECTS = {301, 302, 303, 307, 308}
_SENSITIVE_REDIRECT_HEADERS = {"authorization", "cookie", "proxy-authorization"}
_TRANSPORT_CONTROLLED_HEADERS = {
    "accept-encoding",
    "connection",
    "content-length",
    "host",
    "transfer-encoding",
}


class TransportError(RuntimeError):
    """Base error for safe HTTP transport failures."""


class ResponseTooLargeError(TransportError):
    """Raised before a response can exceed its configured byte budget."""


class HttpStatusError(TransportError):
    """HTTP failure with a machine-readable status code."""

    def __init__(self, url: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"GET {url} returned HTTP {status_code}")


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def header(self, name: str, default: str = "") -> str:
        wanted = name.lower()
        return next(
            (value for key, value in self.headers.items() if key.lower() == wanted), default
        )

    @property
    def media_type(self) -> str:
        return self.header("content-type").partition(";")[0].strip().lower()

    @property
    def text(self) -> str:
        charset = "utf-8"
        content_type = self.header("content-type")
        for part in content_type.split(";")[1:]:
            key, separator, value = part.strip().partition("=")
            if separator and key.lower() == "charset" and value.strip():
                charset = value.strip().strip('"')
                break
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HttpStatusError(self.url, self.status_code)


RequestOnce = Callable[
    [str, str, int, str, Mapping[str, str], float, int],
    HttpResponse,
]


class SafeHttpTransport:
    """Fetch public HTTP(S) resources through a pinned-address seam.

    Every redirect target is revalidated and re-resolved. The selected address
    is passed directly to the socket implementation while TLS still verifies
    the original hostname, closing the preflight-to-connect DNS-rebinding gap.
    """

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        request_once: RequestOnce | None = None,
    ) -> None:
        self._resolver = resolver
        self._request_once = request_once or _request_once
        self._robots_cache: dict[str, RobotFileParser | bool] = {}

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 20.0,
        max_bytes: int = 5 * 1024 * 1024,
        max_redirects: int = 5,
    ) -> HttpResponse:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects must not be negative")

        current = assert_safe_public_url(url)
        request_headers = {
            str(key): str(value)
            for key, value in (headers or {}).items()
            if str(key).lower() not in _TRANSPORT_CONTROLLED_HEADERS
        }
        for hop in range(max_redirects + 1):
            parsed = urlsplit(current)
            hostname = parsed.hostname or ""
            try:
                port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            except ValueError as exc:
                raise TransportError(f"URL contains an invalid port: {current}") from exc
            addresses = resolve_public_addresses(hostname, self._resolver)
            response = self._request_once(
                current,
                hostname,
                port,
                addresses[0],
                request_headers,
                timeout,
                max_bytes,
            )
            if response.status_code not in _REDIRECTS:
                return response

            location = response.header("location")
            if not location:
                raise TransportError(f"redirect response from {current} omitted Location")
            if hop == max_redirects:
                raise TransportError(f"GET exceeded {max_redirects} redirects")
            next_url = assert_safe_public_url(location, base_url=current)
            if _origin(next_url) != _origin(current):
                request_headers = {
                    key: value
                    for key, value in request_headers.items()
                    if key.lower() not in _SENSITIVE_REDIRECT_HEADERS
                }
            current = next_url

        raise AssertionError("redirect loop terminated unexpectedly")  # pragma: no cover

    def robots_allows(
        self, url: str, *, user_agent: str = USER_AGENT, fail_open: bool = True
    ) -> bool:
        """Evaluate robots.txt through the same protected transport as content."""

        parsed = urlsplit(assert_safe_public_url(url))
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._robots_cache.get(origin, "missing")
        if cached != "missing":
            return cached if isinstance(cached, bool) else cached.can_fetch(user_agent, url)
        robots_url = f"{origin}/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            response = self.get(robots_url, max_bytes=512 * 1024)
            if response.status_code in {404, 410}:
                self._robots_cache[origin] = True
                return True
            response.raise_for_status()
            parser.parse(response.text.splitlines())
            self._robots_cache[origin] = parser
            return parser.can_fetch(user_agent, url)
        except (TransportError, ValueError):
            return fail_open


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: float) -> None:
        super().__init__(hostname, port=port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: float) -> None:
        super().__init__(hostname, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


def _request_once(
    url: str,
    hostname: str,
    port: int,
    address: str,
    headers: Mapping[str, str],
    timeout: float,
    max_bytes: int,
) -> HttpResponse:
    parsed = urlsplit(url)
    connection_class = (
        _PinnedHTTPSConnection if parsed.scheme.lower() == "https" else _PinnedHTTPConnection
    )
    connection = connection_class(hostname, address, port, timeout)
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    host_header = _host_header(hostname, port, parsed.scheme.lower())
    merged_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        **headers,
        # Callers cannot override transport-bound safety headers.
        "Accept-Encoding": "identity",
        "Connection": "close",
        "Host": host_header,
    }
    try:
        connection.request("GET", target, headers=merged_headers)
        raw = connection.getresponse()
        response_headers = {key.lower(): value for key, value in raw.getheaders()}
        content_length = response_headers.get("content-length")
        if content_length and raw.status not in _REDIRECTS:
            try:
                if int(content_length) > max_bytes:
                    raise ResponseTooLargeError(
                        f"GET {url} declared {content_length} bytes; limit is {max_bytes}"
                    )
            except ValueError:
                pass
        body = b"" if raw.status in _REDIRECTS else raw.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ResponseTooLargeError(f"GET {url} exceeded {max_bytes} bytes")
        return HttpResponse(
            url=url,
            status_code=raw.status,
            headers=response_headers,
            body=body,
        )
    except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
        raise TransportError(f"GET failed for {url}: {exc}") from exc
    finally:
        connection.close()


def _host_header(hostname: str, port: int, scheme: str) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    return host if default else f"{host}:{port}"


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port
