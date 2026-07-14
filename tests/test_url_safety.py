from urllib.parse import quote

import pytest

from harness.url_safety import (
    UnsafeUrlError,
    assert_safe_public_url,
    canonicalize_url,
    redact_sensitive_text,
    resolve_public_addresses,
    sanitize_url_for_persistence,
)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/x",
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x",
        "https://user:pass@example.com/x",
        "https://example.com/x?api_key=secret",
        "https://example.com/?next=https%3A%2F%2Fu%3Ap%40foo.test%2Fx%3Ftoken%3Dsecret",
        "https://example.com:0/x",
        "https://example.com/x?token%5B%5D=secret",
        "https://example.com/x?api.key=secret",
        "https://example.com/x?token%00=secret",
    ],
)
def test_rejects_unsafe_urls(url):
    with pytest.raises(UnsafeUrlError):
        assert_safe_public_url(url)


def test_mixed_public_private_dns_fails_closed():
    with pytest.raises(UnsafeUrlError, match="non-public"):
        resolve_public_addresses("example.com", lambda _: ["93.184.216.34", "10.0.0.1"])


def test_canonicalization_is_stable_and_strips_tracking():
    assert (
        canonicalize_url("HTTPS://Example.COM:443/a?z=2&utm_source=x&a=1#fragment")
        == "https://example.com/a?a=1&z=2"
    )


def test_sanitization_and_error_redaction_remove_secrets():
    url = "https://user:pass@example.com/a?token=secret&ok=yes#fragment"
    safe = sanitize_url_for_persistence(url)
    message = redact_sensitive_text(f"failed {url} Authorization: Bearer abc.def token=xyz")
    assert safe == "https://example.com/a?ok=yes"
    assert "secret" not in message
    assert "user:pass" not in message
    assert "abc.def" not in message
    assert "xyz" not in message
    assert sanitize_url_for_persistence("ftp://user:pass@example.com/a") == "ftp://example.com/a"


def test_nested_secret_url_parameter_is_removed_from_persistence():
    raw = "https://example.com/?next=https%3A%2F%2Fu%3Ap%40foo.test%2Fx%3Ftoken%3Dsecret&ok=yes"
    assert sanitize_url_for_persistence(raw) == "https://example.com/?ok=yes"


def test_deeply_nested_secret_url_is_rejected_and_removed():
    inner = "https://u:p@foo.test/x?token=secret"
    middle = f"https://middle.test/?next={quote(inner, safe='')}"
    outer = f"https://example.com/?next={quote(middle, safe='')}&ok=yes"
    with pytest.raises(UnsafeUrlError):
        assert_safe_public_url(outer)
    assert sanitize_url_for_persistence(outer) == "https://example.com/?ok=yes"
