"""Config parsing / schema detection / multi-source expansion (spec §4, §5.1.C, §7)."""

from __future__ import annotations

import json

import pytest

from app.parsers.json_parser import (
    SCHEMA_MULTI,
    SCHEMA_SINGLE,
    SCHEMA_SITES,
    ParseError,
    build_doc,
    detect_schema,
    looks_like_binary,
    looks_like_html,
    parse_text,
    required_fields_present,
)
from app.parsers.multi_source_parser import expand, expand_payload
from app.parsers.normalizer import (
    count_results,
    normalize_sites,
    detail_url_candidates,
    extract_play_urls,
    extract_video_ids,
    has_playlist,
    normalize_site,
    search_url_candidates,
)
from app.utils.urls import source_id_for

SINGLE = {
    "sites": [{"key": "a", "name": "A", "type": 3, "api": "https://a.example.com/api.php/provide/vod/"}],
    "lives": [],
    "parses": [],
}
MULTI = {"urls": [{"name": "A", "url": "https://a.example.com/config.json"}]}


@pytest.mark.parametrize("payload,expected", [
    (MULTI, SCHEMA_MULTI),
    (SINGLE, SCHEMA_SINGLE),
    ({"sites": [{"api": "https://a.example.com/api.php"}]}, SCHEMA_SITES),
    (["https://a.example.com/config.json"], SCHEMA_MULTI),
    ({"nope": 1}, "unknown"),
    ([], "unknown"),
    ("str", "unknown"),
])
def test_detect_schema(payload, expected):
    assert detect_schema(payload) == expected


def test_parse_text_roundtrip():
    doc = parse_text(json.dumps(SINGLE))
    assert doc.schema == SCHEMA_SINGLE
    assert len(doc.sites) == 1
    assert doc.is_multi is False


def test_parse_text_tolerates_a_utf8_bom():
    # 部分配置仓库用带 BOM 的 UTF-8 保存 JSON（如 s14685/tv vip.json），
    # 不能被误判成 INVALID_JSON。
    doc = parse_text("\ufeff" + json.dumps(SINGLE))
    assert doc.schema == SCHEMA_SINGLE
    assert len(doc.sites) == 1


@pytest.mark.parametrize("body,code", [
    ("", "EMPTY_BODY"),
    ("   ", "EMPTY_BODY"),
    ("<!doctype html><html></html>", "HTML_NOT_JSON"),
    ("{not json", "INVALID_JSON"),
    ('{"nope": 1}', "UNKNOWN_SCHEMA"),
    ('{"urls": []}', "MULTI_WITHOUT_URLS"),
    ('{"sites": []}', "SITES_EMPTY"),
])
def test_parse_text_failures_are_typed(body, code):
    with pytest.raises(ParseError) as excinfo:
        parse_text(body)
    assert excinfo.value.code == code


def test_multi_requires_children():
    doc = build_doc(MULTI)
    ok, reason = required_fields_present(doc)
    assert ok and reason == ""


def test_site_without_api_is_not_complete():
    doc = build_doc({"sites": [{"name": "A"}]})
    ok, reason = required_fields_present(doc)
    assert not ok and "api" in reason


def test_html_and_binary_sniffers():
    assert looks_like_html("<html><body>x</body></html>")
    assert not looks_like_html('{"sites": []}')
    assert looks_like_binary(b"\x00\x01\x02binary")
    assert not looks_like_binary(b'{"sites": []}')


# -- expansion -------------------------------------------------------------
def test_expand_produces_child_ids_and_strips_self_reference():
    parent_id = source_id_for("https://parent.example.com/multi.json")
    doc = build_doc({
        "urls": [
            {"name": "A", "url": "https://a.example.com/config.json"},
            {"name": "B", "url": "https://b.example.com/config.json"},
            {"name": "Self", "url": "https://parent.example.com/multi.json"},
            {"name": "Dup", "url": "https://A.example.com/config.json/"},
            {"name": "Bad", "url": "not a url"},
        ]
    })
    children = expand(doc, parent_id)
    assert [child.name for child in children] == ["A", "B"]
    assert all(child.parent_id == parent_id for child in children)
    assert children[0].source_id == source_id_for("https://a.example.com/config.json")


def test_expand_respects_max_children():
    doc = build_doc({"urls": [{"url": f"https://h{index}.example.com/c.json"} for index in range(20)]})
    assert len(expand(doc, "parent", max_children=5)) == 5


def test_expand_payload_never_raises():
    assert expand_payload("{not json", "parent") == []
    assert expand_payload('{"sites": []}', "parent") == []


# -- normalizer ------------------------------------------------------------
def test_normalize_site_accepts_common_spellings():
    site = normalize_site({"name": "A", "api": "https://a.example.com/api.php/provide/vod/", "type": "3"})
    assert site is not None and site.usable
    assert site.api.endswith("/")


def test_normalize_site_without_api_is_not_usable():
    site = normalize_site({"name": "A"})
    assert site is not None
    assert site.usable is False
    assert normalize_sites([{"name": "A"}, {"name": "B", "api": "https://b.example.com/api.php"}]) != []
    assert all(site.usable for site in normalize_sites([{"api": "https://b.example.com/api.php"}]))


def test_search_and_detail_url_candidates_cover_both_dialects():
    search = search_url_candidates("https://a.example.com/api.php/provide/vod/", "keyword")
    assert any("wd=" in url for url in search)
    assert len(search) >= 2
    detail = detail_url_candidates("https://a.example.com/api.php/provide/vod/", "1001")
    assert any("ac=detail" in url or "ids=" in url for url in detail)


def test_extract_video_ids_count_and_playlist():
    payload = {
        "code": 1,
        "list": [
            {"vod_id": 1001, "vod_name": "A", "vod_play_url": "第1集$https://cdn.example.com/1.m3u8"},
            {"vod_id": "1002", "vod_name": "B", "vod_play_url": ""},
        ],
    }
    assert extract_video_ids(payload) == ["1001", "1002"]
    assert count_results(payload) == 2
    assert has_playlist(payload) is True
    assert extract_play_urls(payload, limit=5) == ["https://cdn.example.com/1.m3u8"]


def test_count_results_prefers_observed_items_over_the_claimed_total():
    # a source that claims total=7 but returns no items must NOT be credited
    # with a successful search - observed items win over the claimed count
    assert count_results({"code": 1, "total": 7, "list": []}) == 0
    assert count_results({"code": 0, "list": []}) == 0
    assert count_results({"code": 1, "total": 7, "list": [{"vod_id": 1}]}) == 1
    assert count_results([{"vod_id": 1}, {"vod_id": 2}]) == 2
