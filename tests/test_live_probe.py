"""The live probe: a playlist that is gone must not ship as if it worked."""

from __future__ import annotations

from pathlib import Path

from app.build.live import (
    ALIVE,
    DEAD,
    UNKNOWN,
    build_live_entry,
    looks_like_playlist,
    probe_playlists,
    rank_live_entry,
)

M3U = b"#EXTM3U\n#EXTINF:-1, \xe5\xa4\xae\xe8\xa7\x86\xe9\xa2\x91\xe9\x81\x93\nhttp://a.example/1.m3u8\n"
TXT = b"\xe5\xa4\xae\xe8\xa7\x86,http://x/1\n\xe5\x8d\xab\xe8\xa7\x86,http://x/2\n"
BASE = "https://raw.githubusercontent.com/o/r/main/dist/jars/"


class _Result:
    def __init__(self, status: int, content: bytes = b"", ok: bool | None = None):
        self.status = status
        self.content = content
        self.ok = (200 <= status < 300) if ok is None else ok

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


class _Client:
    """Minimal stand-in for :class:`app.utils.http_client.HttpClient`."""

    def __init__(self, table: dict[str, object]):
        self.table = table
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs) -> _Result:
        self.calls.append(url)
        answer = self.table.get(url, _Result(404, b"404: Not Found"))
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[return-value]


def _entry(*lives):
    return build_live_entry([{"lives": list(lives)}])


def _live(name: str, url: str):
    return {"name": name, "url": url}


def test_a_fetched_playlist_is_alive_and_a_404_is_dead():
    entry = _entry(_live("活", "http://a.example/1.m3u"), _live("死", "http://b.example/2.m3u"))
    report = probe_playlists(entry["lives"], client=_Client({
        "http://a.example/1.m3u": _Result(200, M3U),
        "http://b.example/2.m3u": _Result(404, b"404: Not Found"),
    }))
    assert report.verdicts == {"http://a.example/1.m3u": ALIVE, "http://b.example/2.m3u": DEAD}


def test_alive_come_first_and_only_the_provably_gone_are_dropped():
    entry = _entry(
        _live("死", "http://dead.example/1.txt"),
        _live("活", "http://ok.example/2.txt"),
        _live("问号", "http://slow.example/3.txt"),
    )
    report = probe_playlists(entry["lives"], client=_Client({
        "http://dead.example/1.txt": _Result(404, b"404: Not Found"),
        "http://ok.example/2.txt": _Result(200, TXT),
        "http://slow.example/3.txt": TimeoutError("boom"),
    }))
    ranked, stats = rank_live_entry(entry, report)
    assert [item["name"] for item in ranked["lives"]] == ["活", "问号"]
    assert stats["alive"] == 1 and stats["dead"] == 1 and stats["unknown"] == 1
    assert stats["dropped"] == 1


def test_an_unreachable_playlist_is_kept_because_the_runner_is_not_the_client():
    entry = _entry(_live("大陆专线", "http://cn-only.example/1.txt"))
    report = probe_playlists(entry["lives"], client=_Client({
        "http://cn-only.example/1.txt": ConnectionResetError("reset"),
    }))
    assert report.verdicts["http://cn-only.example/1.txt"] == UNKNOWN
    ranked, stats = rank_live_entry(entry, report)
    assert [item["name"] for item in ranked["lives"]] == ["大陆专线"]
    assert stats["dropped"] == 0


def test_drop_unhealthy_removes_the_unreachable_too():
    entry = _entry(_live("问号", "http://slow.example/1.txt"))
    report = probe_playlists(entry["lives"], client=_Client({
        "http://slow.example/1.txt": TimeoutError("boom"),
    }))
    ranked, stats = rank_live_entry(entry, report, drop="unhealthy")
    assert ranked["lives"] == []
    assert stats["dropped"] == 1


def test_drop_none_only_reorders():
    entry = _entry(_live("死", "http://dead.example/1.txt"), _live("活", "http://ok.example/2.txt"))
    report = probe_playlists(entry["lives"], client=_Client({
        "http://dead.example/1.txt": _Result(404, b"nope"),
        "http://ok.example/2.txt": _Result(200, M3U),
    }))
    ranked, stats = rank_live_entry(entry, report, drop="none")
    assert [item["name"] for item in ranked["lives"]] == ["活", "死"]
    assert stats["dropped"] == 0


def test_a_mostly_dead_result_is_treated_as_an_outage_and_only_reorders():
    lives = [_live(f"死{i}", f"http://dead.example/{i}.txt") for i in range(9)]
    lives.append(_live("活", "http://ok.example/1.txt"))
    entry = _entry(*lives)
    report = probe_playlists(entry["lives"], client=_Client({
        "http://ok.example/1.txt": _Result(200, M3U),
    }))
    ranked, stats = rank_live_entry(entry, report)
    assert len(ranked["lives"]) == 10
    assert ranked["lives"][0]["name"] == "活"
    assert stats["drop"] == "none" and stats["dropped"] == 0


