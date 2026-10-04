"""Playback-level publishing admission.

The real client test showed that a parseable config is not enough: most
published sources could be loaded but not played.  These tests pin the strict
gate and its rollback switch.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.build.builder import ConfigBuilder
from app.config import load_config
from app.models import ProbeResult, Source, Status
from app.storage.sqlite import Store
from app.utils.timeutil import to_iso, utcnow

ROOT = Path(__file__).resolve().parents[1]


def build_config(tmp_path: Path, *, quality_gate_enabled: bool = True):
    return load_config(
        explicit_root=ROOT,
        overrides={
            "app": {
                "db_path": str(tmp_path / "monitor.db"),
                "dist_dir": str(tmp_path / "dist"),
            },
            "output": {
                "min_sources": 1,
                "observation_days": 0,
                "max_drop_ratio": 0.70,
                "mirror": {"enabled": False},
                "quality_gate": {"enabled": quality_gate_enabled},
            },
        },
    )


def seed_source(
    store: Store,
    *,
    source_id: str = "s1",
    search_score: float = 100,
    playback_score: float = 100,
    source_type: str = "single",
    probe: ProbeResult | None = None,
) -> Source:
    source = Source(
        id=source_id,
        url=f"https://example.com/{source_id}.json",
        raw_url=f"https://example.com/{source_id}.json",
        name=source_id,
        type=source_type,
        status=Status.ACTIVE,
        score=100,
        stability_score=100,
        search_score=search_score,
        playback_score=playback_score,
        first_seen_at=to_iso(utcnow() - timedelta(days=30)),
    )
    store.upsert_source(source)
    if probe is not None:
        store.record_probes([probe])
    return source


def good_probe(source_id: str = "s1", content_type: str = "application/vnd.apple.mpegurl") -> ProbeResult:
    return ProbeResult(
        source_id=source_id,
        region="CN",
        http_ok=True,
        config_parse_success=True,
        search_success=True,
        detail_success=True,
        detail_has_playlist=True,
        playback_url_obtained=True,
        playback_probe_success=True,
        playback_content_type=content_type,
    )


def test_quality_gate_drops_source_without_playback_evidence(tmp_path):
    cfg = build_config(tmp_path)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store, search_score=100, playback_score=0, probe=None)
        eligible, _ = ConfigBuilder(cfg, store).eligible()
    finally:
        store.close()

    assert eligible == []


def test_quality_gate_rejects_html_as_playback(tmp_path):
    cfg = build_config(tmp_path)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store, probe=good_probe(content_type="text/html; charset=utf-8"))
        eligible, _ = ConfigBuilder(cfg, store).eligible()
    finally:
        store.close()

    assert eligible == []


def test_hls_playback_evidence_publishes_and_bypasses_drop_ratio(tmp_path):
    cfg = build_config(tmp_path)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store, probe=good_probe())
        builder = ConfigBuilder(cfg, store)
        previous = {"urls": [{"name": f"old-{index}", "url": f"https://old.example/{index}.json"} for index in range(10)]}
        result = builder.build(previous_output=previous)
    finally:
        store.close()

    assert result.published is True
    assert len(result.output["urls"]) == 1
    assert result.validation.checks["drop_ratio"] == 0.9


def test_disabled_quality_gate_keeps_the_old_eligibility_behavior(tmp_path):
    cfg = build_config(tmp_path, quality_gate_enabled=False)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store, search_score=0, playback_score=0, probe=None)
        eligible, _ = ConfigBuilder(cfg, store).eligible()
    finally:
        store.close()

    assert [source.id for source in eligible] == ["s1"]
