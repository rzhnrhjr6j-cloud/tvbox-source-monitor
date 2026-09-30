"""Scoring engine + lifecycle state machine (spec §9, §11).

The score is fully explainable - every component is returned in a
``ScoreBreakdown`` so a dashboard or an operator can see *why* a source ranks
where it does.  No black boxes, and no thresholds outside config.

Note (spec §9): this is a technical health score.  It is used for internal
ranking and automatic decisions only, and is deliberately never presented as a
value judgement about the content of a source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger
from ..models import Event, GlobalStatus, Status, Source, SourceEvent
from ..regions.quorum import RegionVerdict, regional_score
from ..utils.timeutil import age_days, to_iso
from .decay import DEFAULT_HALF_LIFE_DAYS, freshness
from .stability import NEUTRAL, windowed_stability

LOGGER = get_logger("scoring")

STATUS_BY_NAME = {
    "global": GlobalStatus.GLOBAL,
    "regional": GlobalStatus.REGIONAL,
    "failed": GlobalStatus.FAILED,
}


@dataclass
class ScoreBreakdown:
    availability: float = 0.0
    search: float = 0.0
    playback: float = 0.0
    latency: float = 0.0
    stability: float = 0.0
    regional: float = 0.0
    freshness: float = 0.0
    total: float = 0.0
    stability_windows: dict[str, float | None] = field(default_factory=dict)
    samples: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "availability": round(self.availability, 3),
            "search": round(self.search, 3),
            "playback": round(self.playback, 3),
            "latency": round(self.latency, 3),
            "stability": round(self.stability, 3),
            "regional": round(self.regional, 3),
            "freshness": round(self.freshness, 3),
            "total": round(self.total, 3),
            "stability_windows": self.stability_windows,
            "samples": self.samples,
        }


class Scorer:
    def __init__(self, cfg, store=None):
        self.cfg = cfg
        self.store = store
        self.weights = {key: float(value) for key, value in cfg.section("weights").items()}
        self.thresholds = cfg.section_default("status_thresholds") or {"active": 70, "degraded": 50, "primary": 85}
        self.windows = list(cfg.get("stability.windows") or [])
        self.decay_half_life = float(cfg.get("decay.half_life_days", DEFAULT_HALF_LIFE_DAYS))
        self.latency_buckets = list(cfg.get("latency.buckets") or [])
        self.lifecycle = cfg.section_default("lifecycle")

    # -- components --------------------------------------------------------
    def latency_score(self, average_ms: float | None) -> float:
        if average_ms is None:
            return NEUTRAL
        for bucket in self.latency_buckets:
            if average_ms <= float(bucket.get("ms", 0)):
                return float(bucket.get("score", 0))
        return 0.0

    def weighted_total(self, breakdown: ScoreBreakdown, exclude: set[str] | None = None) -> float:
        """Weighted sum over the components that actually apply.

        ``exclude`` drops components a source has no surface for (a 多仓 wrapper
        has no search API of its own) and the remaining weights are re-normalised
        - otherwise an inapplicable component would silently score 0 and drag a
        perfectly healthy wrapper down into DEGRADED.
        """
        components = {
            "availability": breakdown.availability,
            "search_success": breakdown.search,
            "playback_success": breakdown.playback,
            "latency": breakdown.latency,
            "stability": breakdown.stability,
            "regional_consistency": breakdown.regional,
            "freshness": breakdown.freshness,
        }
        exclude = exclude or set()
        active = {name: weight for name, weight in self.weights.items() if name not in exclude}
        total_weight = sum(float(weight) for weight in active.values())
        if total_weight <= 0:
            return 0.0
        total = 0.0
        for name, weight in active.items():
            total += (float(weight) / total_weight) * float(components.get(name, 0.0))
        return round(total, 3)

    def status_for(self, score: float) -> str:
        if score >= float(self.thresholds.get("active", 70)):
            return Status.ACTIVE
        if score >= float(self.thresholds.get("degraded", 50)):
            return Status.DEGRADED
        return Status.FAILED

    # -- assemble from stored history --------------------------------------
    def _ratio(self, source_id: str, days: float, field_name: str) -> tuple[float | None, int]:
        if self.store is None:
            return None, 0
        probes = self.store.probes_for(source_id, days=days)
        if not probes:
            return None, 0
        hits = sum(1 for probe in probes if getattr(probe, field_name, False))
        return round(100.0 * hits / len(probes), 3), len(probes)

    def applicable_components(self, source: Source) -> set[str]:
        """Components a source cannot be judged on, based on its own shape."""
        if source.type == "multi":
            # a wrapper config is only reachable + parseable; it exposes no
            # search/detail API of its own (spec §5.1.C - children are scored)
            return {"search_success", "playback_success"}
        return set()

    def score_source(self, source: Source, verdict: RegionVerdict | None = None) -> ScoreBreakdown:
        breakdown = ScoreBreakdown()

        if self.store is not None:
            availability = self.store.availability(source.id, 7)
            if availability is None:
                availability = self.store.availability(source.id, 1)
            breakdown.availability = availability if availability is not None else 0.0

            search_ratio, search_samples = self._ratio(source.id, 7, "search_success")
            breakdown.search = search_ratio if search_ratio is not None else 0.0
            playback_ratio, playback_samples = self._ratio(source.id, 7, "playback_probe_success")
            breakdown.playback = playback_ratio if playback_ratio is not None else 0.0

            breakdown.samples = {"search": search_samples, "playback": playback_samples}
            breakdown.latency = self.latency_score(self.store.average_latency(source.id, 7))

            def lookup(days: float) -> float | None:
                return self.store.availability(source.id, days)

            breakdown.stability, breakdown.stability_windows = windowed_stability(self.windows, lookup)
            total_samples = sum(
                1 for window in self.windows if breakdown.stability_windows.get(str(window.get("name"))) is not None
            )
            breakdown.samples["stability_windows"] = total_samples
        else:
            breakdown.availability = 0.0
            breakdown.search = 0.0
            breakdown.playback = 0.0
            breakdown.latency = NEUTRAL
            breakdown.stability = NEUTRAL

        breakdown.regional = regional_score(verdict) if verdict is not None else NEUTRAL
        breakdown.freshness = freshness(source.last_success_at, self.decay_half_life)
        breakdown.total = self.weighted_total(breakdown, self.applicable_components(source))
        return breakdown

    def apply_scores(self, source: Source, breakdown: ScoreBreakdown) -> None:
        source.previous_score = source.score
        source.availability_score = round(breakdown.availability, 3)
        source.search_score = round(breakdown.search, 3)
        source.playback_score = round(breakdown.playback, 3)
        source.latency_score = round(breakdown.latency, 3)
        source.stability_score = round(breakdown.stability, 3)
        source.regional_score = round(breakdown.regional, 3)
        source.freshness_score = round(breakdown.freshness, 3)
        source.score = round(breakdown.total, 3)
        source.health_level = self.status_for(source.score)

    # -- candidate admission score (spec §27) -------------------------------
    def candidate_score(self, signals: dict[str, bool]) -> float:
        section = self.cfg.section_default("candidate")
        weights = section.get("weights") or {}
        total = 0.0
        for name, weight in weights.items():
            total += float(weight) * (100.0 if signals.get(name) else 0.0)
        return round(total, 3)

    def candidate_threshold(self) -> float:
        return float(self.cfg.get("candidate.threshold", 55))


@dataclass
class LifecycleOutcome:
    status: str
    paused: bool
    events: list[SourceEvent] = field(default_factory=list)
    note: str = ""


def apply_lifecycle(
    source: Source,
    *,
    success: bool,
    scorer: Scorer,
    when: str | None = None,
) -> LifecycleOutcome:
    """Update counters and derive the lifecycle status (spec §11).

    Rules, all thresholds from ``scoring.yaml``:

    * 1 failure    - keep, no penalty
    * 2-3 failures - down-weight (the score does that; we only track counters)
    * 5 failures   - pause output
    * 12 failures  - FAILED
    * 3 successes  - RECOVERING
    * 5 successes  - back to ACTIVE
    """
    stamp = when or to_iso()
    section = scorer.lifecycle

    def rule(key: str, default: int) -> int:
        try:
            return int(section.get(key, default))
        except (TypeError, ValueError):
            return default

    previous_status = source.status
    was_paused = bool(source.paused)
    events: list[SourceEvent] = []

    if success:
        source.consecutive_success += 1
        source.consecutive_failure = 0
        source.total_successes += 1
        source.last_success_at = stamp
        if previous_status in (Status.FAILED, Status.RECOVERING, Status.DEGRADED):
            source.last_recovery_at = stamp
    else:
        source.consecutive_failure += 1
        source.consecutive_success = 0
        source.total_failures += 1
        source.last_failure_at = stamp
        if previous_status in (Status.ACTIVE, Status.RECOVERING):
            source.last_outage_at = stamp

    source.total_checks += 1
    source.max_consecutive_failure = max(source.max_consecutive_failure, source.consecutive_failure)
    source.max_consecutive_success = max(source.max_consecutive_success, source.consecutive_success)
    source.last_check_at = stamp

    fail_failed = rule("fail_mark_failed", 12)
    fail_pause = rule("fail_pause_output", 5)
    recover_recovering = rule("recover_min_success", 3)
    recover_active = rule("recover_to_active", 5)

    status = previous_status
    note = ""
    # a source that genuinely went down (FAILED, or output-paused) must climb the
    # recovery ladder from §11 instead of snapping straight back to ACTIVE on a
    # single good probe.
    was_down = previous_status in (Status.FAILED, Status.RECOVERING) or was_paused

    if source.blacklisted:
        status = Status.BLACKLISTED
    elif source.consecutive_failure >= fail_failed:
        status = Status.FAILED
        note = f"{source.consecutive_failure} consecutive failures"
    elif was_down:
        if source.consecutive_success >= recover_active:
            status = Status.ACTIVE
            note = "recovered"
        elif source.consecutive_success >= recover_recovering:
            status = Status.RECOVERING
            note = f"recovering ({source.consecutive_success}/{recover_active} consecutive successes)"
        else:
            status = Status.FAILED if previous_status == Status.FAILED else Status.DEGRADED
    elif source.consecutive_failure >= fail_pause:
        # paused: never output this source, but do not declare it dead either
        status = Status.DEGRADED
        note = f"paused after {source.consecutive_failure} consecutive failures"
    else:
        status = scorer.status_for(source.score)

    # A completed recovery must not be undone by the trailing score.  After an
    # outage the 7-day availability/stability windows still contain the outage,
    # so the score lags well behind reality; letting it re-decide the status on
    # the very next probe flaps ACTIVE -> DEGRADED every round and silently
    # drops a source that has demonstrably come back.  A fresh success streak is
    # better evidence than a stale 7-day average, but the score still acts as a
    # floor so a genuinely poor source is never promoted on streak alone.
    if (
        not source.blacklisted
        and source.consecutive_success >= recover_active
        and status in (Status.DEGRADED, Status.RECOVERING)
        and source.score >= float(scorer.thresholds.get("degraded", 50))
    ):
        status = Status.ACTIVE
        if not note:
            note = f"recovered ({source.consecutive_success} consecutive successes)"

    source.paused = bool(source.consecutive_failure >= fail_pause and status not in (Status.ACTIVE, Status.RECOVERING))

    source.status = status
    source.touch(stamp)

    if status != previous_status:
        events.append(
            SourceEvent(
                source_id=source.id,
                event=Event.STATUS_CHANGE,
                detail=note or f"{previous_status} -> {status}",
                from_status=previous_status,
                to_status=status,
                ts=stamp,
            )
        )
        if status == Status.ACTIVE and previous_status in (Status.FAILED, Status.RECOVERING, Status.DEGRADED):
            events.append(SourceEvent(source_id=source.id, event=Event.RECOVERED, detail=note, to_status=status, ts=stamp))
        elif status == Status.FAILED:
            events.append(SourceEvent(source_id=source.id, event=Event.FAILED, detail=note, to_status=status, ts=stamp))

    return LifecycleOutcome(status=status, paused=source.paused, events=events, note=note)


def staleness_days(source: Source) -> float | None:
    return age_days(source.last_check_at or source.last_seen_at)
