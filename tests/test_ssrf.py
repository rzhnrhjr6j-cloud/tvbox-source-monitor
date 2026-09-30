"""SSRF guard (spec §15, §38).

The probe must never be usable as a port scanner or a metadata-service reader.
"""

from __future__ import annotations

import pytest

from app.utils.ssrf import UnsafeURLError, guard_url, is_safe, parse_networks


@pytest.mark.parametrize("url,code", [
    ("http://169.254.169.254/latest/meta-data/", "BLOCKED_IP"),   # cloud metadata
    ("http://127.0.0.1:8080/a", "BLOCKED_IP"),                    # loopback
    ("http://[::1]/a", "BLOCKED_IP"),                             # IPv6 loopback
    ("http://10.1.2.3/a", "BLOCKED_IP"),                          # private
    ("http://192.168.1.1/a", "BLOCKED_IP"),                       # private
    ("http://localhost/a", "BLOCKED_HOST"),
    ("http://metadata.google.internal/a", "BLOCKED_HOST"),
])
def test_private_and_metadata_targets_are_refused(url, code):
    with pytest.raises(UnsafeURLError) as excinfo:
        guard_url(url)
    assert str(excinfo.value).startswith(code)


def test_all_private_targets_are_unsafe():
    for url in ("http://169.254.169.254/", "http://127.0.0.1/", "http://10.0.0.1/", "http://localhost/"):
        assert is_safe(url) is False


def test_non_http_schemes_are_refused():
    for url in ("ftp://example.com/a", "file:///etc/passwd", "gopher://example.com/"):
        with pytest.raises(UnsafeURLError) as excinfo:
            guard_url(url)
        assert str(excinfo.value).startswith("BAD_SCHEME")


def test_public_host_without_resolution_is_allowed():
    # resolve=False keeps the test hermetic: no DNS, no network
    assert guard_url("https://example.com/a", resolve=False) == []
    assert guard_url("https://example.com/a", blocked_cidrs=["0.0.0.0/8"], resolve=False) == []


def test_allow_private_is_opt_in_and_returns_addresses():
    assert guard_url("http://127.0.0.1/a", allow_private=True) == ["127.0.0.1"]


def test_allow_private_still_rejects_bad_scheme():
    with pytest.raises(UnsafeURLError):
        guard_url("ftp://127.0.0.1/a", allow_private=True)


def test_blocked_cidrs_come_from_config():
    networks = parse_networks(["203.0.113.0/24", "not-a-cidr"])
    assert len(networks) == 1
    with pytest.raises(UnsafeURLError) as excinfo:
        guard_url("http://203.0.113.7/a", blocked_cidrs=["203.0.113.0/24"])
    assert str(excinfo.value).startswith("BLOCKED_IP")


def test_unsafe_url_error_is_a_value_error():
    assert issubclass(UnsafeURLError, ValueError)
