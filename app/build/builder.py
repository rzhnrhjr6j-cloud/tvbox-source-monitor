"""Config builder (spec §18, §29, §31).

* only ACTIVE sources are eligible (DEGRADED optionally, FAILED never)
* internal tiering PRIMARY / BACKUP / EXPERIMENTAL is always computed
* the published shape is config-driven, because spec §18 explicitly forbids
  guessing the 影视仓 6.1.8 field layout from blog posts
* publishing is atomic (tmp file + os.replace) and keeps the last-known-good
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..logging_setup import get_logger
from ..models import BuildRecord, Event, Source, SourceEvent, Status, Tier
from ..utils.http_client import HttpClient
from ..utils.timeutil import age_days, to_iso, utcnow
from .mirror import SOURCES_DIR, ConfigMirror, MirrorPlan
from .validator import ValidationResult, validate_output

LOGGER = get_logger("build")

TVBOX_FILE = "tvbox.json"
ALT_FILE = "tvbox-cdn.json"
# where the alternate host gets its own copy of the per-source configs
ALT_DIR = "cdn"
HEALTH_FILE = "health.json"
DASHBOARD_FILE = "dashboard.json"
LAST_GOOD_FILE = "last-known-good.json"
BACKUP_DIR = "backup"


@dataclass
class BuildResult:
    build: BuildRecord
    output: dict[str, Any] | list[Any]
    validation: ValidationResult
    published: bool = False
    blocked_reason: str = ""
    output_path: Path | None = None
    health: dict[str, Any] = field(default_factory=dict)
    dashboard: dict[str, Any] = field(default_factory=dict)
    tiers: dict[str, list[str]] = field(default_factory=dict)
    mirror: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "blocked_reason": self.blocked_reason,
            "validation": self.validation.to_dict(),
            "build": {
                "build_id": self.build.build_id,
                "active": self.build.active_count,
                "degraded": self.build.degraded_count,
                "failed": self.build.failed_count,
                "new": self.build.new_count,
                "recovered": self.build.recovered_count,
                "removed": self.build.removed_count,
                "items": self.build.source_count,
                "primary": self.build.primary_count,
                "backup": self.build.backup_count,
                "experimental": self.build.experimental_count,
            },
            "tiers": {name: len(ids) for name, ids in self.tiers.items()},
        }


class ConfigBuilder:
    def __init__(self, cfg, store, git_sha: str = "", client: HttpClient | None = None):
        self.cfg = cfg
        self.store = store
        self.output_cfg = cfg.section("output")
        self.dist_dir = cfg.path("app.dist_dir", "dist")
        self.git_sha = git_sha
        self.client = client or HttpClient(cfg.section("http"))
        self.mirror = ConfigMirror(cfg, self.dist_dir, self.client)

    # -- selection ---------------------------------------------------------
    def eligible(self) -> tuple[list[Source], dict[str, int]]:
        statuses = [Status.ACTIVE]
        if bool(self.output_cfg.get("include_degraded", False)):
            statuses.extend([Status.DEGRADED, Status.RECOVERING])
        candidates = self.store.list_sources(statuses=statuses)
        counts = self.store.count_by_status()
        eligible = [
            source for source in candidates
            if not source.paused and self._passes_quality_gate(source)
        ]
        rejected = len(candidates) - len(eligible)
        if rejected:
            LOGGER.info("quality gate filtered sources", extra={
                "stage": "build",
                "check": "quality_gate",
                "candidates": len(candidates),
                "eligible": len(eligible),
                "rejected": rejected,
            })
        return eligible, counts

    def _passes_quality_gate(self, source: Source) -> bool:
        gate = self.output_cfg.get("quality_gate") or {}
        if not isinstance(gate, dict) or not bool(gate.get("enabled", False)):
            return True
        if not bool(gate.get("allow_multi", False)) and str(source.type or "").lower() == "multi":
            return False

        probes = self.store.probes_for(
            source.id,
            days=float(gate.get("probe_max_age_days", 7)),
            limit=1,
        )
        if not probes:
            return False
        latest = probes[0]
        if bool(gate.get("require_search", True)):
            latest_search_score = 100 if latest.search_success else 0
            if not _score_at_least(latest_search_score, gate.get("min_search_score", 100)):
                return False
        if bool(gate.get("require_playback", True)):
            latest_playback_score = (
                100 if latest.playback_url_obtained and latest.playback_probe_success else 0
            )
            if not _score_at_least(latest_playback_score, gate.get("min_playback_score", 100)):
                return False
            accepted = gate.get("accepted_playback_content_types") or []
            if not _content_type_allowed(latest.playback_content_type, accepted):
                return False
        return True

    def assign_tiers(self, sources: Iterable[Source]) -> dict[str, list[Source]]:
        observation_days = float(self.output_cfg.get("observation_days", 7))
        primary_min = float(self.output_cfg.get("min_score_primary", 85))
        primary_stability = float(self.output_cfg.get("min_stability_primary", 80))
        backup_min = float(self.output_cfg.get("min_score_backup", 70))
        include_experimental = bool(self.output_cfg.get("include_experimental", True))

        tiers: dict[str, list[Source]] = {Tier.PRIMARY: [], Tier.BACKUP: [], Tier.EXPERIMENTAL: []}
        for source in sources:
            age = age_days(source.first_seen_at)
            is_new = age is not None and age < observation_days
            if is_new:
                # spec §28: a source we met yesterday is not allowed to look
                # like a proven one, no matter how good today's probe was
                if include_experimental:
                    tiers[Tier.EXPERIMENTAL].append(source)
                source.tier = Tier.EXPERIMENTAL
                continue
            if source.whitelisted and source.status == Status.ACTIVE:
                tiers[Tier.PRIMARY].append(source)
                source.tier = Tier.PRIMARY
            elif source.score >= primary_min and source.stability_score >= primary_stability:
                tiers[Tier.PRIMARY].append(source)
                source.tier = Tier.PRIMARY
            elif source.score >= backup_min:
                tiers[Tier.BACKUP].append(source)
                source.tier = Tier.BACKUP
            elif include_experimental and source.status != Status.FAILED:
                tiers[Tier.EXPERIMENTAL].append(source)
                source.tier = Tier.EXPERIMENTAL
            else:
                source.tier = Tier.NONE
        for bucket in tiers.values():
            bucket.sort(key=lambda item: (-item.score, item.name or item.id))
        return tiers

    # -- rendering ---------------------------------------------------------
    def render(
        self,
        tiers: dict[str, list[Source]],
        url_map: dict[str, str] | None = None,
        name_map: dict[str, str] | None = None,
    ) -> dict[str, Any] | list[Any]:
        sources = tiers[Tier.PRIMARY] + tiers[Tier.BACKUP] + tiers[Tier.EXPERIMENTAL]
        sort_by = str(self.output_cfg.get("sort_by", "score"))
        if sort_by == "name":
            sources = sorted(sources, key=lambda item: (item.name or item.id))
        name_of, url_of = _naming(url_map, name_map)

        template = self.output_cfg.get("template")
        if isinstance(template, dict) and template:
            return self._render_template(template, sources, url_map, name_map)

        fmt = str(self.output_cfg.get("format", "multi"))
        if fmt == "multi":
            return {
                "version": str(self.cfg.get("app.version", "1.0")),
                "generated_at": to_iso(),
                "urls": [{"name": name_of(source), "url": url_of(source)} for source in sources],
            }
        if fmt == "sites":
            return {
                "sites": [
                    {
                        "key": source.id[:16],
                        "name": name_of(source),
                        "type": 3,
                        "api": url_of(source),
                        "searchable": 1,
                        "quickSearch": 1,
                        "filterable": 1,
                    }
                    for source in sources
                ]
            }
        if fmt == "single":
            return {
                "sites": [
                    {
                        "key": source.id[:16],
                        "name": name_of(source),
                        "type": 3,
                        "api": url_of(source),
                        "searchable": 1,
                        "quickSearch": 1,
                        "filterable": 1,
                    }
                    for source in sources
                ],
                "lives": [],
                "parses": [],
            }
        raise ValueError(f"unsupported output.format: {fmt}")

    def _render_template(
        self,
        template: dict[str, Any],
        sources: list[Source],
        url_map: dict[str, str] | None = None,
        name_map: dict[str, str] | None = None,
    ) -> Any:
        """Delegate to the tiny explicit expander (see _expand_template)."""
        return _expand_template(template, sources, url_map, name_map)

    # -- build -------------------------------------------------------------
    def build(self, previous_output: Any | None = None) -> BuildResult:
        eligible, counts = self.eligible()
        tiers = self.assign_tiers(eligible)

        # spec §18 / §37: re-serve every config from our own Pages host.  The
        # probes run on GitHub's runners and therefore cannot tell that
        # raw.githubusercontent.com is unreachable for the client in mainland
        # China; mirroring is what makes the published URLs actually loadable.
        plan = self.mirror.prepare(
            [source for bucket in tiers.values() for source in bucket]
        )
        if plan.dropped:
            for bucket in tiers.values():
                bucket[:] = [source for source in bucket if source.id not in plan.dropped]
            LOGGER.warning("sources dropped: not mirrorable", extra={
                "stage": "build", "check": "mirror", "count": len(plan.dropped)})
        url_map = {source_id: entry.url for source_id, entry in plan.entries.items()}
        name_map = {source_id: entry.name for source_id, entry in plan.entries.items()}
        output = self.render(tiers, url_map, name_map)

        items = count_output_items(output)
        build_id = f"{utcnow().strftime('%Y%m%d-%H%M%S')}"
        try:
            previous_ids = set(json.loads(self.store.get_meta("published_source_ids") or "[]"))
        except (TypeError, ValueError):
            previous_ids = set()

        new_ids = {source.id for source in tiers[Tier.PRIMARY] + tiers[Tier.BACKUP] + tiers[Tier.EXPERIMENTAL]}
        new_count = len(new_ids - previous_ids)
        removed_count = len(previous_ids - new_ids)
        recovered = sum(1 for source in eligible if source.consecutive_success == int(self.cfg.get("lifecycle.recover_to_active", 5)))

        validation = validate_output(
            output,
            previous=previous_output,
            min_sources=int(self.output_cfg.get("min_sources", 3)),
            max_drop_ratio=self._effective_max_drop_ratio(),
        )

        record = BuildRecord(
            build_id=build_id,
            git_sha=self.git_sha,
            active_count=counts.get(Status.ACTIVE, 0),
            degraded_count=counts.get(Status.DEGRADED, 0),
            failed_count=counts.get(Status.FAILED, 0),
            new_count=new_count,
            recovered_count=recovered,
            removed_count=removed_count,
            source_count=items,
            primary_count=len(tiers[Tier.PRIMARY]),
            backup_count=len(tiers[Tier.BACKUP]),
            experimental_count=len(tiers[Tier.EXPERIMENTAL]),
        )

        result = BuildResult(
            build=record,
            output=output,
            validation=validation,
            tiers={name: [source.id for source in bucket] for name, bucket in tiers.items()},
            mirror=plan,
        )
        if not validation.ok:
            record.published = False
            record.fallback = True
            record.blocked_reason = "; ".join(validation.reasons)[:300]
            result.blocked_reason = record.blocked_reason
            LOGGER.error("build blocked by the safety valve",
                         extra={"stage": "build", "check": "validate", "error": record.blocked_reason})
            return result

        record.published = True
        record.output_sha256 = hashlib.sha256(
            json.dumps(output, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        result.published = True
        result.health = self._health(result, tiers, counts)
        result.dashboard = self._dashboard(result, tiers, counts)
        return result

    def _effective_max_drop_ratio(self) -> float:
        gate = self.output_cfg.get("quality_gate") or {}
        if (
            isinstance(gate, dict)
            and bool(gate.get("enabled", False))
            and bool(gate.get("bypass_drop_ratio", False))
        ):
            return 1.0
        return float(self.output_cfg.get("max_drop_ratio", 0.70))

    def publish(self, result: BuildResult) -> Path | None:
        """Atomic publish.  Never call this when ``result.published`` is False."""
        if not result.published:
            return None
        self.dist_dir.mkdir(parents=True, exist_ok=True)
        path = self.dist_dir / TVBOX_FILE

        if path.is_file():
            self._backup(path)

        if isinstance(result.mirror, MirrorPlan) and result.mirror.enabled:
            self.mirror.write(result.mirror)

        _atomic_write_json(path, result.output, pretty=bool(self.output_cfg.get("pretty", True)))
        self._publish_alt(path)
        _atomic_write_json(self.dist_dir / HEALTH_FILE, result.health, pretty=True)
        _atomic_write_json(self.dist_dir / DASHBOARD_FILE, result.dashboard, pretty=True)

        # spec §19 requires current + backup-YYYYMMDD.json to exist at all
        # times.  _backup() above only archives the *outgoing* version, so the
        # very first publish would leave no dated backup at all - and that is
        # precisely the moment a later failed build needs one.
        if not (self.dist_dir / BACKUP_DIR / f"{utcnow().strftime('%Y%m%d')}.json").is_file():
            self._backup(path)

        try:
            shutil.copyfile(path, self.dist_dir / LAST_GOOD_FILE)
        except OSError as exc:
            LOGGER.warning("could not refresh last-known-good",
                           extra={"stage": "build", "check": "last_good", "error": str(exc)[:200]})

        result.output_path = path
        result.build.published = True
        self.store.record_build(result.build)
        self.store.set_meta("published_source_ids", json.dumps(sorted(
            source_id for bucket in result.tiers.values() for source_id in bucket
        )))
        self.store.set_meta("last_good_build_id", result.build.build_id)
        # tier is part of what was published, so it must be persisted - the
        # in-memory Source objects mutated by assign_tiers() are gone by now.
        tier_by_id = {
            source_id: tier_name
            for tier_name, source_ids in result.tiers.items()
            for source_id in source_ids
        }
        for source in self.store.list_sources():
            new_tier = tier_by_id.get(source.id, Tier.NONE)
            if source.tier != new_tier:
                source.tier = new_tier
                self.store.upsert_source(source)
            if new_tier != Tier.NONE:
                self.store.record_event(SourceEvent(
                    source_id=source.id,
                    event=Event.RESTORED if new_tier == Tier.PRIMARY else Event.CHECKED,
                    detail=f"published as {new_tier}",
                    to_status=source.status,
                ))
        LOGGER.info("published", extra={
            "stage": "build",
            "check": "publish",
            "items": result.build.source_count,
            "primary": result.build.primary_count,
            "build_id": result.build.build_id,
        })
        return path

    def record_blocked(self, result: BuildResult) -> None:
        self.store.record_build(result.build)

    def _publish_alt(self, path: Path) -> Path | None:
        """Publish a second entry point whose urls point at a different host.

        Same list, different base: a client that cannot resolve github.io can
        still load the whole thing through the alternate host.
        """
        primary = self.mirror.public_base
        alt = self.mirror.alt_base
        if not primary or not alt:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not _retarget_urls(data, primary, alt):
            return None
        if self._write_alt_sources(primary, alt):
            # those copies name our jars, and they now live beside them
            needle = f"{alt}/{SOURCES_DIR}/"
            for item in data.get("urls") or []:
                value = item.get("url") if isinstance(item, dict) else None
                if isinstance(value, str) and value.startswith(needle):
                    item["url"] = f"{alt}/{ALT_DIR}/{SOURCES_DIR}/" + value[len(needle):]
        _atomic_write_json(self.dist_dir / ALT_FILE, data, pretty=bool(self.output_cfg.get("pretty", True)))
        return self.dist_dir / ALT_FILE

    def _write_alt_sources(self, primary: str, alt: str) -> int:
        """Copy every mirrored config with our own urls pointed at ``alt``.

        A client on the alternate host must not have to reach the primary one
        for its crawler, so each copy names ``alt`` for the jars as well.
        """
        source_dir = self.dist_dir / SOURCES_DIR
        if not source_dir.is_dir():
            return 0
        target = self.dist_dir / ALT_DIR / SOURCES_DIR
        target.mkdir(parents=True, exist_ok=True)
        written = set()
        for path in source_dir.glob("*.json"):
            text = path.read_text(encoding="utf-8")
            (target / path.name).write_text(text.replace(primary, alt), encoding="utf-8")
            written.add(path.name)
        for stale in target.glob("*.json"):
            if stale.name not in written:
                try:
                    stale.unlink()
                except OSError:
                    pass
        return len(written)

    # -- artefacts ---------------------------------------------------------
    def _backup(self, path: Path) -> None:
        keep = int(self.output_cfg.get("keep_backups", 30))
        backup_dir = self.dist_dir / BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%d")
        target = backup_dir / f"{stamp}.json"
        try:
            shutil.copyfile(path, target)
        except OSError as exc:
            LOGGER.warning("backup copy failed", extra={"stage": "build", "check": "backup", "error": str(exc)[:200]})
            return
        backups = sorted(backup_dir.glob("*.json"))
        for stale in backups[:-keep] if keep > 0 else []:
            try:
                stale.unlink()
            except OSError:
                pass

    def _health(self, result: BuildResult, tiers: dict[str, list[Source]], counts: dict[str, int]) -> dict[str, Any]:
        sources = tiers[Tier.PRIMARY] + tiers[Tier.BACKUP] + tiers[Tier.EXPERIMENTAL]
        regions = self.store.region_health(days=1)
        alerts = self.store.list_alerts(limit=5)
        detail = []
        for source in sources:
            probes = self.store.probes_for(source.id, limit=1)
            last = probes[0] if probes else None
            detail.append({
                "id": source.id,
                "name": source.name,
                "url": source.raw_url or source.url,
                "type": source.type,
                "status": source.status,
                "tier": source.tier,
                "score": source.score,
                "stability_score": source.stability_score,
                "search_score": source.search_score,
                "playback_score": source.playback_score,
                "latency_score": source.latency_score,
                "regional_score": source.regional_score,
                "freshness_score": source.freshness_score,
                "availability_7d": self.store.availability(source.id, 7),
                "availability_30d": self.store.availability(source.id, 30),
                "avg_response_ms": self.store.average_latency(source.id, 7),
                "last_success_at": source.last_success_at,
                "last_failure_at": source.last_failure_at,
                "last_check_at": source.last_check_at,
                "consecutive_failure": source.consecutive_failure,
                "consecutive_success": source.consecutive_success,
                "global_status": source.global_status,
                "regions_ok": source.regions_ok,
                "regions_failed": source.regions_failed,
                "whitelisted": source.whitelisted,
                "error_code": last.error_code if last else None,
                "error_stage": last.error_stage if last else "",
            })
        return {
            "generated_at": to_iso(),
            "build_id": result.build.build_id,
            "summary": {
                "sources": counts,
                "output_items": result.build.source_count,
                "tiers": {name: len(bucket) for name, bucket in tiers.items()},
                "checks": result.validation.checks,
            },
            "regions": regions,
            "last_build": {
                "build_id": result.build.build_id,
                "generated_at": result.build.generated_at,
                "git_sha": result.build.git_sha,
                "active": result.build.active_count,
                "degraded": result.build.degraded_count,
                "failed": result.build.failed_count,
                "new": result.build.new_count,
                "recovered": result.build.recovered_count,
                "removed": result.build.removed_count,
            },
            "last_alert": _alert_summary(alerts[0]) if alerts else None,
            "sources": detail,
        }

    def _dashboard(self, result: BuildResult, tiers: dict[str, list[Source]], counts: dict[str, int]) -> dict[str, Any]:
        alerts = [alert for alert in self.store.list_alerts(limit=50)]
        sources = tiers[Tier.PRIMARY] + tiers[Tier.BACKUP] + tiers[Tier.EXPERIMENTAL]
        return {
            "generated_at": to_iso(),
            "build_id": result.build.build_id,
            "summary": {
                "sources": counts,
                "candidates": counts.get(Status.CANDIDATE, 0) + counts.get(Status.VALIDATING, 0),
                "tiers": {name: len(bucket) for name, bucket in tiers.items()},
                "output_items": result.build.source_count,
            },
            "regions": self.store.region_health(days=1),
            "sources": [
                {
                    "id": source.id,
                    "name": source.name,
                    "url": source.raw_url or source.url,
                    "status": source.status,
                    "tier": source.tier,
                    "type": source.type,
                    "score": source.score,
                    "stability_score": source.stability_score,
                    "availability_7d": self.store.availability(source.id, 7),
                    "availability_30d": self.store.availability(source.id, 30),
                    "last_success_at": source.last_success_at,
                    "last_failure_at": source.last_failure_at,
                    "regions_ok": source.regions_ok,
                    "regions_failed": source.regions_failed,
                    "avg_response_ms": self.store.average_latency(source.id, 7),
                    "consecutive_failure": source.consecutive_failure,
                    "whitelisted": source.whitelisted,
                }
                for source in sources
            ],
            "alerts": [_alert_summary(alert) for alert in alerts],
            "build_history": [
                {
                    "build_id": build.build_id,
                    "generated_at": build.generated_at,
                    "published": build.published,
                    "fallback": build.fallback,
                    "items": build.source_count,
                    "active": build.active_count,
                    "degraded": build.degraded_count,
                    "failed": build.failed_count,
                    "new": build.new_count,
                    "recovered": build.recovered_count,
                    "removed": build.removed_count,
                    "blocked_reason": build.blocked_reason,
                }
                for build in self.store.list_builds(limit=30)
            ],
            "nodes": [
                {
                    "name": node.name,
                    "region": node.region,
                    "enabled": node.enabled,
                    "last_seen_at": node.last_seen_at,
                    "last_ok_at": node.last_ok_at,
                    "consecutive_failures": node.consecutive_failures,
                }
                for node in self.store.list_nodes()
            ],
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _score_at_least(value: Any, minimum: Any) -> bool:
    try:
        return float(value or 0) >= float(minimum)
    except (TypeError, ValueError):
        return False


def _content_type_allowed(value: str | None, accepted: Iterable[str]) -> bool:
    content_type = str(value or "").split(";", 1)[0].strip().lower()
    rules = [str(item or "").split(";", 1)[0].strip().lower() for item in accepted]
    if not rules:
        return True
    return any(
        content_type.startswith(rule) if rule.endswith("/") else content_type == rule
        for rule in rules
        if rule
    )


def _naming(url_map: dict[str, str] | None, name_map: dict[str, str] | None):
    """Resolve a source to the name/url it should be published under."""
    urls = url_map or {}
    names = name_map or {}

    def name_of(source: Source) -> str:
        return names.get(source.id) or _display_name(source)

    def url_of(source: Source) -> str:
        return urls.get(source.id) or _fetch_url(source)

    return name_of, url_of


def _display_name(source: Source) -> str:
    return (source.name or source.id[:8]).strip()


def _fetch_url(source: Source) -> str:
    return (source.raw_url or source.url).strip()


def _alert_summary(alert) -> dict[str, Any]:
    return {
        "kind": alert.kind,
        "source_id": alert.source_id,
        "region": alert.region,
        "title": alert.title,
        "created_at": alert.created_at,
        "resolved_at": alert.resolved_at,
        "severity": alert.severity,
        "channels": alert.channels,
    }


def _retarget_urls(data: Any, primary: str, alt: str) -> int:
    """Point every url of a rendered multi-source list at ``alt``.

    Only urls that actually sit under ``primary`` are touched, so a source the
    mirror could not fetch (and therefore left pointing somewhere else) is not
    silently rewritten.  Returns how many were swapped.
    """
    urls = data.get("urls") if isinstance(data, dict) else None
    if not isinstance(urls, list):
        return 0
    swapped = 0
    for item in urls:
        value = item.get("url") if isinstance(item, dict) else None
        if isinstance(value, str) and value.startswith(f"{primary}/"):
            item["url"] = alt + value[len(primary):]
            swapped += 1
    return swapped


def count_output_items(output: Any) -> int:
    if isinstance(output, list):
        return len(output)
    if isinstance(output, dict):
        for key in ("urls", "sites", "list", "lives"):
            value = output.get(key)
            if isinstance(value, list):
                return len(value)
    return 0


def _expand_template(
    template: dict[str, Any],
    sources: list[Source],
    url_map: dict[str, str] | None = None,
    name_map: dict[str, str] | None = None,
) -> Any:
    """Tiny explicit template expander.

    Supported placeholders inside string values:
      ``$generated_at``, ``$version``, ``$count``
    and per-item expansion for any list whose single element is a dict and
    which contains the marker key ``"$each"``.
    """

    def render_item(item_template: dict[str, Any], source: Source) -> dict[str, Any]:
        mapping = {
            "$name": name_of(source),
            "$url": url_of(source),
            "$api": url_of(source),
            "$id": source.id,
            "$key": source.id[:16],
            "$score": source.score,
            "$tier": source.tier,
            "$status": source.status,
            "$type": 3,
        }
        out: dict[str, Any] = {}
        for key, value in item_template.items():
            if key == "$each":
                continue
            out[key] = mapping.get(value, value) if isinstance(value, str) else value
        return out

    result: dict[str, Any] = {}
    for key, value in template.items():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "$each" in value[0]:
            result[key] = [render_item(value[0], source) for source in sources]
        elif isinstance(value, str):
            result[key] = (
                value.replace("$generated_at", to_iso())
                .replace("$version", "1.0")
                .replace("$count", str(len(sources)))
            )
        elif isinstance(value, dict):
            result[key] = _expand_template(value, sources, url_map, name_map)
        else:
            result[key] = value
    return result


def _atomic_write_json(path: Path, payload: Any, *, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None, sort_keys=False)
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
