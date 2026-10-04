"""Unit tests for the Android verifier driver's site-isolation helpers."""

from __future__ import annotations

import json

from tools.android_verify import (
    jar_download_urls,
    load_config_document,
    merge_isolated_reports,
    mirror_document_jars,
    spider_site_keys,
    summarize,
    unwrap_proxy_url,
)


def test_spider_site_keys_match_android_rules_and_keep_order():
    config = {
        "sites": [
            {"key": "alpha", "api": "csp_Alpha"},
            {"key": "cms", "api": "https://cms.example/api.php/provide/vod"},
            {"key": "beta", "api": "Csp_Beta"},
            {"key": "bare", "api": "AppRJ"},
            {"key": "qualified", "api": "com.example.Spider"},
            {"key": "alpha", "api": "csp_AlphaTwo"},
            {"api": "csp_NoKey"},
        ]
    }
    assert spider_site_keys(config) == ["alpha", "beta", "bare", ""]


def test_spider_site_keys_empty_config_is_empty():
    assert spider_site_keys({}) == []
    assert spider_site_keys({"sites": []}) == []
    assert spider_site_keys({"sites": [{"key": "cms", "api": "https://x/api"}]}) == []


def test_merge_isolated_reports_contains_crash_and_keeps_real_playable():
    good = {
        "sites": [
            {
                "key": "good",
                "loadOk": True,
                "playOk": True,
                "ok": True,
                "stage": "done",
            }
        ]
    }
    report = merge_isolated_reports(
        "http://127.0.0.1:1/cfg.json",
        "https://example.com/cfg.json",
        ["good", "bad", "silent"],
        [
            good,
            RuntimeError("SIGABRT"),
            {"stage": "NO_ITEM", "error": "NO_ITEM", "sites": []},
        ],
    )
    assert report["ok"] is True
    assert report["siteCount"] == 3
    assert summarize(report) == {
        "configUrl": "http://127.0.0.1:1/cfg.json",
        "siteCount": 3,
        "loadableCount": 1,
        "playableCount": 1,
        "playableKeys": ["good"],
    }
    by_key = {site["key"]: site for site in report["sites"]}
    assert by_key["bad"]["stage"] == "instrumentation_crash"
    assert by_key["bad"]["playOk"] is False
    assert by_key["silent"]["stage"] == "NO_ITEM"


def test_merge_isolated_reports_never_fakes_playable_for_a_crash():
    report = merge_isolated_reports(
        "http://127.0.0.1:1/cfg.json",
        "https://example.com/cfg.json",
        ["bad"],
        [RuntimeError("SIGABRT")],
    )
    assert report["ok"] is False
    assert summarize(report)["playableCount"] == 0
    assert summarize(report)["loadableCount"] == 0
    assert report["sites"][0]["stage"] == "instrumentation_crash"


def test_load_config_document_reads_local_file(tmp_path):
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps({"sites": [{"key": "a", "api": "csp_A"}]}), encoding="utf-8")
    document, error = load_config_document(
        {"local_path": path},
        connect_timeout_ms=1000,
        read_timeout_ms=1000,
    )
    assert error == ""
    assert document == {"sites": [{"key": "a", "api": "csp_A"}]}


def test_unwrap_proxy_url_finds_the_original_origin():
    original = "https://raw.githubusercontent.com/owner/repo/main/pg.jar"
    assert unwrap_proxy_url(f"https://ghfast.top/{original}") == original
    assert unwrap_proxy_url(original) == original
    assert unwrap_proxy_url("https://cdn.example/x.jar") == "https://cdn.example/x.jar"


def test_jar_download_urls_puts_origin_first_and_keeps_all_fallbacks():
    value = "https://gh-proxy.com/https://raw.githubusercontent.com/o/r/main/a.jar;md5;abc"
    urls = jar_download_urls(value)
    assert urls[0] == "https://raw.githubusercontent.com/o/r/main/a.jar"
    assert "https://ghfast.top/https://raw.githubusercontent.com/o/r/main/a.jar" in urls
    assert len(urls) == len(set(urls))


def test_jar_download_urls_does_not_proxy_non_github_hosts():
    value = "https://gitee.com/owner/repo/raw/master/jar/a.jar"
    assert jar_download_urls(value) == [value]


def test_mirror_document_jars_rewrites_urls_and_reuses_downloaded_bytes(tmp_path):
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return b"jar-bytes"

    document = {
        "spider": "https://raw.githubusercontent.com/o/r/main/pg.jar;md5;abc",
        "sites": [
            {"key": "a", "api": "csp_A", "jar": "https://raw.githubusercontent.com/o/r/main/pg.jar"},
            {"key": "b", "api": "csp_B", "jar": "csp_B"},
            {"key": "c", "api": "csp_C", "jar": "./extra.jar"},
        ],
    }
    mirrored, errors = mirror_document_jars(
        document,
        fetch=fetch,
        url_for=lambda path: f"http://127.0.0.1:9/{path.name}",
        jars_dir=tmp_path / "jars",
        base_url="https://raw.githubusercontent.com/o/r/main/cfg.json",
    )
    assert errors == []
    assert mirrored["spider"].startswith("http://127.0.0.1:9/")
    assert mirrored["spider"].endswith(";md5;abc")
    assert mirrored["sites"][0]["jar"].startswith("http://127.0.0.1:9/")
    assert mirrored["sites"][1]["jar"] == "csp_B"
    # Relative jars resolve against the config URL.
    assert mirrored["sites"][2]["jar"].startswith("http://127.0.0.1:9/")
    assert len(list((tmp_path / "jars").glob("*.jar"))) == 2
    assert len(calls) == 2
    # The original document must not be mutated.
    assert document["spider"].startswith("https://")


def test_mirror_document_jars_keeps_remote_url_when_download_fails(tmp_path):
    remote = "https://raw.githubusercontent.com/o/r/main/missing.jar"

    def fetch(url: str) -> bytes:
        raise RuntimeError("connect timeout")

    mirrored, errors = mirror_document_jars(
        {"spider": remote},
        fetch=fetch,
        url_for=lambda path: f"http://127.0.0.1:9/{path.name}",
        jars_dir=tmp_path / "jars",
    )
    assert mirrored["spider"] == remote
    assert len(errors) == 1
    assert errors[0]["url"] == remote
