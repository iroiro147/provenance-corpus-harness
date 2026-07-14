"""Explicit-consent acquisition for direct, operator-authorized media URLs."""

from __future__ import annotations

from dataclasses import dataclass

from .assets import AssetStore, StoredAsset
from .rights import SourcePolicy
from .transport import SafeHttpTransport
from .url_safety import sanitize_url_for_persistence


@dataclass(frozen=True)
class MediaPolicy:
    download_media: bool = False
    max_asset_bytes: int = 25 * 1024 * 1024
    allowed_media_types: tuple[str, ...] = (
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/svg+xml",
        "audio/mpeg",
        "audio/mp4",
        "audio/wav",
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "application/pdf",
    )

    def __post_init__(self) -> None:
        if self.max_asset_bytes <= 0:
            raise ValueError("max_asset_bytes must be positive")
        if not self.allowed_media_types:
            raise ValueError("allowed_media_types must not be empty")


@dataclass(frozen=True)
class MediaAcquisitionResult:
    status: str
    source_url: str
    asset: StoredAsset | None = None


def acquire_direct_media(
    url: str,
    *,
    policy: MediaPolicy,
    source_policy: SourcePolicy,
    asset_store: AssetStore,
    transport: SafeHttpTransport | None = None,
    timeout: float = 20.0,
) -> MediaAcquisitionResult:
    """Acquire one direct media response only after explicit download consent.

    Returned bytes stay inert in the content-addressed store. This function does
    not invoke a browser, decoder, player, archive extractor, or account session.
    """

    safe_url = sanitize_url_for_persistence(url)
    if not policy.download_media:
        return MediaAcquisitionResult("not_downloaded", safe_url)
    client = transport or SafeHttpTransport()
    response = client.get(url, timeout=timeout, max_bytes=policy.max_asset_bytes)
    response.raise_for_status()
    media_type = response.media_type
    if media_type not in policy.allowed_media_types:
        raise ValueError(f"media type is not allowed: {media_type or '<missing>'}")
    asset = asset_store.put(
        response.body,
        source_url=response.url,
        media_type=media_type,
        source_policy=source_policy,
        role="direct-media",
    )
    return MediaAcquisitionResult("downloaded", safe_url, asset)
