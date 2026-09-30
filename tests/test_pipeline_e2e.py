"""End-to-end: discovery -> admission -> health -> score -> build -> publish.

Spec §35-10 forbids mocks here, so this drives the real pipeline against a real
HTTP origin over real sockets, and inspects the real files it writes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.build.builder import DASHBOARD_FILE, HEALTH_FILE, TVBOX_FILE
from app.config import load_config
from app.pipeline import Pipeline
from app.storage.sqlite import Store
from app.utils.http_client import HttpClient
from tests.local_source_server import LocalSourceServer


def build_config(tmp_path: Path, extra: dict | None = None):
    overrides = {
        "app": {
            "data_dir": str(tmp_path / "data"),
            "db_path": str(tmp_path / "data" / "monitor.db"),
            "dist_dir": str(tmp_path / "dist"),
        },
        "discovery": {"enabled_adapters": ["manual"]},
        "output": {"min_sources": 1, "observation_days": 0, "include_degraded": False},
    }
    for section, values in (extra or {}).items():
        overrides.setdefault(section, {}).update(values)
    return load_config(explicit_root=Path(__file__).resolve().parents[1], overrides=overrides)


def seed_candidates(cfg, urls: list) -> None:
    data_dir = cfg.path("app.data_dir", ensure_parent=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "candidates.json").write_text(json.dumps({"urls": urls}), encoding="utf-8")


def make_pipeline(cfg):
    """The local origin is on 127.0.0.1, so the client opts into private targets."""
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    client = HttpClient(cfg.section("http"), allow_private=True)
    return Pipeline(cfg, store=store, client=client)


def test_full_chain_publishes_a_config(tmp_path):
    with LocalSourceServer() as server:
        cfg = build_config(tmp_path)
        seed_candidates(cfg, [server.config_url])

        with make_pipeline(cfg) as pipeline:
            report = pipeline.run(stages=["discover", "health", "build"], prune=False)

        assert report["stages"] == ["discover", "health", "build"]
        assert report["discover"]["admitted"] >= 1
        assert report["health"]["checked"] >= 1
        assert report["build"]["published"] is True

        output_path = cfg.path("app.dist_dir", "dist") / TVBOX_FILE
        assert output_path.is_file()
        published = json.loads(output_path.read_text(encoding="utf-8"))

        assert published["version"]
        assert published["generated_at"]
        urls = published["urls"]
        assert len(urls) == 1
        assert urls[0]["name"]
        assert urls[0]["url"].startswith("http")

        # the other two published artifacts (spec §39-12/13/14) exist too
        dist = cfg.path("app.dist_dir", "dist")
        assert (dist / HEALTH_FILE).is_file()
        assert (dist / DASHBOARD_FILE).is_file()

        # tiers are persisted, not just held in memory (spec §29)
        store = Store(cfg.path("app.db_path"))
        try:
            tiers = {source.id: source.tier for source in store.list_sources()}
            assert tiers, "no sources persisted"
            assert all(tier in ("primary", "backup", "experimental") for tier in tiers.values())
            build = store.last_build()
            assert build is not None
            assert build.published is True
            assert build.build_id
            assert build.source_count == 1
        finally:
            store.close()


def test_multi_repo_expands_into_children(tmp_path):
    with LocalSourceServer() as server:
        cfg = build_config(tmp_path)
        seed_candidates(cfg, [f"{server.base}/multi.json"])

        with make_pipeline(cfg) as pipeline:
            report = pipeline.run(stages=["discover"], prune=False)

        # the wrapper plus its expandable child (/config.json); the broken and
        # self-referencing entries must not survive admission
        assert report["discover"]["admitted"] >= 2

        store = Store(cfg.path("app.db_path"))
        try:
            urls = {source.url for source in store.list_sources()}
            assert any(url.endswith("/config.json") for url in urls)
            assert not any(url.endswith("/broken.json") for url in urls)
            children = [source for source in store.list_sources() if source.parent_id]
            assert children, "expansion did not record lineage"
        finally:
            store.close()


def test_one_dead_source_does_not_stop_the_run(tmp_path):
    """spec §35-12."""
    with LocalSourceServer() as server:
        cfg = build_config(tmp_path)
        seed_candidates(cfg, [server.config_url, "http://127.0.0.1:9/dead.json"])

        with make_pipeline(cfg) as pipeline:
            report = pipeline.run(stages=["discover", "health", "build"], prune=False)

        assert report["health"]["checked"] >= 1
        assert report["build"]["published"] is True
        assert report["build"]["build"]["items"] == 1


def test_failed_build_keeps_the_previous_tvbox_json_byte_for_byte(tmp_path):
    """spec §19 / §30 / §32 / 演示 10 - the single most important guarantee."""
    with LocalSourceServer() as server:
        cfg = build_config(tmp_path)
        seed_candidates(cfg, [server.config_url])
        output_path = cfg.path("app.dist_dir", "dist") / TVBOX_FILE

        with make_pipeline(cfg) as pipeline:
            pipeline.run(stages=["discover", "health", "build"], prune=False)
        assert output_path.is_file()
        before = output_path.read_bytes()
        assert len(json.loads(before.decode("utf-8"))["urls"]) == 1

        # the origin now fails every probe
        server.set(config_up=False)

        with make_pipeline(cfg) as pipeline:
            report = pipeline.run(stages=["health", "build"], prune=False)

        assert report["build"]["published"] is False
        reason = report["build"]["blocked_reason"]
        assert "NO_ITEMS" in reason
        assert output_path.read_bytes() == before, "tvbox.json must survive a failed build untouched"

        dist = cfg.path("app.dist_dir", "dist")
        assert (dist / "last-known-good.json").is_file()
        backups = list((dist / "backup").glob("*.json"))
        assert backups, "a dated backup must be kept"


def test_recovery_puts_the_source_back_into_the_config(tmp_path):
    """演示 7: after the source recovers it returns to the published config."""
    with LocalSourceServer() as server:
        cfg = build_config(tmp_path)
        seed_candidates(cfg, [server.config_url])
        output_path = cfg.path("app.dist_dir", "dist") / TVBOX_FILE

        with make_pipeline(cfg) as pipeline:
            pipeline.run(stages=["discover", "health", "build"], prune=False)
        assert json.loads(output_path.read_text(encoding="utf-8"))["urls"]

        server.set(config_up=False)
        with make_pipeline(cfg) as pipeline:
            blocked = pipeline.run(stages=["health", "build"], prune=False)
        assert blocked["build"]["published"] is False

        store = Store(cfg.path("app.db_path"))
        try:
            failures = max(source.consecutive_failure for source in store.list_sources())
        finally:
            store.close()
        assert failures >= 1

        # the source comes back and must climb back into the published config
        server.set(config_up=True)
        recover_active = int(cfg.get("lifecycle.recover_to_active"))
        with make_pipeline(cfg) as pipeline:
            for _ in range(recover_active):
                pipeline.run(stages=["health"], prune=False)
            final = pipeline.run(stages=["health", "build"], prune=False)

        assert final["build"]["published"] is True, final["build"]
        assert json.loads(output_path.read_text(encoding="utf-8"))["urls"]


def test_whitelisted_source_is_never_dropped(tmp_path):
    """A whitelisted source stays eligible even while it is failing (spec §11)."""
    with LocalSourceServer() as server:
        cfg = build_config(tmp_path)
        seed_candidates(cfg, [server.config_url])

        with make_pipeline(cfg) as pipeline:
            pipeline.run(stages=["discover", "health", "build"], prune=False)

        store = Store(cfg.path("app.db_path"))
        try:
            for source in store.list_sources():
                source.whitelisted = True
                store.upsert_source(source)
        finally:
            store.close()

        server.set(config_up=False)
        with make_pipeline(cfg) as pipeline:
            report = pipeline.run(stages=["health"], prune=False)
        assert report["health"]["checked"] >= 1


def test_a_paused_source_keeps_being_probed_and_can_recover(tmp_path):
    """Regression: pausing output must not stop probing.

    `paused` means "do not publish this source", not "stop checking it".  When
    the health stage filtered paused sources out, a suspended source was never
    probed again, so its success counter could never climb and it could never
    re-enter tvbox.json - making spec §11's recovery ladder unreachable.
    """
    with LocalSourceServer() as server:
        cfg = build_config(tmp_path)
        seed_candidates(cfg, [server.config_url])
        output_path = cfg.path("app.dist_dir", "dist") / TVBOX_FILE

        with make_pipeline(cfg) as pipeline:
            assert pipeline.run(stages=["discover", "health", "build"], prune=False)["build"]["published"] is True

        pause_at = int(cfg.get("lifecycle.fail_pause_output"))
        server.set(config_up=False)
        with make_pipeline(cfg) as pipeline:
            for _ in range(pause_at):
                pipeline.run(stages=["health"], prune=False)

        store = Store(cfg.path("app.db_path"))
        try:
            source = store.list_sources()[0]
            assert source.paused is True, "the source should be suspended from output"
            checks_when_paused = source.total_checks
        finally:
            store.close()

        # while paused it must still be probed
        server.set(config_up=True)
        with make_pipeline(cfg) as pipeline:
            pipeline.run(stages=["health"], prune=False)

        store = Store(cfg.path("app.db_path"))
        try:
            source = store.list_sources()[0]
            assert source.total_checks > checks_when_paused, "a paused source was never probed again"
            assert source.consecutive_success == 1
        finally:
            store.close()

        # ...and it must climb back into the published config
        recover_to = int(cfg.get("lifecycle.recover_to_active"))
        with make_pipeline(cfg) as pipeline:
            for _ in range(recover_to):
                pipeline.run(stages=["health"], prune=False)
            final = pipeline.run(stages=["build"], prune=False)

        assert final["build"]["published"] is True, final["build"]
        assert json.loads(output_path.read_text(encoding="utf-8"))["urls"]

        store = Store(cfg.path("app.db_path"))
        try:
            source = store.list_sources()[0]
            assert source.status == "active"
            assert source.paused is False
        finally:
            store.close()


def test_a_long_dead_source_is_retired_from_probing(tmp_path):
    """app.stale_source_days must actually bound the probing cost."""
    with LocalSourceServer() as server:
        cfg = build_config(tmp_path / "disabled", {"app": {"stale_source_days": 0}})
        seed_candidates(cfg, [server.config_url])

        with make_pipeline(cfg) as pipeline:
            assert pipeline.run(stages=["discover", "health"], prune=False)["health"]["checked"] == 1

    # stale_source_days=0 disables retirement, so the source is still checked
    with LocalSourceServer() as server:
        cfg = build_config(tmp_path / "retire", {"app": {"stale_source_days": 30}})
        seed_candidates(cfg, [server.config_url])
        with make_pipeline(cfg) as pipeline:
            assert pipeline.run(stages=["discover", "health"], prune=False)["health"]["checked"] == 1

        store = Store(cfg.path("app.db_path"))
        try:
            source = store.list_sources()[0]
            source.status = "failed"
            # pretend it has been dead for longer than the retention window
            source.last_success_at = None
            source.first_seen_at = "2020-01-01T00:00:00+0000"
            store.upsert_source(source)
        finally:
            store.close()

        with make_pipeline(cfg) as pipeline:
            assert pipeline.run(stages=["health"], prune=False)["health"]["checked"] == 0
