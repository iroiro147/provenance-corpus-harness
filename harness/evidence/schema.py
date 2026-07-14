"""Versioned, portable records for local evidence indexing and citation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..rights import AuthorizationDeclaration

AccessClass = Literal["public", "owned", "licensed", "paid-personal", "private", "unknown"]
Modality = Literal["text", "document", "image", "audio", "video"]


@dataclass(frozen=True)
class RightsDeclaration:
    """Declared reuse rights; access alone is never treated as a license."""

    status: AccessClass = "unknown"
    permitted_uses: tuple[str, ...] = ("local-research",)
    license: str | None = None
    notes: str | None = None

    @classmethod
    def from_value(cls, value: object, *, access_class: AccessClass = "unknown"):
        if not isinstance(value, dict):
            return cls(status=access_class)
        status = _access_class(value.get("status", access_class))
        uses = value.get("permitted_uses", ["local-research"])
        if not isinstance(uses, (list, tuple)) or not all(isinstance(v, str) for v in uses):
            raise ValueError("rights.permitted_uses must be a list of strings")
        return cls(
            status=status,
            permitted_uses=tuple(uses) or ("local-research",),
            license=_optional_text(value.get("license")),
            notes=_optional_text(value.get("notes")),
        )


@dataclass(frozen=True)
class EvidenceAsset:
    asset_id: str
    source_id: str
    record_path: str
    local_path: str
    sha256: str
    bytes: int
    mime: str
    modality: Modality
    title: str
    safe_source_url: str | None
    rights: RightsDeclaration
    authorization: AuthorizationDeclaration | None = None
    access_class: AccessClass = "unknown"
    local_only: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    tombstoned_at: str | None = None


@dataclass(frozen=True)
class EvidenceUnit:
    evidence_id: str
    asset_id: str
    kind: str
    title: str
    text: str
    locator: dict[str, int] = field(default_factory=dict)
    derivative_path: str | None = None
    content_hash: str = ""


@dataclass(frozen=True)
class VisualDescriptor:
    evidence_id: str
    asset_id: str
    image_path: str
    dhash: str
    color_grid: tuple[int, ...]
    width: int
    height: int


@dataclass(frozen=True)
class EvidenceCitation:
    index_id: str
    evidence_id: str
    asset_id: str
    rank: int
    fused_score: float
    text_score: float | None
    visual_score: float | None
    matched_modalities: tuple[str, ...]
    title: str
    snippet: str
    locator: dict[str, int]
    record_path: str
    original_path: str
    derivative_path: str | None
    safe_source_url: str | None
    rights: RightsDeclaration
    authorization: AuthorizationDeclaration | None
    access_class: AccessClass
    local_only: bool
    source_sha256: str
    evidence_sha256: str


def to_dict(value: object) -> dict[str, Any]:
    return asdict(value)


def _access_class(value: object) -> AccessClass:
    allowed = {"public", "owned", "licensed", "paid-personal", "private", "unknown"}
    if value not in allowed:
        raise ValueError(f"invalid access class: {value}")
    return value  # type: ignore[return-value]


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
