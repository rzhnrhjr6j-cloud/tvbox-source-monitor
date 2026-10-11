"""Child-safe second entry (user request 2026-10-11).

The adult rows are not separate top-level sources: they sit *inside* otherwise
ordinary configs (a 豆瓣 list also carries 麻豆 / 黄色仓库 / 51吃瓜), so dropping
matching names at the top level alone would leak them.  These tests pin both
halves - the whole-source drop and the per-row strip - and then drive the real
publishing path against real files on disk (spec §35-10: no mocks).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.build.builder import TVBOX_FILE, ConfigBuilder
from app.build.clean import CleanPolicy, build_clean_entry
from app.build.mirror import SOURCES_DIR
from app.config import load_config
from app.pipeline import Pipeline
from app.storage.sqlite import Store
from app.utils.http_client import HttpClient
from tests.local_source_server import LocalSourceServer

ROOT = Path(__file__).resolve().parents[1]

BASE = "https://me.github.io/repo"
SOURCE_PREFIX = f"{BASE}/{SOURCES_DIR}/"
CLEAN_PREFIX = f"{BASE}/clean/{SOURCES_DIR}/"


def policy(**settings) -> CleanPolicy:
    merged = {
        "enabled": True,
        "block_emoji": ["🔞", "🈲"],
        "block_keywords": ["精东", "成人", "麻豆", "色播"],
        "block_regex": ["(?i)(porn|18av)"],
    }
    merged.update(settings)
    return CleanPolicy.from_settings(merged)


def entry(name: str, slug: str) -> dict:
    return {"name": name, "url": f"{SOURCE_PREFIX}{slug}"}


# ---------------------------------------------------------------------------
# the shipped rules
# ---------------------------------------------------------------------------
def test_shipped_config_enables_the_second_entry(cfg):
    """Deleting the block would silently drop the second entry point."""
    settings = cfg.section("output").get("clean")
    assert isinstance(settings, dict) and settings.get("enabled") is True

    rule = CleanPolicy.from_settings(settings)
    for name in ("精东影业816", "🔞麻豆4K", "[🈲]麻豆(js)", "黄色仓库[采集]",
                 "丝袜资源1080P", "91porn", "黑料不打烊.py"):
        assert rule.blocks(name), name
    for name in ("豆瓣", "豆了（159站）", "饭太硬", "爱壹帆 (海外)", "天天南瓜"):
        assert not rule.blocks(name), name


# ---------------------------------------------------------------------------
# the policy
# ---------------------------------------------------------------------------
def test_policy_is_off_when_the_block_is_absent():
    assert CleanPolicy.from_settings(None).enabled is False
    assert CleanPolicy.from_settings({}).enabled is False


def test_policy_reads_every_rule_from_settings():
    rule = CleanPolicy.from_settings({
        "enabled": True,
        "output_file": "safe.json",
        "directory": "/safe/",
        "block_emoji": ["❌"],
        "block_keywords": [" 麻豆 ", ""],
        "block_regex": ["(?i)18av"],
    })

    assert (rule.enabled, rule.output_file, rule.directory) == (True, "safe.json", "safe")
    assert rule.block_emoji == ("❌",)
    assert rule.block_keywords == ("麻豆",)
    assert rule.blocks("18AV在线")


def test_policy_accepts_dir_as_well_as_directory():
    assert CleanPolicy.from_settings({"directory": "x"}).directory == "x"
    assert CleanPolicy.from_settings({"dir": "y"}).directory == "y"
    assert CleanPolicy.from_settings({}).directory == "clean"


@pytest.mark.parametrize(
    "name, blocked",
    [
        ("🔞麻豆4K", True),
        ("[🈲]麻豆(js)", True),
        ("台湾成人", True),
        ("91PORN", True),
        ("精东影业816", True),
        ("【高清】无尽影库", False),
        ("豆瓣", False),
        ("", False),
        (None, False),
        ("   ", False),
    ],
)
def test_blocks_by_emoji_keyword_or_regex(name, blocked):
    assert policy().blocks(name) is blocked


def test_a_broken_pattern_never_matches_and_never_raises():
    rule = CleanPolicy.from_settings({"enabled": True, "block_regex": ["([unclosed"]})
    assert rule.blocks("anything") is False


# ---------------------------------------------------------------------------
# editing one config
# ---------------------------------------------------------------------------
def test_strip_rows_drops_matching_sites_and_lives():
    payload = {
        "sites": [
            {"name": "豆瓣", "api": "https://a/"},
            {"name": "🔞麻豆4K", "api": "https://b/"},
            {"name": "台湾成人", "api": "https://c/"},
            "not-a-dict",
        ],
        "lives": [{"name": "CCTV", "url": "x"}, {"name": "色播", "url": "y"}],
        "parses": [{"name": "p"}],
    }

    assert policy().strip_rows(payload) == 3
    assert [row["name"] if isinstance(row, dict) else row for row in payload["sites"]] == ["豆瓣", "not-a-dict"]
    assert [row["name"] for row in payload["lives"]] == ["CCTV"]
    assert payload["parses"] == [{"name": "p"}]


def test_strip_rows_leaves_a_clean_config_untouched():
    payload = {"sites": [{"name": "豆瓣"}], "lives": [], "parses": []}
    assert policy().strip_rows(payload) == 0
    assert payload == {"sites": [{"name": "豆瓣"}], "lives": [], "parses": []}


def test_a_config_is_empty_only_when_nothing_playable_is_left():
    rule = policy()
    assert rule.is_empty({}) is True
    assert rule.is_empty({"sites": [], "lives": [], "parses": []}) is True
    # a web parser is playable even with no sites and no lives left
    assert rule.is_empty({"sites": [], "lives": [], "parses": [{"url": "p.js"}]}) is False
    assert rule.is_empty({"sites": [{"name": "x"}]}) is False


# ---------------------------------------------------------------------------
# deriving the whole entry
# ---------------------------------------------------------------------------
def make(read: dict, entries: list, **settings):
    seen: list[str] = []

    def read_child(slug):
        seen.append(slug)
        return read.get(slug)

    result = build_clean_entry(
        {"version": "1.0", "generated_at": "now", "urls": entries},
        policy(**settings),
        read_child,
        source_prefix=SOURCE_PREFIX,
        clean_prefix=CLEAN_PREFIX,
    )
    return result, seen


def test_a_source_named_after_an_adult_site_is_dropped_whole():
    result, seen = make({}, [entry("豆瓣", "a.json"), entry("精东影业", "b.json")])

    assert [item["name"] for item in result.payload["urls"]] == ["豆瓣"]
    assert result.dropped_sources == ["精东影业"]
    assert seen == ["a.json"], "a dropped source must not even be read"


def test_rows_inside_a_child_are_stripped_and_its_url_repointed():
    read = {"b.json": {"sites": [{"name": "普通"}, {"name": "🔞麻豆"}]}}
    result, _ = make(read, [entry("豆瓣", "b.json")])

    assert list(result.children) == ["b.json"]
    assert result.children["b.json"]["sites"] == [{"name": "普通"}]
    assert (result.removed_rows, result.rewritten) == (1, 1)
    assert result.payload["urls"][0]["url"] == f"{CLEAN_PREFIX}b.json"
    # the dict handed in is edited in place, the way the caller reads it back
    assert read["b.json"]["sites"] == [{"name": "普通"}]


def test_an_untouched_child_keeps_its_own_url_and_is_not_duplicated():
    read = {"b.json": {"sites": [{"name": "普通"}]}}
    result, _ = make(read, [entry("豆瓣", "b.json")])

    assert result.children == {}
    assert result.rewritten == 0
    assert result.payload["urls"][0]["url"] == f"{SOURCE_PREFIX}b.json"


def test_a_child_that_loses_every_site_is_dropped():
    read = {"b.json": {"sites": [{"name": "麻豆"}], "lives": []}}
    result, _ = make(read, [entry("豆瓣", "b.json")])

    assert result.payload["urls"] == []
    assert result.dropped_sources == ["豆瓣"]
    assert result.children == {}


def test_a_child_that_keeps_only_a_parser_is_kept():
    read = {"b.json": {"sites": [{"name": "色播"}], "parses": [{"url": "p.js"}]}}
    result, _ = make(read, [entry("豆瓣", "b.json")])

    assert list(result.children) == ["b.json"]


def test_a_child_we_cannot_read_is_left_alone():
    result, seen = make({}, [entry("豆瓣", "b.json")])

    assert seen == ["b.json"]
    assert result.payload["urls"][0]["url"] == f"{SOURCE_PREFIX}b.json"
    assert result.children == {}


def test_a_slug_that_is_a_path_is_never_read():
    result, seen = make({}, [{"name": "豆瓣", "url": f"{SOURCE_PREFIX}nested/b.json"}])

    assert seen == []
    assert result.children == {}


def test_a_bare_list_payload_is_supported():
    result = build_clean_entry(
        [entry("豆瓣", "a.json"), entry("精东", "b.json")],
        policy(),
        lambda slug: None,
        source_prefix=SOURCE_PREFIX,
        clean_prefix=CLEAN_PREFIX,
    )

    assert [item["name"] for item in result.payload] == ["豆瓣"]
    assert result.dropped_sources == ["精东"]


def test_a_payload_without_urls_is_returned_untouched():
    payload = {"sites": [{"name": "豆瓣"}]}
    result = build_clean_entry(
        payload, policy(), lambda slug: None, source_prefix=SOURCE_PREFIX, clean_prefix=CLEAN_PREFIX,
    )

    assert result.payload == payload
    assert (result.children, result.dropped_sources, result.removed_rows) == ({}, [], 0)


def test_a_non_mapping_entry_survives():
    result, _ = make({}, ["junk", entry("豆瓣", "a.json")])
    assert result.payload["urls"] == ["junk", entry("豆瓣", "a.json")]


# ---------------------------------------------------------------------------
# the publishing path, against real files
# ---------------------------------------------------------------------------
def make_builder(tmp_path: Path, clean: dict | None = None):
    cfg = load_config(
        explicit_root=ROOT,
        overrides={
            "app": {
                "data_dir": str(tmp_path / "data"),
                "db_path": str(tmp_path / "data" / "monitor.db"),
                "dist_dir": str(tmp_path / "dist"),
            },
            "output": {
                "min_sources": 1,
                "observation_days": 0,
                "mirror": {"enabled": True, "public_base": BASE, "alt_base": "off"},
                "quality_gate": {"enabled": False},
                "clean": dict(clean or {}),
            },
        },
    )
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    return cfg, store


def seed_dist(tmp_path: Path, entries: list, configs: dict) -> Path:
    """Write an already-published entry plus the mirrored configs it names."""
    dist = tmp_path / "dist"
    sources = dist / SOURCES_DIR
    sources.mkdir(parents=True, exist_ok=True)
    for slug, payload in configs.items():
        (sources / slug).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (dist / TVBOX_FILE).write_text(
        json.dumps({"version": "1.0", "generated_at": "now", "urls": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    return dist


def test_publishing_writes_the_clean_entry_and_its_children(tmp_path):
    cfg, store = make_builder(tmp_path)
    try:
        dist = seed_dist(
            tmp_path,
            [entry("豆瓣", "clean.json"), entry("精东影业", "adult.json")],
            {
                "clean.json": {"sites": [{"name": "普通", "api": "https://a/"},
                                         {"name": "🔞麻豆", "api": "https://b/"}]},
                "adult.json": {"sites": [{"name": "精东", "api": "https://c/"}]},
            },
        )
        out = ConfigBuilder(cfg, store)._publish_clean(dist / TVBOX_FILE)

        assert out == dist / "tvbox-clean.json"
        published = json.loads(out.read_text(encoding="utf-8"))
        assert published["version"] == "1.0" and published["generated_at"] == "now"
        assert [item["name"] for item in published["urls"]] == ["豆瓣"]
        assert published["urls"][0]["url"] == f"{CLEAN_PREFIX}clean.json"

        trimmed = json.loads((dist / "clean" / SOURCES_DIR / "clean.json").read_text(encoding="utf-8"))
        assert [site["name"] for site in trimmed["sites"]] == ["普通"]

        full = json.loads((dist / TVBOX_FILE).read_text(encoding="utf-8"))
        assert len(full["urls"]) == 2, "the primary entry must stay untouched"
    finally:
        store.close()


def test_the_clean_entry_never_points_at_a_full_config(tmp_path):
    cfg, store = make_builder(tmp_path)
    try:
        dist = seed_dist(
            tmp_path,
            [entry("豆瓣", "a.json")],
            {"a.json": {"sites": [{"name": "🔞x"}, {"name": "普通"}]}},
        )
        ConfigBuilder(cfg, store)._publish_clean(dist / TVBOX_FILE)

        published = json.loads((dist / "tvbox-clean.json").read_text(encoding="utf-8"))
        assert all(f"/clean/{SOURCES_DIR}/" in item["url"] for item in published["urls"])
    finally:
        store.close()


def test_stale_clean_copies_are_removed(tmp_path):
    cfg, store = make_builder(tmp_path)
    try:
        dist = seed_dist(tmp_path, [entry("豆瓣", "a.json")], {"a.json": {"sites": [{"name": "豆瓣"}]}})
        target = dist / "clean" / SOURCES_DIR
        target.mkdir(parents=True)
        (target / "gone.json").write_text("{}", encoding="utf-8")

        ConfigBuilder(cfg, store)._publish_clean(dist / TVBOX_FILE)

        assert list(target.glob("*.json")) == []
    finally:
        store.close()


def test_the_second_entry_can_be_switched_off(tmp_path):
    cfg, store = make_builder(tmp_path, clean={"enabled": False})
    try:
        dist = seed_dist(tmp_path, [entry("豆瓣", "a.json")], {"a.json": {"sites": [{"name": "🔞x"}]}})

        assert ConfigBuilder(cfg, store)._publish_clean(dist / TVBOX_FILE) is None
        assert not (dist / "tvbox-clean.json").exists()
    finally:
        store.close()


def test_a_config_with_a_bom_and_a_comment_header_is_still_filtered(tmp_path):
    """The two shapes dozens of authors ship; a strict parse would skip them."""
    cfg, store = make_builder(tmp_path)
    try:
        dist = tmp_path / "dist"
        sources = dist / SOURCES_DIR
        sources.mkdir(parents=True)
        raw = "\ufeff//以下来源于网络，仅供学习\n{\"sites\": [{\"name\": \"普通\"}, {\"name\": \"色播\"}],}\n"
        (sources / "b.json").write_bytes(raw.encode("utf-8"))
        (dist / TVBOX_FILE).write_text(
            json.dumps({"urls": [entry("豆瓣", "b.json")]}, ensure_ascii=False), encoding="utf-8",
        )

        ConfigBuilder(cfg, store)._publish_clean(dist / TVBOX_FILE)

        trimmed = json.loads((dist / "clean" / SOURCES_DIR / "b.json").read_text(encoding="utf-8"))
        assert [site["name"] for site in trimmed["sites"]] == ["普通"]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# end to end, over real sockets
# ---------------------------------------------------------------------------
def test_the_pipeline_publishes_both_entries_from_a_real_origin(tmp_path):
    with LocalSourceServer() as server:
        cfg = load_config(
            explicit_root=ROOT,
            overrides={
                "app": {
                    "data_dir": str(tmp_path / "data"),
                    "db_path": str(tmp_path / "data" / "monitor.db"),
                    "dist_dir": str(tmp_path / "dist"),
                },
                "discovery": {"enabled_adapters": ["manual"]},
                "output": {
                    "min_sources": 1,
                    "observation_days": 0,
                    "include_degraded": False,
                    "mirror": {"enabled": True, "public_base": BASE, "alt_base": "off"},
                    "clean": {"enabled": True, "block_keywords": ["麻豆"], "block_regex": []},
                },
            },
        )
        data_dir = cfg.path("app.data_dir", ensure_parent=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "candidates.json").write_text(
            json.dumps({"urls": [f"{server.base}/adultrows.json"]}), encoding="utf-8",
        )

        # the local origin is on 127.0.0.1, so the client opts into private hosts
        store = Store(cfg.path("app.db_path", ensure_parent=True))
        client = HttpClient(cfg.section("http"), allow_private=True)
        with Pipeline(cfg, store=store, client=client) as pipeline:
            report = pipeline.run(stages=["discover", "health", "build"], prune=False)

        assert report["build"]["published"] is True
        dist = cfg.path("app.dist_dir", "dist")

        main = json.loads((dist / TVBOX_FILE).read_text(encoding="utf-8"))
        assert len(main["urls"]) == 1, "the primary entry still carries the source"

        clean = json.loads((dist / "tvbox-clean.json").read_text(encoding="utf-8"))
        assert len(clean["urls"]) == 1
        url = clean["urls"][0]["url"]
        assert f"/clean/{SOURCES_DIR}/" in url
        slug = url.rsplit("/", 1)[-1]
        trimmed = json.loads((dist / "clean" / SOURCES_DIR / slug).read_text(encoding="utf-8"))
        assert [site["key"] for site in trimmed["sites"]] == ["plain"]
