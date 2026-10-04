"""Unit tests for the Android verifier driver's site-isolation helpers."""

from __future__ import annotations

import json

from tools.android_verify import (
    load_config_document,
    merge_isolated_reports,
    spider_site_keys,
    summarize,
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
