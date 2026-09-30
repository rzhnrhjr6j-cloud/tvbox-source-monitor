"""Dedup / normalisation (spec §7)."""

from __future__ import annotations

import pytest

from app.utils.urls import (
    extract_urls,
    host_of,
    is_http_url,
    looks_like_media,
    normalize_url,
    parse_url,
    redact,
    source_id_for,
)

EQUIVALENT = [
    "http://example.com/a",
    "https://example.com/a",
    "https://example.com/a/",
    "https://EXAMPLE.com/a",
    "https://example.com:443/a",
    "https://example.com//a",
]


@pytest.mark.parametrize("spelling", EQUIVALENT)
def test_scheme_case_slash_and_default_port_collapse(spelling):
    assert normalize_url(spelling) == "https://example.com/a"
    assert source_id_for(spelling) == source_id_for("https://example.com/a")


def test_non_default_port_survives():
    assert normalize_url("http://example.com:8080/a") == "https://example.com:8080/a"


def test_tracking_params_are_dropped_and_query_is_sorted():
    assert normalize_url("https://example.com/a?b=2&a=1&utm_source=x") == "https://example.com/a?a=1&b=2"


def test_github_blob_and_raw_spellings_collapse():
    blob = "https://github.com/owner/repo/blob/main/config/tvbox.json"
    raw = "https://raw.githubusercontent.com/owner/repo/main/config/tvbox.json"
    refs = "https://raw.githubusercontent.com/owner/repo/refs/heads/main/config/tvbox.json"
    keys = {normalize_url(spelling) for spelling in (blob, raw, refs)}
    assert len(keys) == 1
    assert keys.pop() == "https://raw.githubusercontent.com/owner/repo/main/config/tvbox.json"


@pytest.mark.parametrize("garbage", ["", "   ", "not a url", "ftp://example.com/a", "javascript:alert(1)"])
def test_unusable_input_yields_empty_key(garbage):
    assert normalize_url(garbage) == ""
    assert source_id_for(garbage) == ""


def test_ipv6_literal_stays_bracketed():
    parsed = parse_url("http://[2001:db8::1]:8080/a")
    assert parsed is not None
    assert parsed.fetch == "http://[2001:db8::1]:8080/a"
    assert host_of("http://[2001:db8::1]:8080/a") == "2001:db8::1"


def test_schemeless_input_is_promoted_to_https():
    assert parse_url("example.com/a").fetch == "https://example.com/a"


def test_is_http_url_and_host_of():
    assert is_http_url("https://example.com/a")
    assert not is_http_url("not a url")
    assert host_of("https://Sub.Example.com./a") == "sub.example.com"


@pytest.mark.parametrize("url,expected", [
    ("https://e.com/x.m3u8", True),
    ("https://e.com/x.mp4?token=1", True),
    ("https://e.com/x.json", False),
])
def test_looks_like_media(url, expected):
    assert looks_like_media(url) is expected


def test_extract_urls_dedups_after_normalisation():
    text = "see https://example.com/a and https://EXAMPLE.com/a/ plus https://other.com/b."
    assert sorted(extract_urls(text)) == ["https://example.com/a", "https://other.com/b"]


def test_redact_hides_credentials():
    assert redact("https://user:secret@example.com/a") == "https://example.com/a"
    assert redact("https://example.com/a") == "https://example.com/a"
