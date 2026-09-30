"""The pipeline: discover -> health -> score -> build -> validate (spec §35, §36).

Everything is bounded: concurrency is configurable, every request has a
timeout, one broken source never aborts the run, and the whole run respects a
wall-clock budget.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .alerts.engine import AlertEngine
from .build.builder import DASHBOARD_FILE, HEALTH_FILE, TVBOX_FILE, BuildResult, ConfigBuilder
from .checks.runner import CheckOptions, run_checks
from .discovery.aggregator import DiscoveryAggregator
from .logging_setup import get_logger
from .models import NodeRecord, ProbeResult, Source, SourceEvent, Status, Event
from .policy import PolicySet
from .regions.aggregator import RegionAggregator
from .regions.quorum import decide, probe_ok
from .scoring.scorer import Scorer, apply_lifecycle
from .storage.sqlite import Store
from .utils.http_client import HttpClient
from .utils.timeutil import age_days, day_key, to_iso, utcnow

LOGGER = get_logger("pipeline")

CHECKABLE_STATUSES = (
    Status.CANDIDATE,
    Status.VALIDATING,
    Status.ACTIVE,
    Status.DEGRADED,
    Status.RECOVERING,
    Status.FAILED,
)


def _mark_stage(report: "PipelineReport", stage: str) -> None:
    if stage not in report.stages:
        report.stages.append(stage)


@dataclass
class PipelineReport:
    stages: list[str] = field(default_factory=list)
    discover: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    build: dict[str, Any] = field(default_factory=dict)
    alerts: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    deadline_exceeded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "stages": self.stages,
            "discover": self.discover,
            "health": self.health,
            "build": self.build,
            "alerts": self.alerts,
            "duration_seconds": round(self.duration_seconds, 2),
            "deadline_exceeded": self.deadline_exceeded,
        }


class Pipeline:
    def __init__(self, cfg, store: Store | None = None, client: HttpClient | None = None, git_sha: str = ""):
        self.cfg = cfg
        self.store = store or Store(cfg.path("app.db_path", ensure_parent=True))
        self.client = client or HttpClient(cfg.section("http"))
        self.policy = PolicySet(cfg)
        self.scorer = Scorer(cfg, self.store)
        self.checks = CheckOptions.from_config(cfg)
        self.alerts = AlertEngine(cfg, self.store, self.client)
        self.run_timeout = float(cfg.get("app.run_timeout_seconds", 1800) or 0)
        self._started = time.monotonic()
        self.report = PipelineReport()
        self.git_sha = git_sha

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "Pipeline":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    # -- helpers -----------------------------------------------------------
    def _deadline(self) -> float:
        return self._started + self.run_timeout if self.run_timeout > 0 else float("inf")

    def _expired(self) -> bool:
        return time.monotonic() >= self._deadline()

    def _log_stage(self, stage: str, **fields: Any) -> None:
        LOGGER.info("stage finished", extra={"stage": stage, **fields})

    # -- stage 1: discovery -----------------------------------------------
    def run_discovery(self) -> dict[str, Any]:
        aggregator = DiscoveryAggregator(self.cfg, self.client, self.store, self.scorer, self.policy)
        result = aggregator.run()
        self.report.discover = result
        _mark_stage(self.report, "discover")
        return result

    # -- stage 2: health + score ------------------------------------------
    def _checkable_sources(self) -> list[Source]:
        """Every source worth probing this round (spec §11, §12).

        ``paused`` means "do not publish this source" (see
        ``ConfigBuilder.eligible``), NOT "stop probing it".  Filtering paused
        sources out here would make recovery impossible: a source suspended
        after N failures would never be probed again, so its success counter
        could never climb back up and it could never re-enter the output.

        A source that has stayed dead for longer than ``app.stale_source_days``
        is retired from probing; it stays in the database for history.
        """
        sources = self.store.list_sources(statuses=list(CHECKABLE_STATUSES))
        whitelisted = [source for source in self.store.list_sources(include_blacklisted=False)]
        seen = {source.id: source for source in sources}
        for source in whitelisted:
            seen.setdefault(source.id, source)

        stale_days = float(self.cfg.get("app.stale_source_days", 60) or 0)
        checkable: list[Source] = []
        retired = 0
        for source in seen.values():
            if source.whitelisted:
                checkable.append(source)
                continue
            if stale_days > 0 and source.status == Status.FAILED:
                dead_for = age_days(source.last_success_at or source.first_seen_at)
                if dead_for is not None and dead_for > stale_days:
                    retired += 1
                    continue
            checkable.append(source)
        if retired:
            LOGGER.info("retired stale sources from probing", extra={
                "stage": "health", "check": "stale", "retired": retired, "stale_days": stale_days,
            })
        return checkable

    def run_health(self) -> dict[str, Any]:
        local_region = str(self.cfg.get("regions.local.region") or "CN")
        local_node = str(self.cfg.get("regions.local.node_name") or "github-runner")
        tolerance = int(self.cfg.get("regions.node_failure_tolerance", 1))
        concurrency = max(1, int(self.cfg.get("http.concurrency", 20)))

        for node in [{"name": local_node, "region": local_region, "url": ""}] + list(self.cfg.get("regions.nodes") or []):
            if not isinstance(node, dict) or not node.get("name"):
                continue
            self.store.upsert_node(NodeRecord(
                name=str(node["name"]),
                region=str(node.get("region") or local_region),
                url=str(node.get("url") or ""),
                enabled=bool(node.get("enabled", True)),
                weight=float(node.get("weight", 1.0)),
            ))

        sources = self._checkable_sources()
        if not sources:
            self._log_stage("health", checked=0, note="no sources to check")
            self.report.health = {"checked": 0, "skipped": 0}
            _mark_stage(self.report, "health")
            return self.report.health

        region_cfg = self.cfg.section_default("regions")
        quorum_cfg = region_cfg.get("quorum") or {}
        aggregator = RegionAggregator(
            self.cfg,
            self.client,
            local_region=local_region,
            local_node=local_node,
            tolerant_failures=tolerance,
        )

        latest_probes: dict[str, ProbeResult] = {}
        checked = 0
        skipped = 0
        verdicts: dict[str, Any] = {}

        def work(source: Source):
            collection = aggregator.collect(
                source,
                lambda region, node: run_checks(
                    source, self.client, self.checks, region=region, node=node
                ),
                options={"layers": ["L1", "L2", "L3", "L4", "L5"]},
            )
            return source, collection

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {}
            submitted = 0
            for source in sources:
                if self._expired():
                    skipped += 1
                    continue
                futures[pool.submit(work, source)] = source
                submitted += 1
            for future in as_completed(futures):
                source = futures[future]
                try:
                    source, collection = future.result()
                except Exception as exc:  # noqa: BLE001 - never let one source kill the run
                    LOGGER.error("health task crashed", extra={
                        "stage": "health", "source_id": source.id, "check": "task", "error": str(exc)[:300],
                    })
                    skipped += 1
                    continue
                checked += 1
                verdict = decide(
                    collection.probes,
                    global_success_ratio=float(quorum_cfg.get("global_success_ratio", 0.75)),
                    region_success_ratio=float(quorum_cfg.get("region_success_ratio", 0.5)),
                    min_nodes_for_quorum=int(quorum_cfg.get("min_nodes_for_quorum", 2)),
                )
                verdicts[source.id] = verdict.to_dict()
                self._record_collection(source, collection, verdict)
                if collection.local_probe is not None:
                    latest_probes[source.id] = collection.local_probe

        self.report.health = {
            "checked": checked,
            "skipped": skipped,
            "deadline_exceeded": self._expired(),
            "verdicts": {key: value["global_status"] for key, value in verdicts.items()},
        }
        self.report.alerts = self.alerts.run_rules(self.store.list_sources(), latest_probes).to_dict()
        _mark_stage(self.report, "health")
        self._log_stage("health", checked=checked, skipped=skipped)
        return self.report.health

    def _record_collection(self, source: Source, collection, verdict) -> None:
        all_probes: list[ProbeResult] = []
        for region, probes in collection.probes.items():
            all_probes.extend(probes)

        self.store.record_probes(all_probes)
        for region in collection.probes:
            self.store.rebuild_daily_stats(source.id, region, day_key())

        source.global_status = verdict.global_status
        source.regions_ok = list(verdict.regions_ok)
        source.regions_failed = list(verdict.regions_failed)
        source.node_count = verdict.node_count

        round_ok = any(probe_ok(probe) for probe in all_probes)
        # stamp the outcome *before* scoring so the freshness component sees
        # this round's success instead of lagging one round behind
        stamp = to_iso()
        if round_ok:
            source.last_success_at = stamp
        else:
            source.last_failure_at = stamp
        breakdown = self.scorer.score_source(source, verdict)
        self.scorer.apply_scores(source, breakdown)

        if source.status == Status.CANDIDATE and round_ok:
            source.status = Status.VALIDATING

        outcome = apply_lifecycle(source, success=round_ok, scorer=self.scorer)
        self.store.upsert_source(source)
        if outcome.events:
            self.store.record_events(outcome.events)

        for probe in all_probes:
            self.store.record_event(SourceEvent(
                source_id=source.id,
                event=Event.CHECKED,
                detail=f"{probe.region}:{probe.error_stage or 'ok'} score={breakdown.total:.1f}",
                to_status=source.status,
                ts=probe.ts,
            ))

    # -- stage 3: build ----------------------------------------------------
    def run_build(self) -> dict[str, Any]:
        previous = self._read_previous()
        builder = ConfigBuilder(self.cfg, self.store, git_sha=self.git_sha)
        result = builder.build(previous_output=previous)

        if result.published:
            builder.publish(result)
        else:
            builder.record_blocked(result)
            self.alerts.build_blocked(result.blocked_reason or "validation failed", detail=json.dumps(result.validation.to_dict()))
            if previous is not None:
                self.alerts.output_rollback(result.blocked_reason or "validation failed")
            self.report.health.setdefault("build_blocked", result.blocked_reason)

        self.report.build = result.to_dict()
        _mark_stage(self.report, "build")
        self._log_stage("build", published=result.published, items=result.build.source_count)
        return self.report.build

    def _read_previous(self) -> Any | None:
        path = self.cfg.path("app.dist_dir", "dist") / TVBOX_FILE
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("previous tvbox.json unreadable; treating as absent",
                           extra={"stage": "build", "check": "read_previous", "error": str(exc)[:200]})
            return None

    # -- maintenance -------------------------------------------------------
    def prune(self) -> dict[str, int]:
        keep_days = float(self.cfg.get("app.probe_retention_days", 120) or 120)
        removed = self.store.prune_probes(keep_days)
        self.store.checkpoint()
        if removed:
            LOGGER.info("pruned old probes", extra={"stage": "prune", "removed": removed, "keep_days": keep_days})
        return {"probes_removed": removed}

    # -- top level ---------------------------------------------------------
    def run(self, stages: Iterable[str] = ("discover", "health", "build"), prune: bool = True) -> dict[str, Any]:
        stages = list(stages)
        try:
            if "discover" in stages:
                self.run_discovery()
            if "health" in stages:
                self.run_health()
            if "build" in stages:
                self.run_build()
            if prune:
                self.prune()
        finally:
            self.report.duration_seconds = time.monotonic() - self._started
            self.report.deadline_exceeded = self._expired()
            self.store.set_meta("last_run_at", to_iso())
            self.store.checkpoint()
        return self.report.to_dict()

    def close(self) -> None:
        self.client.close()
        self.store.close()
