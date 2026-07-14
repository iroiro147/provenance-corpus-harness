import json

import pytest

from harness.source_gates import classify_source_gate


def test_public_web_is_allowed_without_packet() -> None:
    decision = classify_source_gate("https://example.com/article")
    assert decision.action == "allow_public"
    assert decision.packet is None


@pytest.mark.parametrize(
    ("url", "markers", "source_class"),
    [
        ("https://x.com/user", (), "account-platform"),
        ("https://pinterest.com/pin/1", (), "visual-board"),
        ("https://example.com/member", ("paid",), "subscriber-content"),
        ("https://example.com/private", ("cookie",), "browser-session"),
        ("https://example.com/x?access_token=secret", (), "credential-bearing-url"),
    ],
)
def test_gated_sources_require_secret_free_export_packet(
    url: str, markers: tuple[str, ...], source_class: str
) -> None:
    decision = classify_source_gate(url, markers=markers)
    assert decision.action == "export_required"
    assert decision.source_class == source_class
    rendered = json.dumps(decision.packet.to_dict())  # type: ignore[union-attr]
    assert "secret" not in rendered
    assert "access_token" not in rendered
    assert "browser profiles" in rendered


def test_invalid_source_url_is_rejected() -> None:
    with pytest.raises(ValueError):
        classify_source_gate("file:///tmp/export")
