"""L6 Android jar gate.

L1-L5 run over HTTP on a runner and cannot see the ``DexClassLoader`` step a
TVBox client performs for every ``csp_*`` / jar site.  These tests pin the
local-verdict gate that stands in for that step.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.build.builder import ConfigBuilder
from app.checks.jar_check import AndroidRecord, has_jar_sites, load_evidence, write_evidence
from app.config import load_config
from app.models import ProbeResult, Source, Status
from app.storage.sqlite import Store
from app.utils.timeutil import to_iso, utcnow

ROOT = Path(__file__).resolve().parents[1]


class _Result:
    def __init__(self, text: str):
        self.text = text
        self.ok = True


class FakeClient:
    connect_timeout = 5
    read_timeout = 10

    def __init__(self, payload: dict):
        self.body = json.dumps(payload)

    def get_json(self, url, timeout=None, max_bytes=None):
        return _Result(self.body)


class UrlClient(FakeClient):
    def __init__(self, payloads: dict[str, dict]):
        self.payloads = payloads

    def get_json(self, url, timeout=None, max_bytes=None):
        return _Result(json.dumps(self.payloads[url]))


JAR_CONFIG = {
    "sites": [
        {"key": "jin", "name": "jingdong", "type": 3, "api": "csp_AppRJ", "ext": "https://x/j.jar"},
    ]
}
CMS_CONFIG = {
    "sites": [
        {"key": "cms", "name": "cms", "type": 1, "api": "https://cms.example.com/api.php/provide/vod"},
    ]
}


def build_config(
    tmp_path: Path,
    *,
    require_android_jar: bool,
    android_satisfies_playback: bool = True,
    android_satisfies_search: bool = True,
):
    return load_config(
        explicit_root=ROOT,
        overrides={
            "app": {
                "db_path": str(tmp_path / "monitor.db"),
                "dist_dir": str(tmp_path / "dist"),
                "data_dir": str(tmp_path / "data"),
            },
            "output": {
                "min_sources": 1,
                "observation_days": 0,
                "mirror": {"enabled": False},
                "quality_gate": {
                    "enabled": True,
                    "require_android_jar": require_android_jar,
                    "android_satisfies_playback": android_satisfies_playback,
                    "android_satisfies_search": android_satisfies_search,
                    "content_overlap_threshold": 0,
                },
            },
        },
    )


def seed_source(
    store: Store,
    source_id: str = "s1",
    *,
    status: Status = Status.ACTIVE,
    search_success: bool = True,
    playback_url_obtained: bool = True,
    playback_probe_success: bool = True,
    playback_content_type: str = "application/vnd.apple.mpegurl",
) -> Source:
    source = Source(
        id=source_id,
        url=f"https://example.com/{source_id}.json",
        raw_url=f"https://example.com/{source_id}.json",
        name=source_id,
        type="single",
        status=status,
        score=100,
        stability_score=100,
        first_seen_at=to_iso(utcnow() - timedelta(days=30)),
    )
    probe = ProbeResult(
        source_id=source_id,
        region="CN",
        http_ok=True,
        config_parse_success=True,
        search_success=search_success,
        detail_success=True,
        detail_has_playlist=True,
        playback_url_obtained=playback_url_obtained,
        playback_probe_success=playback_probe_success,
        playback_content_type=playback_content_type,
    )
    store.upsert_source(source)
    store.record_probes([probe])
    return source


def record(tmp_path: Path, source_id: str, *, loadable: int, playable: int, days_old: float = 0) -> None:
    write_evidence(
        tmp_path / "data" / "android_verified.json",
        [
            AndroidRecord(
                source_id=source_id,
                config_url=f"https://example.com/{source_id}.json",
                verified_at=to_iso(utcnow() - timedelta(days=days_old)),
                site_count=40,
                loadable_count=loadable,
                playable_count=playable,
            )
        ],
    )


def test_has_jar_sites_detects_csp_and_jar_spider():
    assert has_jar_sites(JAR_CONFIG) is True
    assert has_jar_sites(CMS_CONFIG) is False
    assert has_jar_sites({"spider": "https://x/spider.jar", "sites": []}) is True


def test_evidence_round_trips(tmp_path):
    record(tmp_path, "s1", loadable=40, playable=2)
    evidence = load_evidence(tmp_path / "data" / "android_verified.json")
    assert evidence["s1"].loadable_count == 40
    assert evidence["s1"].playable_count == 2


def test_narrow_rerun_cannot_overwrite_stronger_verdict(tmp_path):
    # 全量跑出过 playable=1，之后只抽前 20 站的跑出 0，不能把强证据冲掉。
    record(tmp_path, "s1", loadable=59, playable=1)
    record(tmp_path, "s1", loadable=20, playable=0)
    evidence = load_evidence(tmp_path / "data" / "android_verified.json")
    assert evidence["s1"].loadable_count == 59
    assert evidence["s1"].playable_count == 1


def test_stronger_rerun_replaces_weaker_verdict(tmp_path):
    record(tmp_path, "s1", loadable=20, playable=0)
    record(tmp_path, "s1", loadable=59, playable=1)
    evidence = load_evidence(tmp_path / "data" / "android_verified.json")
    assert evidence["s1"].loadable_count == 59
    assert evidence["s1"].playable_count == 1


def test_same_playable_more_loadable_replaces_verdict(tmp_path):
    record(tmp_path, "s1", loadable=20, playable=1)
    record(tmp_path, "s1", loadable=59, playable=1)
    evidence = load_evidence(tmp_path / "data" / "android_verified.json")
    assert evidence["s1"].loadable_count == 59


def test_equal_verdict_refreshes_timestamp(tmp_path):
    record(tmp_path, "s1", loadable=59, playable=1, days_old=10)
    record(tmp_path, "s1", loadable=59, playable=1, days_old=0)
    evidence = load_evidence(tmp_path / "data" / "android_verified.json")
    verified = datetime.fromisoformat(evidence["s1"].verified_at)
    if verified.tzinfo is None:
        verified = verified.replace(tzinfo=timezone.utc)
    assert abs((utcnow() - verified).total_seconds()) < 60
    assert evidence["s1"].loadable_count == 59


def test_jar_config_without_evidence_is_dropped_when_gate_on(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=True)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store)
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(JAR_CONFIG)).eligible()
    finally:
        store.close()
    assert eligible == []


def test_jar_config_with_fresh_playable_evidence_is_kept(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=True)
    record(tmp_path, "s1", loadable=40, playable=2)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store)
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(JAR_CONFIG)).eligible()
    finally:
        store.close()
    assert [source.id for source in eligible] == ["s1"]


def test_jar_config_with_load_only_evidence_is_dropped_when_playable_required(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=True)
    record(tmp_path, "s1", loadable=40, playable=0)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store)
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(JAR_CONFIG)).eligible()
    finally:
        store.close()
    assert eligible == []


def test_stale_evidence_is_dropped(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=True)
    record(tmp_path, "s1", loadable=40, playable=2, days_old=45)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store)
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(JAR_CONFIG)).eligible()
    finally:
        store.close()
    assert eligible == []


def test_cms_config_ignores_android_gate(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=True)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store)
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(CMS_CONFIG)).eligible()
    finally:
        store.close()
    assert [source.id for source in eligible] == ["s1"]


def test_gate_off_keeps_jar_config_without_evidence(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=False)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store)
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(JAR_CONFIG)).eligible()
    finally:
        store.close()
    assert [source.id for source in eligible] == ["s1"]


def test_android_playable_substitutes_failed_l5_playback(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=True)
    record(tmp_path, "s1", loadable=40, playable=1)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store, playback_probe_success=False, playback_content_type="")
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(JAR_CONFIG)).eligible()
    finally:
        store.close()
    assert [source.id for source in eligible] == ["s1"]


def test_android_playable_does_not_substitute_when_option_off(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=True, android_satisfies_playback=False)
    record(tmp_path, "s1", loadable=40, playable=1)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store, playback_probe_success=False, playback_content_type="")
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(JAR_CONFIG)).eligible()
    finally:
        store.close()
    assert eligible == []


def test_android_playable_does_not_substitute_for_cms_playback(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=True)
    record(tmp_path, "s1", loadable=40, playable=1)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store, playback_probe_success=False, playback_content_type="")
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(CMS_CONFIG)).eligible()
    finally:
        store.close()
    assert eligible == []


def test_android_playable_substitutes_failed_search(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=True)
    record(tmp_path, "s1", loadable=40, playable=1)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store, search_success=False)
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(JAR_CONFIG)).eligible()
    finally:
        store.close()
    assert [source.id for source in eligible] == ["s1"]


def test_android_playable_does_not_substitute_search_when_option_off(tmp_path):
    cfg = build_config(
        tmp_path,
        require_android_jar=True,
        android_satisfies_search=False,
    )
    record(tmp_path, "s1", loadable=40, playable=1)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store, search_success=False)
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(JAR_CONFIG)).eligible()
    finally:
        store.close()
    assert eligible == []


def test_android_playable_does_not_substitute_search_for_cms(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=True)
    record(tmp_path, "s1", loadable=40, playable=1)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store, search_success=False)
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(CMS_CONFIG)).eligible()
    finally:
        store.close()
    assert eligible == []


def test_degraded_jar_with_android_playback_is_still_a_candidate(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=True)
    record(tmp_path, "s1", loadable=40, playable=1)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store, status=Status.DEGRADED, search_success=False)
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(JAR_CONFIG)).eligible()
    finally:
        store.close()
    assert [source.id for source in eligible] == ["s1"]


def test_degraded_jar_without_android_playback_stays_filtered(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=True)
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store, status=Status.DEGRADED, search_success=False)
        eligible, _ = ConfigBuilder(cfg, store, client=FakeClient(JAR_CONFIG)).eligible()
    finally:
        store.close()
    assert eligible == []


def test_dedup_does_not_merge_distinct_csp_keys(tmp_path):
    cfg = build_config(tmp_path, require_android_jar=False)
    first = {
        "sites": [
            {"key": "a", "name": "alpha", "type": 3, "api": "csp_Alpha"},
            {"key": "cms", "name": "cms", "type": 1, "api": "https://cms.example.com/api"},
        ]
    }
    second = {
        "sites": [
            {"key": "b", "name": "beta", "type": 3, "api": "csp_Beta"},
            {"key": "cms", "name": "cms", "type": 1, "api": "https://cms.example.com/api"},
        ]
    }
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        seed_source(store, "s1")
        seed_source(store, "s2")
        payloads = {
            "https://example.com/s1.json": first,
            "https://example.com/s2.json": second,
        }
        eligible, _ = ConfigBuilder(cfg, store, client=UrlClient(payloads)).eligible()
    finally:
        store.close()
    assert sorted(source.id for source in eligible) == ["s1", "s2"]
