"""Fail-closed routing for sources that require an official API or local export."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Iterable, Literal
from urllib.parse import urlsplit

from .url_safety import inspect_url_credentials, sanitize_url_for_persistence

GateAction = Literal["allow_public", "export_required"]
SourceClass = Literal[
    "public-web",
    "credential-bearing-url",
    "account-platform",
    "visual-board",
    "subscriber-content",
    "browser-session",
]


@dataclass(frozen=True)
class SourceRequestPacket:
    packet_id: str
    status: Literal["operator_required"]
    source_url: str
    source_class: SourceClass
    reasons: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    disallowed_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SourceGateDecision:
    action: GateAction
    source_class: SourceClass
    canonical_host: str
    reasons: tuple[str, ...]
    packet: SourceRequestPacket | None = None


def classify_source_gate(url: str, *, markers: Iterable[str] = ()) -> SourceGateDecision:
    """Classify an operator-supplied URL without fetching or authenticating to it."""

    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source URL must be an absolute HTTP(S) URL")
    host = parsed.hostname.lower().rstrip(".")
    normalized_markers = {marker.strip().lower() for marker in markers}
    credentials = inspect_url_credentials(url)
    if credentials.has_userinfo or credentials.sensitive_query_keys:
        return _export_decision(
            url,
            host,
            "credential-bearing-url",
            ("source URL contains credentials or a sensitive query field",),
        )
    if normalized_markers & {"cookie", "session", "login", "persistent-session"}:
        return _export_decision(
            url,
            host,
            "browser-session",
            ("source requires browser or session state",),
        )
    if _host_matches(host, {"x.com", "twitter.com"}):
        return _export_decision(
            url,
            host,
            "account-platform",
            ("use an official API or account-owned export",),
        )
    if _host_matches(host, {"pinterest.com", "pin.it"}):
        return _export_decision(
            url,
            host,
            "visual-board",
            ("use an approved API or operator-owned board export",),
        )
    if normalized_markers & {"paid", "member-only", "subscriber-only", "paywall"}:
        return _export_decision(
            url,
            host,
            "subscriber-content",
            ("subscriber content requires a rights-aware local export",),
        )
    return SourceGateDecision("allow_public", "public-web", host, ("public URL",))


def _export_decision(
    url: str, host: str, source_class: SourceClass, reasons: tuple[str, ...]
) -> SourceGateDecision:
    safe_url = sanitize_url_for_persistence(url)
    packet = SourceRequestPacket(
        packet_id=f"source-request-{hashlib.sha256((safe_url + source_class).encode()).hexdigest()[:12]}",
        status="operator_required",
        source_url=safe_url,
        source_class=source_class,
        reasons=reasons,
        allowed_paths=(
            "Use a platform-provided official API with environment-backed credentials.",
            "Use an account-owned or operator-approved export imported as a local source package.",
            "Record authorization, rights, permitted uses, and local-only constraints.",
        ),
        disallowed_paths=(
            "Do not persist cookies, tokens, authorization headers, or browser profiles.",
            "Do not automate login, replay sessions, evade access controls, or bypass paywalls.",
            "Do not put credential values in URLs, manifests, logs, fixtures, or corpus records.",
        ),
    )
    return SourceGateDecision("export_required", source_class, host, reasons, packet)


def _host_matches(host: str, apexes: set[str]) -> bool:
    return any(host == apex or host.endswith(f".{apex}") for apex in apexes)
