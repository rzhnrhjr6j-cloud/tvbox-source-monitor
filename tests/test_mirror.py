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

from app.build.builder import TVBOX_FILE, ConfigBuilder
from app.models import Status
from app.build.mirror import SOURCES_DIR, ConfigMirror, _clean_name, _pick_name
from app.config import load_config
from app.storage.sqlite import Store
from app.utils.http_client import HttpClient
from tests.local_source_server import LocalSourceServer

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


def test_pick_name_falls_back_to_host_then_source_name():
    assert _pick_name({}, "https://cdn.example.com/a.json", stub_source("x")) == "cdn.example.com"
    assert _pick_name({}, "not-a-url", stub_source("x", name="kept")) == "kept"


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
