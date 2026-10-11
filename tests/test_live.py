"""The second entry point: one deduped ``lives`` list (直播入口)."""

from __future__ import annotations

import json

from app.build.live import (
    build_live_entry,
    configs_from_directory,
    configs_from_plan,
    write_live_entry,
)


def _cfg(*lives):
    return {"lives": list(lives)}


def test_playlists_from_every_config_are_merged_in_order():
    payload = build_live_entry([
        _cfg({"name": "央视频道", "url": "http://a.example/1.txt"}),
        _cfg({"name": "地方频道", "url": "http://b.example/2.txt"}),
    ])
    assert [item["name"] for item in payload["lives"]] == ["央视频道", "地方频道"]
    assert [item["url"] for item in payload["lives"]] == [
        "http://a.example/1.txt", "http://b.example/2.txt"]


def test_the_same_url_is_kept_once_even_under_different_names():
    payload = build_live_entry([
        _cfg({"name": "甲", "url": "https://a.example/list.txt"}),
        _cfg({"name": "乙", "url": "https://a.example/list.txt/"}),
    ])
    assert [item["name"] for item in payload["lives"]] == ["甲"]


def test_a_repeated_name_is_kept_once_so_the_menu_is_not_full_of_twins():
    payload = build_live_entry([
        _cfg(
            {"name": "直播", "url": "https://a.example/1.txt"},
            {"name": "直播", "url": "https://b.example/2.txt"},
            {"name": "直播", "url": "https://c.example/3.txt"},
        ),
    ])
    assert [item["url"] for item in payload["lives"]] == ["https://a.example/1.txt"]


def test_items_without_a_playable_url_are_dropped():
    payload = build_live_entry([
        _cfg(
            {"name": "空", "url": ""},
            {"name": "脚本", "url": "javascript:x"},
            {"name": "缺字段"},
            {"name": "本机", "url": "http://127.0.0.1:5266/iptv.m3u"},
            {"name": "本机6", "url": "http://[::1]/list.txt"},
            {"name": "无主机", "url": "http:///list.txt"},
            {"name": "正常", "url": "https://ok.example/1.m3u"},
        ),
    ])
    assert [item["name"] for item in payload["lives"]] == ["正常"]


def test_only_known_fields_survive_and_type_is_normalised():
    payload = build_live_entry([
        _cfg({"name": "A", "url": "http://a.example/1.txt", "type": "1", "epg": "http://epg",
              "logo": "http://logo", "杂物": "drop me"}),
    ])
    assert payload["lives"][0] == {
        "name": "A", "url": "http://a.example/1.txt", "type": 1, "epg": "http://epg",
        "logo": "http://logo",
    }


def test_an_unnamed_playlist_still_gets_a_label():
    payload = build_live_entry([_cfg({"url": "http://a.example/1.txt"})])
    assert payload["lives"][0]["name"] == "直播1"


def test_no_playlist_at_all_yields_an_empty_entry():
    assert build_live_entry([])["lives"] == []
    assert build_live_entry([{"sites": []}])["lives"] == []


def test_the_cap_keeps_the_first_playlists():
    payload = build_live_entry(
        [_cfg(*[{"name": f"频道{i}", "url": f"http://a.example/{i}.txt"} for i in range(5)])],
        max_playlists=2,
    )
    assert [item["name"] for item in payload["lives"]] == ["频道0", "频道1"]


def test_configs_from_plan_reads_the_mirrored_text():
    class Entry:
        def __init__(self, content):
            self.content = content

    class Plan:
        entries = {"a": Entry(b'{"lives":[{"name":"A","url":"http://a.example/1.txt"}]}')}

    payload = build_live_entry(configs_from_plan(Plan()))
    assert [item["name"] for item in payload["lives"]] == ["A"]


def test_configs_from_directory_skips_what_it_cannot_parse(tmp_path):
    (tmp_path / "b.json").write_text('{"lives":[]}', encoding="utf-8")
    (tmp_path / "a.json").write_text('{"lives":[{"name":"A","url":"http://a.example/1.txt"}]}',
                                      encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    payload = build_live_entry(configs_from_directory(tmp_path))
    assert [item["name"] for item in payload["lives"]] == ["A"]


def test_write_live_entry_leaves_no_temporary_file(tmp_path):
    path = write_live_entry(tmp_path / "tvbox-live.json", build_live_entry(
        [_cfg({"name": "A", "url": "http://a.example/1.txt"})]))
    assert json.loads(path.read_text(encoding="utf-8"))["lives"][0]["name"] == "A"
    assert list(tmp_path.glob("*.tmp")) == []
