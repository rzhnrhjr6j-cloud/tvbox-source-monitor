"""Merged 直播 playlist (app/build/live_playlist.py)."""

from __future__ import annotations

import json

from app.build.live_playlist import (
    ALIVE,
    DEAD,
    Channel,
    canonical_group,
    load_verdicts,
    merge_channels,
    name_key,
    parse_playlist,
    render_m3u,
    render_txt,
    save_verdicts,
    sort_channels,
)


M3U = """#EXTM3U
#EXTINF:-1 tvg-name="CCTV1" group-title="央视IPV4",CCTV1
http://a.example/cctv1.m3u8
#EXTINF:-1 tvg-name="湖南卫视高清" group-title="卫视频道",湖南卫视
http://b.example/hunan.m3u8
#EXTINF:-1,凤凰中文
http://c.example/fh.m3u8
"""

TXT = """央视频道,#genre#
CCTV1,http://a.example/cctv1.m3u8
CCTV2,http://a.example/cctv2.m3u8#http://b.example/cctv2.m3u8
凤凰卫视,http://c.example/fh.m3u8
"""


def test_parse_m3u_reads_name_group_and_url():
    channels = parse_playlist(M3U)
    assert [channel.name for channel in channels] == ["CCTV1", "湖南卫视", "凤凰中文"]
    assert channels[0].group == "央视"
    assert channels[1].group == "卫视"
    assert channels[2].url == "http://c.example/fh.m3u8"


def test_parse_txt_splits_groups_and_multiple_urls():
    channels = parse_playlist(TXT)
    assert len(channels) == 4
    assert {channel.group for channel in channels[:3]} == {"央视"}
    # 名称里带「卫视」时按名称归档，不信上游那个 genre 标签
    assert channels[-1].group == "卫视"


def test_parse_playlist_rejects_html_and_empty():
    assert parse_playlist("<html>404</html>") == []
    assert parse_playlist("") == []


def test_name_key_collapses_the_same_channel():
    for spelling in ("CCTV-1", "cctv1", "CCTV-1 (1080p)", "CCTV-1HD", "CCTV-1 高清"):
        assert name_key(spelling) == "CCTV1"


def test_canonical_group_folds_the_labels_in_the_wild():
    assert canonical_group("CCTV1", "央视IPV4") == "央视"
    assert canonical_group("湖南卫视", "卫视频道") == "卫视"
    assert canonical_group("翡翠台", "香港") == "港澳台"
    assert canonical_group("河南民生", "省内频道") == "地方"
    assert canonical_group("随便", "") == "其他"


def test_merge_dedupes_caps_and_puts_alive_first():
    channels = [
        Channel("CCTV1", "http://dead.example/1", "央视"),
        Channel("CCTV-1HD", "http://ok.example/1", "央视"),
        Channel("CCTV1", "http://ok.example/1", "央视"),        # duplicate url
        Channel("CCTV1", "http://ok.example/2", "央视"),
        Channel("CCTV1", "http://ok.example/3", "央视"),
        Channel("CCTV1", "http://ok.example/4", "央视"),        # over the cap
    ]
    merged = merge_channels([channels], verdicts={
        "http://dead.example/1": DEAD,
        "http://ok.example/1": ALIVE,
    }, max_per_name=3)
    assert [channel.url for channel in merged] == [
        "http://ok.example/1", "http://ok.example/2", "http://ok.example/3",
    ]


def test_merge_keeps_groups_contiguous_and_can_cap_them():
    channels = (
        [Channel(f"地方{i}", f"http://d.example/{i}", "地方") for i in range(5)]
        + [Channel("CCTV1", "http://c.example/1", "央视")]
    )
    merged = merge_channels([channels], max_per_name=3, max_per_group={"地方": 2})
    groups = [channel.group for channel in merged]
    assert groups == ["央视", "地方", "地方"]


def test_sort_is_stable_for_unknown_verdicts():
    channels = [Channel("B", "http://b.example", "央视"), Channel("A", "http://a.example", "央视")]
    assert [channel.name for channel in sort_channels(channels)] == ["A", "B"]


def test_render_m3u_round_trips():
    channels = [Channel("CCTV1", "http://a.example/1", "央视", logo="http://l.example/1.png")]
    text = render_m3u(channels)
    assert text.startswith("#EXTM3U")
    assert 'tvg-logo="http://l.example/1.png"' in text
    assert parse_playlist(text) == channels


def test_render_txt_round_trips():
    channels = [Channel("CCTV1", "http://a.example/1", "央视"), Channel("湖南卫视", "http://b.example/1", "卫视")]
    parsed = parse_playlist(render_txt(channels))
    assert [(channel.name, channel.url, channel.group) for channel in parsed] == [
        ("CCTV1", "http://a.example/1", "央视"),
        ("湖南卫视", "http://b.example/1", "卫视"),
    ]


def test_verdict_cache_round_trip(tmp_path):
    path = tmp_path / "live_probe.json"
    save_verdicts(path, {"http://a.example/1": ALIVE, "http://b.example/1": DEAD})
    assert load_verdicts(path) == {"http://a.example/1": ALIVE, "http://b.example/1": DEAD}
    assert json.loads(path.read_text(encoding="utf-8"))["verdicts"]["http://a.example/1"]["verdict"] == ALIVE
    assert load_verdicts(tmp_path / "missing.json") == {}
