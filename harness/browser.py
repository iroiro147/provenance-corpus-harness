"""Safe browser-render seam with transport-fulfilled network requests.

The browser never receives permission to perform direct network requests.  A
driver must route every HTTP(S) GET through :class:`BrowserNetworkGateway`,
which delegates to the DNS-pinned protected transport.  State-changing methods,
credentials, service workers, WebSockets, downloads, and persistent profiles
are outside this public rendering contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .constants import USER_AGENT
from .transport import HttpResponse, SafeHttpTransport
from .url_safety import (
    assert_safe_public_url,
    redact_sensitive_text,
    sanitize_url_for_persistence,
)

_REQUEST_HEADER_ALLOWLIST = {"accept", "accept-language"}
_RESPONSE_HEADER_ALLOWLIST = {
    "access-control-allow-origin",
    "cache-control",
    "content-language",
    "content-security-policy",
    "content-type",
    "etag",
    "last-modified",
}


class BrowserRequestDenied(RuntimeError):
    """A browser request fell outside the read-only render contract."""


class BrowserBudgetExceeded(BrowserRequestDenied):
    """The browser exceeded its network request or byte budget."""


class BrowserRenderFailure(RuntimeError):
    """A render failed after consuming a known amount of gateway traffic."""

    def __init__(self, *, request_count: int, network_bytes: int) -> None:
        self.request_count = request_count
        self.network_bytes = network_bytes
        super().__init__(
            "browser render failed "
            f"after {request_count} gateway requests and {network_bytes} network bytes"
        )


@dataclass(frozen=True)
class BrowserPolicy:
    timeout_seconds: float = 30.0
    ready_selector: str | None = None
    idle_wait_ms: int = 250
    viewport_width: int = 1440
    viewport_height: int = 900
    locale: str = "en-US"
    timezone_id: str = "UTC"
    capture_screenshot: bool = False
    max_requests: int = 100
    max_resource_bytes: int = 5 * 1024 * 1024
    max_total_bytes: int = 25 * 1024 * 1024
    max_redirects: int = 0
    allowed_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.idle_wait_ms < 0:
            raise ValueError("idle_wait_ms must not be negative")
        if not 1 <= self.viewport_width <= 10_000 or not 1 <= self.viewport_height <= 10_000:
            raise ValueError("browser viewport dimensions must be between 1 and 10000")
        if self.max_requests <= 0:
            raise ValueError("max_requests must be positive")
        if self.max_resource_bytes <= 0 or self.max_total_bytes <= 0:
            raise ValueError("browser byte budgets must be positive")
        if self.max_resource_bytes > self.max_total_bytes:
            raise ValueError("max_resource_bytes must not exceed max_total_bytes")
        if self.max_redirects != 0:
            raise ValueError(
                "browser redirects require an every-hop origin validator; use zero for now"
            )


@dataclass(frozen=True)
class BrowserRequest:
    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    resource_type: str = "document"


@dataclass(frozen=True)
class BrowserResponse:
    requested_url: str
    final_url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class RenderedPage:
    requested_url: str
    final_url: str
    title: str
    html: str
    status_code: int
    content_type: str
    screenshot: bytes | None
    request_count: int
    network_bytes: int
    source_profile: str = "browser"
    source_primitive: str = "playwright"


@dataclass(frozen=True)
class BrowserReceiptVerification:
    ok: bool
    checked: int
    errors: tuple[str, ...]


class BrowserDriver(Protocol):
    """Injected browser adapter.

    Implementations must fulfill every HTTP(S) request with ``gateway.fetch``;
    they must not use a direct-continue operation.  They must also disable
    service workers, WebSockets, downloads, persistent browser state, pop-up
    escape, and non-GET methods.
    """

    primitive: str

    def render(
        self,
        url: str,
        *,
        gateway: BrowserNetworkGateway,
        policy: BrowserPolicy,
    ) -> RenderedPage: ...


class BrowserNetworkGateway:
    """The only permitted browser-to-network seam."""

    def __init__(
        self,
        seed_url: str,
        *,
        policy: BrowserPolicy | None = None,
        transport: SafeHttpTransport | None = None,
    ) -> None:
        self.policy = policy or BrowserPolicy()
        self.transport = transport or SafeHttpTransport()
        self.seed_url = assert_safe_public_url(seed_url)
        self._allowed_origins = {_origin(self.seed_url)}
        self._allowed_origins.update(
            _normalize_origin(item) for item in self.policy.allowed_origins
        )
        self.request_count = 0
        self.network_bytes = 0

    def fetch(self, request: BrowserRequest) -> BrowserResponse:
        if request.method.upper() != "GET":
            raise BrowserRequestDenied("browser network is read-only; only GET is allowed")
        safe_url = assert_safe_public_url(request.url)
        if _origin(safe_url) not in self._allowed_origins:
            raise BrowserRequestDenied("browser request origin is not explicitly allowed")
        if self.request_count >= self.policy.max_requests:
            raise BrowserBudgetExceeded("browser request budget exceeded")
        remaining = self.policy.max_total_bytes - self.network_bytes
        if remaining <= 0:
            raise BrowserBudgetExceeded("browser total byte budget exceeded")
        self.request_count += 1
        response = self.transport.get(
            safe_url,
            headers={
                str(key): str(value)
                for key, value in request.headers.items()
                if str(key).lower() in _REQUEST_HEADER_ALLOWLIST
            },
            timeout=self.policy.timeout_seconds,
            max_bytes=min(self.policy.max_resource_bytes, remaining),
            max_redirects=self.policy.max_redirects,
        )
        final_url = assert_safe_public_url(response.url)
        if _origin(final_url) not in self._allowed_origins:
            raise BrowserRequestDenied("browser redirect origin is not explicitly allowed")
        self.network_bytes += len(response.body)
        if self.network_bytes > self.policy.max_total_bytes:
            raise BrowserBudgetExceeded("browser total byte budget exceeded")
        return BrowserResponse(
            requested_url=sanitize_url_for_persistence(safe_url),
            final_url=sanitize_url_for_persistence(final_url),
            status_code=response.status_code,
            headers=_safe_response_headers(response),
            body=response.body,
        )


def render_page(
    url: str,
    *,
    driver: BrowserDriver,
    policy: BrowserPolicy | None = None,
    transport: SafeHttpTransport | None = None,
) -> RenderedPage:
    """Render one public page through an injected no-direct-egress driver."""

    resolved_policy = policy or BrowserPolicy()
    gateway = BrowserNetworkGateway(url, policy=resolved_policy, transport=transport)
    try:
        page = driver.render(gateway.seed_url, gateway=gateway, policy=resolved_policy)
    except Exception as exc:
        raise BrowserRenderFailure(
            request_count=gateway.request_count,
            network_bytes=gateway.network_bytes,
        ) from exc
    final_url = _validated_rendered_url(page.final_url, gateway)
    return RenderedPage(
        requested_url=sanitize_url_for_persistence(gateway.seed_url),
        final_url=sanitize_url_for_persistence(final_url),
        title=page.title,
        html=page.html,
        status_code=page.status_code,
        content_type=page.content_type,
        screenshot=page.screenshot,
        request_count=gateway.request_count,
        network_bytes=gateway.network_bytes,
        source_profile="browser",
        source_primitive=str(driver.primitive),
    )


def write_browser_receipt(
    page: RenderedPage,
    record_path: str | Path | None,
    out_dir: str | Path,
    *,
    outcome: str,
    asset_ids: tuple[str, ...] = (),
    created_at: str | None = None,
) -> Path:
    """Write one immutable browser-run receipt with confined record evidence."""

    if outcome not in {"written", "duplicate", "empty"}:
        raise ValueError("browser outcome must be written, duplicate, or empty")
    root = Path(out_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    provenance = root / "_provenance"
    runs = provenance / "browser-runs"
    for directory in (provenance, runs):
        if directory.is_symlink():
            raise ValueError("browser receipt directory must not be a symlink")
        directory.mkdir(exist_ok=True)
        if not directory.resolve().is_relative_to(root):
            raise ValueError("browser receipt directory escapes the output root")
    timestamp, filename_timestamp = _browser_receipt_timestamp(
        created_at or datetime.now(timezone.utc).isoformat()
    )
    relative = _browser_relative_record_path(record_path, root)
    if outcome in {"written", "duplicate"} and relative is None:
        raise ValueError("written and duplicate browser outcomes require a record path")
    if outcome == "empty" and relative is not None:
        raise ValueError("empty browser outcomes must not include a record path")
    normalized_assets = tuple(asset_ids)
    if len(normalized_assets) != len(set(normalized_assets)) or any(
        re.fullmatch(r"asset-[a-f0-9]{64}", item) is None for item in normalized_assets
    ):
        raise ValueError("browser asset_ids must be unique content-addressed asset IDs")
    run_id = uuid.uuid4().hex[:16]
    payload = {
        "contract": "provenance-browser-run.v1",
        "run_id": run_id,
        "created_at": timestamp,
        "outcome": outcome,
        "requested_url": sanitize_url_for_persistence(page.requested_url),
        "final_url": sanitize_url_for_persistence(page.final_url),
        "title": str(page.title),
        "status_code": page.status_code,
        "content_type": str(page.content_type),
        "request_count": page.request_count,
        "network_bytes": page.network_bytes,
        "source_profile": str(page.source_profile),
        "source_primitive": str(page.source_primitive),
        "record_path": relative,
        "record_hash": _browser_record_hash(relative, root),
        "asset_ids": list(normalized_assets),
    }
    destination = runs / f"{filename_timestamp}-{run_id}.json"
    if destination.resolve().parent != runs.resolve():
        raise ValueError("browser receipt destination escapes its run directory")
    validation_errors = _validate_browser_receipt_payload(payload, destination)
    if validation_errors:
        raise ValueError(f"browser receipt data is invalid: {validation_errors[0]}")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=runs, prefix=".browser-receipt-", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def verify_browser_receipt(
    receipt_path: str | Path,
    out_dir: str | Path,
    *,
    max_receipt_bytes: int = 1024 * 1024,
) -> BrowserReceiptVerification:
    """Strictly reconcile one browser receipt with its record and asset manifest."""

    errors: list[str] = []
    checked = 0
    try:
        if max_receipt_bytes <= 0:
            raise ValueError("max_receipt_bytes must be positive")
        root = Path(out_dir).expanduser().resolve()
        runs = (root / "_provenance" / "browser-runs").resolve()
        receipt = Path(receipt_path).expanduser()
        resolved = receipt.resolve()
        if receipt.is_symlink() or not resolved.is_relative_to(runs):
            raise ValueError("browser receipt path escapes its run directory")
        if not resolved.is_file():
            raise ValueError("browser receipt must be a regular file")
        if resolved.stat().st_size > max_receipt_bytes:
            raise ValueError("browser receipt exceeds the verification byte limit")
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        errors.extend(_validate_browser_receipt_payload(payload, resolved))
        if isinstance(payload, dict) and isinstance(payload.get("record_path"), str):
            try:
                record = _browser_record_path(payload["record_path"], root)
                actual = hashlib.sha256(record.read_bytes()).hexdigest()
                checked += 1
                if actual != payload.get("record_hash"):
                    errors.append("browser record hash mismatch")
            except (OSError, ValueError) as exc:
                errors.append(redact_sensitive_text(str(exc))[:300])
        asset_ids = payload.get("asset_ids", []) if isinstance(payload, dict) else []
        if (
            isinstance(asset_ids, list)
            and asset_ids
            and all(isinstance(item, str) for item in asset_ids)
        ):
            manifest = root / "assets.json"
            if manifest.is_symlink() or not manifest.is_file():
                errors.append("browser asset_ids require a regular assets.json manifest")
            else:
                manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
                rows = (
                    manifest_payload.get("assets", [])
                    if isinstance(manifest_payload, dict)
                    and manifest_payload.get("contract") == "provenance-assets.v1"
                    else []
                )
                known = {
                    row.get("asset_id")
                    for row in rows
                    if isinstance(row, dict) and isinstance(row.get("asset_id"), str)
                }
                missing = [item for item in asset_ids if item not in known]
                if missing:
                    errors.append("browser receipt references unknown asset_ids")
                checked += len(asset_ids) - len(missing)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(redact_sensitive_text(str(exc))[:300])
    return BrowserReceiptVerification(not errors, checked, tuple(errors))


def _validate_browser_receipt_payload(payload: object, receipt: Path) -> list[str]:
    if not isinstance(payload, dict):
        return ["browser receipt must be a JSON object"]
    errors: list[str] = []
    expected = {
        "contract",
        "run_id",
        "created_at",
        "outcome",
        "requested_url",
        "final_url",
        "title",
        "status_code",
        "content_type",
        "request_count",
        "network_bytes",
        "source_profile",
        "source_primitive",
        "record_path",
        "record_hash",
        "asset_ids",
    }
    if set(payload) != expected:
        errors.append("browser receipt fields do not match the v1 contract")
    if payload.get("contract") != "provenance-browser-run.v1":
        errors.append("browser receipt contract is missing or unsupported")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or re.fullmatch(r"[a-f0-9]{16}", run_id) is None:
        errors.append("browser receipt run_id is invalid")
    elif not receipt.name.endswith(f"-{run_id}.json"):
        errors.append("browser receipt filename does not match run_id")
    created_at = payload.get("created_at")
    if not isinstance(created_at, str):
        errors.append("browser receipt created_at must be a string")
    else:
        try:
            _browser_receipt_timestamp(created_at)
        except ValueError as exc:
            errors.append(str(exc))
    for key in ("requested_url", "final_url"):
        value = payload.get(key)
        if not isinstance(value, str):
            errors.append(f"browser receipt {key} must be a string")
            continue
        try:
            if sanitize_url_for_persistence(assert_safe_public_url(value)) != value:
                errors.append(f"browser receipt {key} is not sanitized")
        except ValueError:
            errors.append(f"browser receipt {key} is invalid")
    for key in ("title", "content_type", "source_primitive"):
        if not isinstance(payload.get(key), str):
            errors.append(f"browser receipt {key} must be a string")
    if payload.get("source_primitive") == "":
        errors.append("browser receipt source_primitive must not be empty")
    if payload.get("source_profile") != "browser":
        errors.append("browser receipt source_profile must be browser")
    for key in ("status_code", "request_count", "network_bytes"):
        if not _browser_nonnegative_int(payload.get(key)):
            errors.append(f"browser receipt {key} must be a non-negative integer")
    if (
        _browser_nonnegative_int(payload.get("status_code"))
        and not 100 <= payload["status_code"] <= 599
    ):
        errors.append("browser receipt status_code is outside the HTTP range")
    if payload.get("request_count") == 0:
        errors.append("browser receipt request_count must be positive")
    outcome = payload.get("outcome")
    if outcome not in {"written", "duplicate", "empty"}:
        errors.append("browser receipt outcome is invalid")
    relative = payload.get("record_path")
    digest = payload.get("record_hash")
    if relative is None:
        if outcome != "empty" or digest is not None:
            errors.append("browser receipt record evidence does not match outcome")
    elif (
        not isinstance(relative, str)
        or not isinstance(digest, str)
        or re.fullmatch(r"[a-f0-9]{64}", digest) is None
    ):
        errors.append("browser receipt record evidence is invalid")
    elif outcome not in {"written", "duplicate"}:
        errors.append("browser receipt record path requires a written or duplicate outcome")
    asset_ids = payload.get("asset_ids")
    if (
        not isinstance(asset_ids, list)
        or any(
            not isinstance(item, str) or re.fullmatch(r"asset-[a-f0-9]{64}", item) is None
            for item in asset_ids
        )
        or len(asset_ids) != len(set(asset_ids))
    ):
        errors.append("browser receipt asset_ids are invalid")
    return errors


def _browser_receipt_timestamp(value: str) -> tuple[str, str]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("created_at must include a timezone")
    return (
        parsed.isoformat(),
        parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"),
    )


def _browser_relative_record_path(path: str | Path | None, root: Path) -> str | None:
    if path is None:
        return None
    candidate_path = Path(path).expanduser()
    candidate = candidate_path.resolve()
    if candidate_path.is_symlink() or not candidate.is_relative_to(root):
        raise ValueError("browser record path escapes the output root")
    if not candidate.is_file():
        raise ValueError("browser record path must be a regular file")
    return candidate.relative_to(root).as_posix()


def _browser_record_path(relative: str, root: Path) -> Path:
    candidate_path = Path(relative)
    if candidate_path.is_absolute() or ".." in candidate_path.parts:
        raise ValueError("browser record path is not a confined relative path")
    unresolved = root / candidate_path
    resolved = unresolved.resolve()
    if unresolved.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("browser record path must name a confined regular file")
    return resolved


def _browser_record_hash(relative: str | None, root: Path) -> str | None:
    if relative is None:
        return None
    return hashlib.sha256(_browser_record_path(relative, root).read_bytes()).hexdigest()


def _browser_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _safe_response_headers(response: HttpResponse) -> dict[str, str]:
    headers = {
        str(key).lower(): str(value)
        for key, value in response.headers.items()
        if str(key).lower() in _RESPONSE_HEADER_ALLOWLIST
    }
    headers.pop("set-cookie", None)
    headers["content-length"] = str(len(response.body))
    return headers


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    port = parsed.port
    default = (parsed.scheme == "http" and port in {None, 80}) or (
        parsed.scheme == "https" and port in {None, 443}
    )
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme.lower()}://{host.lower()}{'' if default else f':{port}'}"


def _normalize_origin(value: str) -> str:
    safe = assert_safe_public_url(value)
    parsed = urlsplit(safe)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("allowed browser origins must not include a path, query, or fragment")
    return _origin(safe)


def _validated_rendered_url(url: str, gateway: BrowserNetworkGateway) -> str:
    try:
        final_url = assert_safe_public_url(url, base_url=gateway.seed_url)
    except (TypeError, ValueError) as exc:
        raise BrowserRequestDenied("rendered page URL is not a safe public URL") from exc
    if _origin(final_url) not in gateway._allowed_origins:
        raise BrowserRequestDenied("rendered page escaped the allowed origins")
    return final_url


class PlaywrightBrowserDriver:
    """Optional synchronous Playwright adapter with transport fulfillment.

    Import is lazy.  The adapter refuses old Playwright builds that cannot
    intercept WebSockets, because silently continuing them would violate the
    no-direct-egress contract.
    """

    primitive = "playwright"

    def render(
        self,
        url: str,
        *,
        gateway: BrowserNetworkGateway,
        policy: BrowserPolicy,
    ) -> RenderedPage:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "browser rendering requires the optional Playwright dependency"
            ) from exc

        final_url = url
        status_code = 200
        content_type = "text/html"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-domain-reliability",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                ],
            )
            try:
                context = browser.new_context(
                    accept_downloads=False,
                    service_workers="block",
                    java_script_enabled=True,
                    user_agent=USER_AGENT,
                    locale=policy.locale,
                    timezone_id=policy.timezone_id,
                    viewport={"width": policy.viewport_width, "height": policy.viewport_height},
                )
                context.add_init_script(
                    """
                    (() => {
                      const blocked = () => { throw new Error("network primitive disabled"); };
                      for (const name of ["WebSocket", "WebTransport", "RTCPeerConnection", "webkitRTCPeerConnection"]) {
                        try { Object.defineProperty(globalThis, name, { value: blocked, configurable: false }); }
                        catch (_) {}
                      }
                      try { Object.defineProperty(globalThis, "open", { value: () => null, configurable: false }); }
                      catch (_) {}
                    })();
                    """
                )
                if not hasattr(context, "route_web_socket"):
                    raise RuntimeError(
                        "installed Playwright cannot block WebSockets; upgrade the browser extra"
                    )
                context.route_web_socket("**/*", lambda websocket: websocket.close())

                route_error: Exception | None = None

                def fulfill(route) -> None:
                    nonlocal final_url, status_code, content_type
                    nonlocal route_error
                    request = route.request
                    try:
                        response = gateway.fetch(
                            BrowserRequest(
                                url=request.url,
                                method=request.method,
                                headers=request.all_headers(),
                                resource_type=request.resource_type,
                            )
                        )
                    except BrowserRequestDenied as exc:
                        if request.is_navigation_request() and route_error is None:
                            route_error = exc
                        route.abort("blockedbyclient")
                        return
                    except Exception as exc:  # noqa: BLE001 - callback must not leak failures
                        if route_error is None:
                            route_error = exc
                        route.abort("failed")
                        return
                    if request.is_navigation_request():
                        final_url = response.final_url
                        status_code = response.status_code
                        content_type = response.headers.get("content-type", "text/html")
                    route.fulfill(
                        status=response.status_code,
                        headers=dict(response.headers),
                        body=response.body,
                    )

                context.route("**/*", fulfill)
                page = context.new_page()
                context.on("page", lambda popup: popup.close() if popup != page else None)
                page.set_default_timeout(policy.timeout_seconds * 1000)
                try:
                    page.goto(url, wait_until="domcontentloaded")
                except Exception:
                    if route_error is not None:
                        raise route_error
                    raise
                if route_error is not None:
                    raise route_error
                if policy.ready_selector:
                    page.wait_for_selector(policy.ready_selector)
                elif policy.idle_wait_ms:
                    page.wait_for_timeout(policy.idle_wait_ms)
                if route_error is not None:
                    raise route_error
                final_url = _validated_rendered_url(page.url, gateway)
                html = page.content()
                title = page.title()
                screenshot = (
                    page.screenshot(full_page=True, animations="disabled")
                    if policy.capture_screenshot
                    else None
                )
                context.close()
            finally:
                browser.close()
        return RenderedPage(
            requested_url=url,
            final_url=final_url,
            title=title,
            html=html,
            status_code=status_code,
            content_type=content_type,
            screenshot=screenshot,
            request_count=gateway.request_count,
            network_bytes=gateway.network_bytes,
            source_primitive=self.primitive,
        )
