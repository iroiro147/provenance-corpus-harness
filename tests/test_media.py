from pathlib import Path

import pytest

from harness.assets import AssetStore
from harness.media import MediaPolicy, acquire_direct_media
from harness.rights import AuthorizationDeclaration, RightsDeclaration, SourcePolicy
from harness.transport import HttpResponse


class FakeTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls = 0

    def get(self, *_args, **_kwargs) -> HttpResponse:
        self.calls += 1
        return self.response


def public_policy() -> SourcePolicy:
    return SourcePolicy(RightsDeclaration("public"), AuthorizationDeclaration("public"), "public")


def test_media_download_is_off_by_default(tmp_path: Path) -> None:
    transport = FakeTransport(HttpResponse("https://example.com/a.png", 200, {}, b"x"))
    result = acquire_direct_media(
        "https://example.com/a.png",
        policy=MediaPolicy(),
        source_policy=public_policy(),
        asset_store=AssetStore(tmp_path),
        transport=transport,  # type: ignore[arg-type]
    )
    assert result.status == "not_downloaded"
    assert result.asset is None
    assert transport.calls == 0


def test_explicit_media_download_stores_inert_content(tmp_path: Path) -> None:
    transport = FakeTransport(
        HttpResponse("https://cdn.example.com/a.png", 200, {"content-type": "image/png"}, b"png")
    )
    result = acquire_direct_media(
        "https://example.com/a.png",
        policy=MediaPolicy(download_media=True),
        source_policy=public_policy(),
        asset_store=AssetStore(tmp_path),
        transport=transport,  # type: ignore[arg-type]
    )
    assert result.status == "downloaded"
    assert result.asset is not None
    assert (tmp_path / result.asset.relative_path).read_bytes() == b"png"


def test_media_rejects_unapproved_type(tmp_path: Path) -> None:
    transport = FakeTransport(
        HttpResponse("https://example.com/a", 200, {"content-type": "text/html"}, b"html")
    )
    with pytest.raises(ValueError, match="not allowed"):
        acquire_direct_media(
            "https://example.com/a",
            policy=MediaPolicy(download_media=True),
            source_policy=public_policy(),
            asset_store=AssetStore(tmp_path),
            transport=transport,  # type: ignore[arg-type]
        )
