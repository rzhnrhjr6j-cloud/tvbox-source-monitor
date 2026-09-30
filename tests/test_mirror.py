"""Mirror tests (spec §18 / §37).

Why this module exists at all: the probes run on GitHub runners and can always
reach raw.githubusercontent.com, so nothing in the health pipeline can notice
that the *client* in mainland China cannot.  Mirroring is the fix, and these
tests pin the behaviour that makes the published URLs loadable.

The fetch/rewrite path is exercised over real sockets against
tests/local_source_server.py - spec §35-10 forbids mocks on the final link.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from app.build.builder import TVBOX_FILE, ConfigBuilder, _retarget_urls
from app.models import Status
from app.build.mirror import (
    SOURCES_DIR,
    ConfigMirror,
    _clean_name,
    _pick_name,
    _site_count,
    rewrite_inner,
    rewrite_relative,
)
from app.config import load_config
from app.storage.sqlite import Store
from app.utils.http_client import HttpClient
from tests.local_source_server import INNER_RAW, LocalSourceServer

ROOT = Path(__file__).resolve().parents[1]


def make_cfg(tmp_path: Path, mirror: dict | None = None):
    return load_config(
        explicit_root=ROOT,
        overrides={
            "app": {
                "data_dir": str(tmp_path / "data"),
                "db_path": str(tmp_path / "data" / "monitor.db"),
                "dist_dir": str(tmp_path / "dist"),
            },
            "output": {"min_sources": 1, "observation_days": 0, "mirror": dict(mirror or {})},
        },
    )


def make_mirror(tmp_path: Path, mirror: dict | None = None) -> ConfigMirror:
    cfg = make_cfg(tmp_path, mirror)
    client = HttpClient(cfg.section("http"), allow_private=True)
    return ConfigMirror(cfg, tmp_path / "dist", client)


def stub_source(url: str, name: str = "stub", source_id: str = "a" * 64):
    return types.SimpleNamespace(id=source_id, url=url, raw_url=url, name=name)


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("[607KB/s|1290ms|稳] 360资源", "360资源"),
        ("【高清】无尽影库", "无尽影库"),
        ("🚀XMVideo┃SP", "XMVideo┃SP"),
        ("🍏无印", "无印"),
        ("豆瓣", "豆瓣"),
    ],
)
def test_clean_name_strips_bracket_speed_and_emoji(raw, expected):
    assert _clean_name(raw) == expected


def test_pick_name_prefers_a_chinese_site_name():
    config = {"sites": [{"name": "[999KB/s] 🚀虎牙影库"}, {"name": "second"}]}
    name = _pick_name(config, "https://raw.githubusercontent.com/o/r/main/c.json", stub_source("x"))
    assert name == "虎牙影库"


def test_pick_name_falls_back_to_owner_slash_repo():
    """An all-English config still gets an identifying, non-duplicate label."""
    config = {"sites": [{"name": "English Only"}]}
    name = _pick_name(config, "https://raw.githubusercontent.com/bluefriendCN/set/main/mx.json", stub_source("x"))
    assert name == "bluefriendCN/set"


@pytest.mark.parametrize(
    "config, expected",
    [
        ({"urls": [{"name": "🚀天微影视VIP线🚀"}, {"name": "其他"}]}, "天微影视VIP线"),
        ({"urls": [{"name": "English"}, {"name": "💫影视仓口Pro"}]}, "影视仓口Pro"),
        ([{"name": "茅台资源站采集接口"}], "茅台资源站采集接口"),
    ],
)
def test_pick_name_reads_child_rows_of_a_multi_warehouse(config, expected):
    """Multi-warehouse configs carry no sites, but their children are Chinese."""
    name = _pick_name(config, "https://raw.githubusercontent.com/o/r/main/c.json", stub_source("x"))
    assert name == expected


@pytest.mark.parametrize(
    "config, expected",
    [
        ({"sites": [{}, {}]}, 2),
        ({"urls": [{}, {}, {}]}, 3),
        ([{}, {}], 2),
        ({}, 0),
        ("nonsense", 0),
    ],
)
def test_site_count_covers_sites_children_and_bare_arrays(config, expected):
    assert _site_count(config) == expected


def test_pick_name_falls_back_to_host_then_source_name():
    assert _pick_name({}, "https://cdn.example.com/a.json", stub_source("x")) == "cdn.example.com"
    assert _pick_name({}, "not-a-url", stub_source("x", name="kept")) == "kept"


def test_clean_name_strips_a_full_width_paren_prefix():
    assert _clean_name("（微信公众号）宝盒没宝") == "宝盒没宝"


def test_dedupe_names_makes_every_label_unique_and_counts_sites():
    """The original bug report: eight entries all literally called 'tvbox.json'."""
    from app.build.mirror import MirrorEntry, MirrorPlan

    plan = MirrorPlan(enabled=True)
    for index in range(3):
        plan.entries[f"id{index}"] = MirrorEntry(
            source_id=f"id{index}", slug=f"slug{index}", name="tvbox.json",
            url=f"https://h/{SOURCES_DIR}/slug{index}.json", site_count=10 + index,
            content=b"{}", origin="o",
        )
    ConfigMirror._dedupe_names(plan)

    names = [entry.name for entry in plan.entries.values()]
    assert len(set(names)) == 3, names
    assert all("站" in name for name in names), names


def test_identical_bytes_are_published_once():
    """Discovered sources often mirror the same upstream file."""
    from app.build.mirror import MirrorEntry, MirrorPlan

    plan = MirrorPlan(enabled=True)
    for index, body in enumerate([b'{"sites":[]}', b'{"sites":[]}', b'{"sites":[1]}']):
        plan.entries[f"id{index}"] = MirrorEntry(
            source_id=f"id{index}", slug=f"slug{index}", name=f"源{index}",
            url=f"https://h/{SOURCES_DIR}/slug{index}.json", site_count=1,
            content=body, origin="o",
        )
    ConfigMirror._drop_duplicate_content(plan)

    assert set(plan.entries) == {"id0", "id2"}
    assert plan.dropped == {"id1"}


# ---------------------------------------------------------------------------
# inner references
# ---------------------------------------------------------------------------
RAW = "https://raw.githubusercontent.com/4TVBox/TVBox/refs/heads/main/config.json"
ORIGIN = "https://raw.githubusercontent.com/qist/tvbox/master/config.json"


def test_relative_references_resolve_against_the_origin_and_keep_the_md5():
    text, count = rewrite_relative('{"spider":"./jar/fan.txt;md5;abc"}', ORIGIN)
    assert count == 1
    assert "https://raw.githubusercontent.com/qist/tvbox/master/jar/fan.txt;md5;abc" in text


def test_relative_references_never_escape_the_origin_directory():
    payload = '{"spider":"../../etc/passwd"}'
    text, count = rewrite_relative(payload, ORIGIN)
    assert count == 0 and text == payload


def test_relative_rewrite_leaves_absolute_and_unrelated_strings_alone():
    payload = '{"spider":"https://x.com/a.jar","note":"./looks/like/a/path"}'
    text, count = rewrite_relative(payload, ORIGIN)
    assert count == 0 and text == payload


def test_resolved_references_then_get_the_acceleration_prefix():
    text, _ = rewrite_relative('{"spider":"./spider.jar"}', ORIGIN)
    text, count = rewrite_inner(text, "https://gh-proxy.com/")
    assert count == 1
    assert "https://gh-proxy.com/https://raw.githubusercontent.com/qist/tvbox/master/spider.jar" in text


def test_a_shipped_jar_is_resolved_over_a_real_socket(tmp_path):
    """The published config must point at a file the client can actually fetch."""
    with LocalSourceServer() as server:
        mirror = make_mirror(tmp_path, {"enabled": True, "public_base": "https://me.github.io/repo"})
        plan = mirror.prepare([stub_source(f"{server.base}/withjar.json", source_id="7" * 64)])
        body = plan.entries["7" * 64].content.decode("utf-8")
        assert f"{server.base}/assets/spider.jar;md5;deadbeef" in body
        assert "./assets/spider.jar" not in body


def test_sites_with_a_dead_jar_are_pruned_over_a_real_socket(tmp_path):
    """A jar that 404s would make 影视仓 answer "jar加载失败" on that entry."""
    with LocalSourceServer() as server:
        mirror = make_mirror(tmp_path, {"enabled": True, "public_base": "https://me.github.io/repo"})
        plan = mirror.prepare([stub_source(f"{server.base}/deadjars.json", source_id="8" * 64)])
        entry = plan.entries["8" * 64]
        body = json.loads(entry.content)
        assert [site["key"] for site in body["sites"]] == ["live-jar", "plain"]
        assert entry.site_count == 2
        # the client loads a crawler the moment the source is opened, so a dead
        # one has to go even when every surviving site talks http directly
        assert "spider" not in body


def test_a_config_whose_every_site_has_a_dead_jar_is_dropped(tmp_path):
    """Nothing to offer is worse than nothing: the entry must not be published."""
    with LocalSourceServer() as server:
        mirror = make_mirror(tmp_path, {"enabled": True, "public_base": "https://me.github.io/repo"})
        plan = mirror.prepare([stub_source(f"{server.base}/alljarsdead.json", source_id="9" * 64)])
        assert "9" * 64 in plan.dropped
        assert "9" * 64 not in plan.entries


def test_jar_pruning_can_be_switched_off(tmp_path):
    with LocalSourceServer() as server:
        mirror = make_mirror(tmp_path, {
            "enabled": True, "public_base": "https://me.github.io/repo",
            "prune_dead_jars": False,
        })
        plan = mirror.prepare([stub_source(f"{server.base}/deadjars.json", source_id="b" * 64)])
        body = json.loads(plan.entries["b" * 64].content)
        assert len(body["sites"]) == 4


def test_inner_raw_urls_get_the_proxy_prefix():
    """A config points at sibling configs and playlists; all of them were dead."""
    text, count = rewrite_inner('{"urls":["%s"]}' % RAW, "https://gh-proxy.com/")
    assert count == 1
    assert f"https://gh-proxy.com/{RAW}" in text


def test_an_already_proxied_reference_is_not_wrapped_twice():
    once = f"https://gh-proxy.com/{RAW}"
    text, count = rewrite_inner('{"urls":["%s"]}' % once, "https://gh-proxy.com/")
    assert count == 0 and text.count("gh-proxy.com") == 1


def test_rewrite_leaves_unrelated_hosts_alone():
    payload = '{"lives":[{"url":"https://example.com/list.m3u8"}],"x":"http://a.b/c"}'
    text, count = rewrite_inner(payload, "https://gh-proxy.com/")
    assert count == 0 and text == payload


def test_rewrite_handles_several_references_and_refs_heads_paths():
    payload = f'{{"a":"{RAW}","b":"https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"}}'
    text, count = rewrite_inner(payload, "https://ghfast.top/")
    assert count == 2
    assert "ghfast.top/https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8" in text


def test_rewrite_is_off_when_no_proxy_is_configured():
    text, count = rewrite_inner(f'{{"a":"{RAW}"}}', "")
    assert count == 0 and text == f'{{"a":"{RAW}"}}'


def test_a_proxied_neighbour_does_not_shelter_the_next_bare_reference():
    """Only the prefix directly in front of a reference marks it as proxied.

    Configs often list a proxied URL and a bare one side by side; the bare one
    still has to be rewritten, otherwise it stays unreachable in China.  The
    neighbour is re-pointed at our proxy on the way through - theirs may be
    gone, and the client cannot reach the origin either way.
    """
    bare = "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"
    payload = f'{{"urls":["https://ghfast.top/{RAW}","{bare}"]}}'
    text, count = rewrite_inner(payload, "https://gh-proxy.com/")
    assert count == 2
    assert "ghfast.top" not in text
    assert f"https://gh-proxy.com/{RAW}" in text
    assert f"https://gh-proxy.com/{bare}" in text


def test_a_foreign_proxy_is_replaced_by_ours():
    """An author's hop can itself be gone; a dead middleman 404s the client.

    daili.korice.eu.org carried 12 references of the published set and no
    longer answered, which is one whole family of "jar加载失败" reports.
    """
    payload = f'{{"urls":["https://daili.korice.eu.org/{RAW}"]}}'
    text, count = rewrite_inner(payload, "https://gh-proxy.com/")
    assert count == 1
    assert "daili.korice.eu.org" not in text
    assert f"https://gh-proxy.com/{RAW}" in text


def test_a_schemeless_foreign_proxy_is_replaced_by_ours():
    """Some proxies drop the inner scheme: "<host>/raw.githubusercontent.com/…"."""
    schemeless = RAW.replace("https://", "", 1)
    payload = f'{{"urls":["https://hub.gitmirror.example/{schemeless}"]}}'
    text, count = rewrite_inner(payload, "https://gh-proxy.com/")
    assert count == 1
    assert "gitmirror.example" not in text
    assert f"https://gh-proxy.com/{RAW}" in text


def test_disabling_rewrite_publishes_the_origin_bytes(tmp_path):
    with LocalSourceServer() as server:
        mirror = make_mirror(tmp_path, {
            "enabled": True, "public_base": "https://me.github.io/repo",
            "rewrite_inner": False,
        })
        plan = mirror.prepare([stub_source(f"{server.base}/inner.json", source_id="9" * 64)])
        entry = plan.entries["9" * 64]
        assert entry.inner_rewrites == 0
        assert INNER_RAW.encode() in entry.content


def test_enabling_rewrite_proxies_inner_references(tmp_path):
    with LocalSourceServer() as server:
        mirror = make_mirror(tmp_path, {
            "enabled": True, "public_base": "https://me.github.io/repo",
            "rewrite_inner": True, "inner_proxy": "https://gh-proxy.com/",
        })
        plan = mirror.prepare([stub_source(f"{server.base}/inner.json", source_id="8" * 64)])
        entry = plan.entries["8" * 64]
        assert entry.inner_rewrites == 2
        assert entry.content.decode().count(f"https://gh-proxy.com/{INNER_RAW}") == 2


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
def test_public_base_is_derived_from_the_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/tvbox-source-monitor")
    assert make_mirror(tmp_path, {"enabled": True}).public_base == (
        "https://someone.github.io/tvbox-source-monitor"
    )


def test_mirror_is_inert_without_a_publish_target(tmp_path, monkeypatch):
    """No public_base means we do not know where the files will live, so skip."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    mirror = make_mirror(tmp_path, {"enabled": True})
    assert mirror.enabled is False
    plan = mirror.prepare([stub_source("http://127.0.0.1:1/x.json")])
    assert plan.enabled is False and not plan.dropped


