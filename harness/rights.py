"""Explicit rights and authorization declarations for non-text corpus assets.

These declarations record an operator's decision. They are provenance evidence,
not a legal opinion or proof that a particular use is permitted.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Literal, Mapping, cast
from urllib.parse import urlsplit

from .url_safety import inspect_url_credentials, redact_sensitive_text

AccessClass = Literal["public", "owned", "licensed", "paid-personal", "private", "unknown"]
AuthorizationBasis = Literal[
    "public",
    "account-owned-export",
    "operator-approved-export",
    "official-api",
    "licensed-dataset",
]

_ACCESS_CLASSES = {"public", "owned", "licensed", "paid-personal", "private", "unknown"}
_AUTHORIZATION_BASES = {
    "public",
    "account-owned-export",
    "operator-approved-export",
    "official-api",
    "licensed-dataset",
}
_LOCAL_ONLY_CLASSES = {"paid-personal", "private", "unknown"}
_CREDENTIAL_VALUE = re.compile(
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+|"
    r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
_CREDENTIAL_HEADER = re.compile(
    r"\b(?:Authorization|Cookie|Set-Cookie|Proxy-Authorization)\s*:", re.I
)


@dataclass(frozen=True)
class RightsDeclaration:
    status: AccessClass
    permitted_uses: tuple[str, ...] = ("local-research",)
    notes: str = ""

    def __post_init__(self) -> None:
        if self.status not in _ACCESS_CLASSES:
            raise ValueError(f"unsupported rights status: {self.status}")
        uses = tuple(use.strip() for use in self.permitted_uses)
        if not uses or any(not use or len(use) > 100 for use in uses):
            raise ValueError("permitted_uses must contain short, non-empty values")
        if len(set(uses)) != len(uses):
            raise ValueError("permitted_uses must not contain duplicates")
        if len(self.notes) > 2000:
            raise ValueError("rights notes must not exceed 2000 characters")
        for index, use in enumerate(uses):
            _assert_policy_text_safe(use, f"permitted_uses[{index}]")
        _assert_policy_text_safe(self.notes, "rights notes")
        object.__setattr__(self, "permitted_uses", uses)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RightsDeclaration:
        raw_uses = value.get("permitted_uses", ("local-research",))
        if not isinstance(raw_uses, (list, tuple)) or not all(
            isinstance(use, str) for use in raw_uses
        ):
            raise ValueError("rights permitted_uses must be an array of strings")
        status = value.get("status")
        if not isinstance(status, str):
            raise ValueError("rights status is required")
        notes = value.get("notes", "")
        if not isinstance(notes, str):
            raise ValueError("rights notes must be a string")
        return cls(cast(AccessClass, status), tuple(raw_uses), notes)


@dataclass(frozen=True)
class AuthorizationDeclaration:
    basis: AuthorizationBasis
    approval_id: str = ""
    account_owner: str = ""

    def __post_init__(self) -> None:
        if self.basis not in _AUTHORIZATION_BASES:
            raise ValueError(f"unsupported authorization basis: {self.basis}")
        if self.basis == "operator-approved-export" and not self.approval_id.strip():
            raise ValueError("operator-approved-export requires approval_id")
        if self.basis == "account-owned-export" and not self.account_owner.strip():
            raise ValueError("account-owned-export requires account_owner")
        if len(self.approval_id) > 200 or len(self.account_owner) > 200:
            raise ValueError("authorization identifiers must not exceed 200 characters")
        _assert_policy_text_safe(self.approval_id, "approval_id")
        _assert_policy_text_safe(self.account_owner, "account_owner")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AuthorizationDeclaration:
        basis = value.get("basis")
        if not isinstance(basis, str):
            raise ValueError("authorization basis is required")
        approval_id = value.get("approval_id", "")
        account_owner = value.get("account_owner", "")
        if not isinstance(approval_id, str) or not isinstance(account_owner, str):
            raise ValueError("authorization identifiers must be strings")
        return cls(cast(AuthorizationBasis, basis), approval_id, account_owner)


@dataclass(frozen=True)
class SourcePolicy:
    rights: RightsDeclaration
    authorization: AuthorizationDeclaration
    access_class: AccessClass
    local_only: bool = True

    def __post_init__(self) -> None:
        if self.access_class not in _ACCESS_CLASSES:
            raise ValueError(f"unsupported access class: {self.access_class}")
        if self.access_class in _LOCAL_ONLY_CLASSES and not self.local_only:
            raise ValueError(f"{self.access_class} sources must remain local_only")
        if self.rights.status != self.access_class:
            raise ValueError("rights status must match access_class")
        if self.authorization.basis == "public" and self.access_class != "public":
            raise ValueError("public authorization requires public access_class")
        if self.authorization.basis == "licensed-dataset" and self.rights.status != "licensed":
            raise ValueError("licensed-dataset authorization requires licensed rights")
        if self.rights.status == "paid-personal" and self.access_class != "paid-personal":
            raise ValueError("paid-personal rights require paid-personal access_class")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SourcePolicy:
        rights = value.get("rights")
        authorization = value.get("authorization")
        access_class = value.get("access_class")
        local_only = value.get("local_only", True)
        if not isinstance(rights, Mapping) or not isinstance(authorization, Mapping):
            raise ValueError("source policy requires rights and authorization objects")
        if not isinstance(access_class, str) or not isinstance(local_only, bool):
            raise ValueError("source policy access_class/local_only are invalid")
        return cls(
            rights=RightsDeclaration.from_mapping(rights),
            authorization=AuthorizationDeclaration.from_mapping(authorization),
            access_class=cast(AccessClass, access_class),
            local_only=local_only,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "rights": asdict(self.rights),
            "authorization": asdict(self.authorization),
            "access_class": self.access_class,
            "local_only": self.local_only,
        }


def _assert_policy_text_safe(value: str, label: str) -> None:
    if not value:
        return
    if (
        _CREDENTIAL_VALUE.search(value)
        or _CREDENTIAL_HEADER.search(value)
        or redact_sensitive_text(value) != value
    ):
        raise ValueError(f"{label} contains credential-like material")
    if value.lower().startswith(("http://", "https://")):
        parsed = urlsplit(value)
        inspection = inspect_url_credentials(value)
        if (
            parsed.username
            or parsed.password
            or inspection.has_userinfo
            or inspection.sensitive_query_keys
        ):
            raise ValueError(f"{label} contains a credential-bearing URL")
