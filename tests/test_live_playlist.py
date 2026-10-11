"""Merged 直播 playlist (app/build/live_playlist.py)."""

from __future__ import annotations

import json

from app.build.live_playlist import (
    ALIVE,
    DEAD,
    Channel,
    canonical_group,
    fetch_channels,
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
东方卫视,http://c.example/df.m3u8
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


def test_canonical_group_sends_foreign_channels_to_their_own_group():
    assert canonical_group("NHK World Japan", "国际") == "国外"
    assert canonical_group("KBS World", "") == "国外"
    assert canonical_group("BBC News", "News") == "国外"
    # 大陆自己的台名里常带「国际」，不能因此被划进国外
    assert canonical_group("CCTV-4 国际频道", "央视") == "央视"
    # 名字里的国家/品牌词压过上分组标签
    assert canonical_group("日本 NHK 综合", "央视频道") == "国外"


def test_canonical_group_keeps_mainland_international_feeds_off_the_foreign_group():
    """用户报的「伪国外」：广西国际 / 浙江国际 / 中国国际教育 摆进了国外组。"""
    for name in ("广西国际", "浙江国际", "浙江国际[1920*1080]", "湖南国际"):
        assert canonical_group(name, "") == "地方"
        assert canonical_group(name, "国际频道") == "地方"
    assert canonical_group("中国国际教育1[768*576]", "") == "地方"
    assert canonical_group("内蒙古农牧频道[1920*1080]", "") == "地方"
    # 上游爱用拼音，也要挡住
    assert canonical_group("Zhejiang TV International", "") == "地方"
    # 真外国台不许被这道闸误伤
    assert canonical_group("Africa 24 English (1080p)", "") == "国外"
    assert canonical_group("Carousel International (576p)", "") == "国外"


def test_canonical_group_files_hk_stations_by_name_not_by_upstream_label():
    """港澳台上游把它塞进哪个标签都不算数，名字说了算。"""
    assert canonical_group("翡翠台", "") == "港澳台"
    assert canonical_group("明珠台", "其他") == "港澳台"
    assert canonical_group("无线新闻台", "央视频道") == "港澳台"
    assert canonical_group("ViuTV", "") == "港澳台"
    assert canonical_group("HOY TV 77", "") == "港澳台"
    assert canonical_group("港台电视31", "") == "港澳台"
    assert canonical_group("凤凰中文", "央视频道") == "港澳台"
    assert canonical_group("莲花卫视（澳门）", "") == "港澳台"


def test_premium_foreign_channels_land_in_the_foreign_group():
    """HBO / STAR Movies / AXN 这种是用户点名要的真国外台。"""
    assert canonical_group("HBO", "") == "国外"
    assert canonical_group("HBO（美）[768*576]", "") == "国外"
    assert canonical_group("STAR Movies", "国外") == "国外"
    assert canonical_group("AXN", "") == "国外"
    assert canonical_group("Cinemax", "") == "国外"
    assert canonical_group("国家地理（港）", "") == "国外"


def test_local_source_reads_a_file_that_ships_with_the_repo(tmp_path):
    """港澳台精选跟着仓库走：不指望任何公开列表还留着这几条。"""
    curated = tmp_path / "live_hk.txt"
    curated.write_text(
        '#EXTM3U\n#EXTINF:-1 tvg-name="翡翠台" group-title="港澳台",翡翠台\n'
        "http://hk.example/jade.ts\n",
        encoding="utf-8",
    )

    def never_called(url: str, mode: str) -> str:
        raise AssertionError("fetch: local 不该走网络")

    channels, failed = fetch_channels(
        [{"name": "精选", "url": str(curated), "fetch": "local"}], never_called
    )
    assert failed == []
    assert [(channel.name, channel.group, channel.url) for channel in channels] == [
        ("翡翠台", "港澳台", "http://hk.example/jade.ts")
    ]
    assert [channel.priority for channel in channels] == [1]


def test_a_missing_local_source_is_skipped_not_fatal():
    channels, failed = fetch_channels(
        [{"name": "精选", "url": "config/does-not-exist.txt", "fetch": "local"}], lambda u, m: ""
    )
    assert channels == []
    assert failed and "精选" in failed[0]


def test_a_curated_line_survives_the_per_name_cap():
    """上游同名线路一多就把精选那条挤出上限 —— 它恰恰是唯一能播的那条。"""
    channels = [
        Channel("STAR Movies", "http://aaa.example/1", "国外"),
        Channel("Star Movies HD (1080p)", "http://aab.example/2", "国外"),
        Channel("Star Movies HD (1080p)", "http://aac.example/3", "国外"),
        Channel("STAR Movies", "http://zzz.example/hk.ts", "国外", priority=1),
    ]

    merged = merge_channels([channels], max_per_name=3, require_alive_groups=())

    assert [channel.url for channel in merged][0] == "http://zzz.example/hk.ts"
    assert len(merged) == 3


def test_a_curated_line_survives_a_group_that_is_already_over_its_cap():
    """国外组上千条、上限 600：排序靠后的精选线路不许在实测之前就被砍掉。"""
    foreign = [Channel(f"外国台{i:03d}", f"http://f{i}.example/1", "国外") for i in range(600)]
    curated = Channel("HBO", "http://hk.example/hbo.ts", "国外", priority=1)

    merged = merge_channels(
        [foreign + [curated]],
        max_per_name=3,
        max_per_group={"国外": 600},
        require_alive_groups=(),
    )

    urls = [channel.url for channel in merged]
    assert "http://hk.example/hbo.ts" in urls
    assert len(urls) == 600


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


def test_merge_drops_a_group_channel_with_no_playable_source():
    """港澳台: a channel whose sources all fail is worse than no channel.

    The user taps it, gets 「播放失败」, and writes off the whole list.
    """
    channels = [
        Channel("东森新闻", "http://dead.example/ds.m3u8", "港澳台"),
        Channel("东森新闻", "http://slow.example/ds.m3u8", "港澳台"),
        Channel("凤凰中文", "http://ok.example/fh.m3u8", "港澳台"),
        Channel("CCTV1", "http://slow.example/cctv1.m3u8", "央视"),
    ]
    verdicts = {
        "http://dead.example/ds.m3u8": DEAD,
        "http://slow.example/ds.m3u8": "unknown",
        "http://ok.example/fh.m3u8": ALIVE,
        "http://slow.example/cctv1.m3u8": "unknown",
    }

    without = merge_channels([channels], verdicts=verdicts)
    assert {"东森新闻", "凤凰中文"} <= {channel.name for channel in without}

    merged = merge_channels([channels], verdicts=verdicts, require_alive_groups=["港澳台"])
    names = {channel.name for channel in merged}
    assert "东森新闻" not in names  # every source failed
    assert "凤凰中文" in names
    assert "CCTV1" in names  # other groups keep their unclear sources


def test_drop_without_alive_does_not_let_a_variant_ride_along():
    """「凤凰中文高清」 is a separate row in the client, so it needs its own proof."""
    channels = [
        Channel("凤凰中文", "http://ok.example/fh.m3u8", "港澳台"),
        Channel("凤凰中文高清", "http://slow.example/fh-hd.m3u8", "港澳台"),
    ]
    verdicts = {
        "http://ok.example/fh.m3u8": ALIVE,
        "http://slow.example/fh-hd.m3u8": "unknown",
    }

    merged = merge_channels([channels], verdicts=verdicts, require_alive_groups=["港澳台"])
    assert [channel.name for channel in merged] == ["凤凰中文"]


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