# ---------------------------------------------------------------------------
# fetching / rewriting
# ---------------------------------------------------------------------------
def test_alt_base_derives_jsdelivr_from_the_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    mirror = make_mirror(tmp_path, {"enabled": True, "public_base": "https://owner.github.io/repo"})
    assert mirror.alt_base == "https://cdn.jsdelivr.net/gh/owner/repo@main/dist"


def test_alt_base_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    mirror = make_mirror(tmp_path, {"enabled": True, "alt_base": "off"})
    assert mirror.alt_base == ""


def test_alt_base_is_empty_without_a_repository(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    mirror = make_mirror(tmp_path, {"enabled": True})
    assert mirror.alt_base == ""


def test_retarget_urls_swaps_only_urls_under_the_primary_base():
    data = {"urls": [
        {"name": "甲", "url": "https://me.github.io/repo/sources/aa.json"},
        {"name": "乙", "url": "https://elsewhere.example/x.json"},
        {"name": "丙", "url": "https://me.github.io/repo/sources/bb.json"},
    ]}
    assert _retarget_urls(data, "https://me.github.io/repo", "https://cdn.example/d") == 2
    assert data["urls"][0]["url"] == "https://cdn.example/d/sources/aa.json"
    assert data["urls"][1]["url"] == "https://elsewhere.example/x.json"
    assert data["urls"][2]["url"] == "https://cdn.example/d/sources/bb.json"


def test_retarget_urls_ignores_a_shape_without_urls():
    assert _retarget_urls({"sites": []}, "https://a", "https://b") == 0
    assert _retarget_urls("nonsense", "https://a", "https://b") == 0


def test_prepare_fetches_real_bytes_and_rewrites_the_url(tmp_path):
    with LocalSourceServer() as server:
        mirror = make_mirror(tmp_path, {"enabled": True, "public_base": "https://me.github.io/repo"})
        source = stub_source(server.config_url, source_id="c" * 64)
        plan = mirror.prepare([source])

        assert plan.enabled is True
        entry = plan.entries[source.id]
        assert entry.url == f"https://me.github.io/repo/{SOURCES_DIR}/{'c' * 16}.json"
        assert plan.url_for(source.id) == entry.url
        assert plan.name_for(source.id) == entry.name

        # the mirrored payload is the origin's payload, byte for byte
        written = mirror.write(plan)
        stored = (written / f"{entry.slug}.json").read_bytes()
        assert stored == entry.content
        assert json.loads(stored.decode("utf-8"))["sites"]


def test_unreachable_config_is_dropped_by_default(tmp_path):
    mirror = make_mirror(tmp_path, {"enabled": True, "public_base": "https://me.github.io/repo"})
    source = stub_source("http://127.0.0.1:1/gone.json", source_id="d" * 64)
    plan = mirror.prepare([source])
    assert source.id in plan.dropped
    assert source.id not in plan.entries


def test_on_failure_keep_leaves_the_original_url_alone(tmp_path):
    mirror = make_mirror(
        tmp_path, {"enabled": True, "public_base": "https://me.github.io/repo", "on_failure": "keep"}
    )
    source = stub_source("http://127.0.0.1:1/gone.json", source_id="e" * 64)
    plan = mirror.prepare([source])
    assert not plan.dropped and not plan.entries


def test_already_mirrored_urls_are_not_wrapped_twice(tmp_path):
    base = "https://me.github.io/repo"
    mirror = make_mirror(tmp_path, {"enabled": True, "public_base": base})
    source = stub_source(f"{base}/{SOURCES_DIR}/{'f' * 16}.json", source_id="f" * 64)
    plan = mirror.prepare([source])
    assert not plan.entries and not plan.dropped


def test_write_removes_configs_that_are_no_longer_published(tmp_path):
    base = "https://me.github.io/repo"
    mirror = make_mirror(tmp_path, {"enabled": True, "public_base": base})
    stale = tmp_path / "dist" / SOURCES_DIR
    stale.mkdir(parents=True)
    (stale / "oldone.json").write_text("{}", encoding="utf-8")

    with LocalSourceServer() as server:
        plan = mirror.prepare([stub_source(server.config_url, source_id="1" * 64)])
        mirror.write(plan)

    remaining = sorted(p.name for p in stale.glob("*.json"))
    assert "oldone.json" not in remaining, remaining
    assert remaining == [f"{'1' * 16}.json"]


# ---------------------------------------------------------------------------
# through the real builder
# ---------------------------------------------------------------------------
def test_builder_publishes_our_own_urls_and_writes_the_mirror(tmp_path):
    """The whole point: no published URL may point at the origin any more."""
    base = "https://me.github.io/repo"
    cfg = make_cfg(tmp_path, {"enabled": True, "public_base": base})
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    client = HttpClient(cfg.section("http"), allow_private=True)
    try:
        with LocalSourceServer() as server:
            from app.models import Source

            store.upsert_source(Source(id="2" * 64, name="origin", status=Status.ACTIVE,
                                       url=server.config_url, raw_url=server.config_url))
            builder = ConfigBuilder(cfg, store, client=client)
            result = builder.build()
            assert result.published is True, result.blocked_reason

            urls = result.output["urls"]
            assert len(urls) == 1
            published_url = urls[0]["url"]
            assert published_url.startswith(f"{base}/{SOURCES_DIR}/")
            assert published_url != server.config_url
            assert urls[0]["name"] and "站" in urls[0]["name"]

            builder.publish(result)
            dist = cfg.path("app.dist_dir", "dist")
            target = dist / SOURCES_DIR / f"{'2' * 16}.json"
            assert target.is_file()
            # byte-identical to what the origin served
            assert json.loads(target.read_text(encoding="utf-8"))["sites"]
            assert json.loads((dist / TVBOX_FILE).read_text(encoding="utf-8")) == result.output
    finally:
        store.close()