def test_a_mirrored_playlist_is_judged_from_disk_not_over_the_network(tmp_path: Path):
    (tmp_path / "abc.m3u").write_bytes(M3U)
    entry = _entry(_live("镜像", BASE + "abc.m3u"))
    client = _Client({})  # every network call would 404
    report = probe_playlists(entry["lives"], client=client, local_root=tmp_path)
    assert report.verdicts[BASE + "abc.m3u"] == ALIVE
    assert "local mirror" in report.details[BASE + "abc.m3u"]
    assert client.calls == []


def test_a_mirrored_stub_of_the_right_shape_is_still_alive(tmp_path: Path):
    (tmp_path / "tiny.m3u").write_bytes(b"#EXTM3U\n#EXTINF:-1,cctv\nhttp://x/1\n")
    entry = _entry(_live("小", BASE + "tiny.m3u"))
    report = probe_playlists(entry["lives"], client=_Client({}), local_root=tmp_path)
    assert report.verdicts[BASE + "tiny.m3u"] == ALIVE


def test_an_empty_channel_file_is_dead(tmp_path: Path):
    (tmp_path / "empty.txt").write_bytes(b"")
    entry = _entry(_live("空", BASE + "empty.txt"))
    report = probe_playlists(entry["lives"], client=_Client({}), local_root=tmp_path)
    assert report.verdicts[BASE + "empty.txt"] == DEAD


def test_a_2xx_error_page_is_dead_but_a_txt_list_is_alive():
    entry = _entry(_live("页", "http://a.example/1"), _live("列表", "http://b.example/2"))
    report = probe_playlists(entry["lives"], client=_Client({
        "http://a.example/1": _Result(200, b"<html><body>forbidden</body></html>"),
        "http://b.example/2": _Result(200, TXT),
    }))
    assert report.verdicts["http://a.example/1"] == DEAD
    assert report.verdicts["http://b.example/2"] == ALIVE


def test_a_github_blob_page_served_with_200_is_dead():
    page = b"<!DOCTYPE html><html><body><a href=\"https://github.com/x\">x</a></body></html>" + b" " * 4096
    entry = _entry(_live("blob", "https://github.com/x/y/blob/main/a.m3u"))
    report = probe_playlists(entry["lives"], client=_Client({
        "https://github.com/x/y/blob/main/a.m3u": _Result(200, page),
    }))
    assert report.verdicts["https://github.com/x/y/blob/main/a.m3u"] == DEAD


def test_a_bare_extm3u_header_is_not_a_playlist():
    entry = _entry(_live("空", "http://a.example/1"))
    report = probe_playlists(entry["lives"], client=_Client({
        "http://a.example/1": _Result(200, b"#EXTM3U"),
    }))
    assert report.verdicts["http://a.example/1"] == DEAD


def test_a_json_error_stub_is_dead_but_a_json_live_api_is_not():
    entry = _entry(_live("错", "http://a.example/1"), _live("api", "http://b.example/2"))
    report = probe_playlists(entry["lives"], client=_Client({
        "http://a.example/1": _Result(200, b'{"code":40044,"msg":""}'),
        "http://b.example/2": _Result(200, b'{"channels": [{"url": "http://x/1"}]}'),
    }))
    assert report.verdicts["http://a.example/1"] == DEAD
    assert report.verdicts["http://b.example/2"] == ALIVE


def test_a_large_body_we_cannot_read_is_unknown_and_therefore_kept():
    blob = b"\x00\x01\x02\xff" * 4096
    entry = _entry(_live("怪", "http://a.example/1"))
    report = probe_playlists(entry["lives"], client=_Client({
        "http://a.example/1": _Result(200, blob),
    }))
    assert report.verdicts["http://a.example/1"] == UNKNOWN
    ranked, stats = rank_live_entry(entry, report)
    assert [item["name"] for item in ranked["lives"]] == ["怪"]
    assert stats["dropped"] == 0


def test_an_empty_json_stub_is_dead_and_a_populated_one_is_not():
    entry = _entry(_live("空", "http://a.example/1"), _live("有", "http://b.example/2"))
    report = probe_playlists(entry["lives"], client=_Client({
        "http://a.example/1": _Result(200, b'{"lives": []}'),
        "http://b.example/2": _Result(200, b'{"lives": [{"name": "cctv"}]}'),
    }))
    assert report.verdicts["http://a.example/1"] == DEAD
    assert report.verdicts["http://b.example/2"] == ALIVE


def test_playlist_shapes():
    assert looks_like_playlist("#EXTM3U\n#EXTINF:-1,a\nhttp://x\n")
    assert looks_like_playlist("央视,http://x/1\n卫视,http://x/2\n")
    assert not looks_like_playlist("")
    assert not looks_like_playlist("#EXTM3U")
    assert not looks_like_playlist("央视,http://x/1\n")
    assert not looks_like_playlist("404: Not Found")
    assert not looks_like_playlist("<html>oops</html>")
