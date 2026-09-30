"""Core data model (spec §4.1 - §4.3, §11, §24, §29, §31).

Plain dataclasses with explicit ``to_row`` / ``from_row`` mappings so the
SQLite schema and the JSON payloads stay in lock-step.  List/dict fields are
stored as JSON text.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from .utils.timeutil import to_iso, utcnow

SCHEMA_VERSION = 3


class Status:
    """Source lifecycle (spec §11)."""

    DISCOVERED = "discovered"
    CANDIDATE = "candidate"
    VALIDATING = "validating"
    ACTIVE = "active"
    DEGRADED = "degraded"
    FAILED = "failed"
    RECOVERING = "recovering"
    BLACKLISTED = "blacklisted"
    REJECTED = "rejected"

    # accepted on load, normalised on write
    ALIASES = {"healthy": ACTIVE, "ok": ACTIVE, "unknown": CANDIDATE, "": CANDIDATE, None: CANDIDATE}
    ALL = (
        DISCOVERED, CANDIDATE, VALIDATING, ACTIVE, DEGRADED,
        FAILED, RECOVERING, BLACKLISTED, REJECTED,
    )


def normalize_status(value: Any) -> str:
    if value in Status.ALL:
        return str(value)
    if value in Status.ALIASES:
        return Status.ALIASES[value]
    return Status.CANDIDATE


class Tier:
    """Output tiering (spec §29)."""

    PRIMARY = "primary"
    BACKUP = "backup"
    EXPERIMENTAL = "experimental"
    NONE = "none"


class GlobalStatus:
    """Multi-region verdict (spec §13)."""

    GLOBAL = "GLOBAL"
    REGIONAL = "REGIONAL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class HealthLevel:
    """Health grade used by probes (spec §8)."""

    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class Event:
    DISCOVERED = "discovered"
    ADMITTED = "admitted"
    REJECTED = "rejected"
    CHECKED = "checked"
    STATUS_CHANGE = "status_change"
    SCORE_CHANGE = "score_change"
    RECOVERED = "recovered"
    FAILED = "failed"
    REMOVED = "removed"
    RESTORED = "restored"
    BLACKLISTED = "blacklisted"
    WHITELISTED = "whitelisted"


class AlertKind:
    SOURCE_FAILED = "source_failed"
    SOURCE_RECOVERED = "source_recovered"
    SCORE_DROP = "score_drop"
    BUILD_BLOCKED = "build_blocked"
    NODE_DOWN = "node_down"
    OUTPUT_ROLLBACK = "output_rollback"


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else None, ensure_ascii=False, sort_keys=True)


def _load(value: Any, default: Any) -> Any:
    if value in (None, "", b""):
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Source:
    """A single point-on-demand source (spec §4.1)."""

    id: str
    url: str
    name: str = ""
    raw_url: str = ""
    type: str = "single"                 # single | multi
    parent_id: str | None = None
    schema: str = ""                     # detected config schema
    status: str = Status.CANDIDATE
    tier: str = Tier.NONE

    first_seen_at: str = field(default_factory=to_iso)
    last_seen_at: str = field(default_factory=to_iso)
    last_check_at: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_recovery_at: str | None = None
    last_outage_at: str | None = None
    updated_at: str = field(default_factory=to_iso)

    score: float = 0.0
    stability_score: float = 0.0
    search_score: float = 0.0
    playback_score: float = 0.0
    latency_score: float = 0.0
    regional_score: float = 0.0
    freshness_score: float = 0.0
    availability_score: float = 0.0
    candidate_score: float = 0.0
    previous_score: float | None = None

    consecutive_success: int = 0
    consecutive_failure: int = 0
    max_consecutive_failure: int = 0
    max_consecutive_success: int = 0
    total_checks: int = 0
    total_successes: int = 0
    total_failures: int = 0

    global_status: str = GlobalStatus.UNKNOWN
    regions_ok: list[str] = field(default_factory=list)
    regions_failed: list[str] = field(default_factory=list)
    node_count: int = 0

    health_level: str = HealthLevel.UNKNOWN
    tags: list[str] = field(default_factory=list)
    note: str = ""
    whitelisted: bool = False
    blacklisted: bool = False
    paused: bool = False

    # -- serialisation -----------------------------------------------------
    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        for key in ("regions_ok", "regions_failed", "tags"):
            row[key] = _dump(row[key])
        for key in ("whitelisted", "blacklisted", "paused"):
            row[key] = 1 if row[key] else 0
        return row

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Source":
        data = dict(row)
        for key in ("regions_ok", "regions_failed", "tags"):
            data[key] = _load(data.get(key), [])
        for key in ("whitelisted", "blacklisted", "paused"):
            data[key] = bool(data.get(key))
        data["status"] = normalize_status(data.get("status"))
        # tolerate rows written by older schema versions
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        data = {key: value for key, value in data.items() if key in allowed}
        return cls(**data)

    def touch(self, when: str | None = None) -> None:
        stamp = when or to_iso()
        self.updated_at = stamp
        self.last_seen_at = stamp

    def is_output_eligible(self) -> bool:
        return (
            not self.blacklisted
            and not self.paused
            and self.status in (Status.ACTIVE, Status.DEGRADED, Status.RECOVERING)
        )


@dataclass
class ProbeResult:
    """One probe of one source from one region (spec §4.2)."""

    source_id: str
    region: str
    node: str = ""
    ts: str = field(default_factory=to_iso)

    http_ok: bool = False
    status_code: int | None = None
    dns_ok: bool = False
    tls_ok: bool = False
    response_ms: int | None = None

    config_parse_success: bool = False
    schema_detected: str = ""

    search_success: bool = False
    search_result_count: int = 0
    search_first_ms: int | None = None

    detail_success: bool = False
    detail_has_playlist: bool = False

    playback_url_obtained: bool = False
    playback_probe_success: bool = False
    playback_content_type: str = ""
    playback_first_byte_ms: int | None = None

    health_level: str = HealthLevel.UNKNOWN
    score: float = 0.0
    error_code: str | None = None
    error_message: str | None = None
    # which layer produced error_code: L1_http / L2_config / L3_search /
    # L4_detail / L5_playback (spec §35-13 traceability)
    error_stage: str = ""
    checks_version: int = 1

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        for key in (
            "http_ok", "dns_ok", "tls_ok", "config_parse_success", "search_success",
            "detail_success", "detail_has_playlist", "playback_url_obtained",
            "playback_probe_success",
        ):
            row[key] = 1 if row[key] else 0
        return row

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ProbeResult":
        data = dict(row)
        data.pop("id", None)
        for key in (
            "http_ok", "dns_ok", "tls_ok", "config_parse_success", "search_success",
            "detail_success", "detail_has_playlist", "playback_url_obtained",
            "playback_probe_success",
        ):
            data[key] = bool(data.get(key))
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})

    @property
    def ok(self) -> bool:
        return self.http_ok and self.config_parse_success

    def summary(self) -> str:
        return (
            f"http={self.http_ok} cfg={self.config_parse_success} "
            f"search={self.search_success} detail={self.detail_success} "
            f"playback={self.playback_probe_success} ms={self.response_ms}"
        )


@dataclass
class DailyStats:
    """Per source / region / day aggregate (spec §4.3)."""

    source_id: str
    region: str
    day: str
    checks: int = 0
    successes: int = 0
    failures: int = 0
    availability_percent: float = 0.0
    avg_response_ms: float | None = None
    p50_ms: float | None = None
    p95_ms: float | None = None
    search_success_percent: float = 0.0
    playback_success_percent: float = 0.0

    def to_row(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "DailyStats":
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in dict(row).items() if k in allowed})


@dataclass
class Alert:
    source_id: str | None = None
    region: str = ""
    kind: str = AlertKind.SOURCE_FAILED
    severity: str = "warning"
    title: str = ""
    message: str = ""
    fingerprint: str = ""
    created_at: str = field(default_factory=to_iso)
    resolved_at: str | None = None
    suppressed: bool = False
    channels: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["channels"] = _dump(row["channels"])
        row["suppressed"] = 1 if row["suppressed"] else 0
        row.pop("id", None)
        return row

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Alert":
        data = dict(row)
        data["channels"] = _load(data.get("channels"), [])
        data["suppressed"] = bool(data.get("suppressed"))
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class BuildRecord:
    """Build provenance + counters (spec §31)."""

    build_id: str
    generated_at: str = field(default_factory=to_iso)
    git_sha: str = ""
    active_count: int = 0
    degraded_count: int = 0
    failed_count: int = 0
    new_count: int = 0
    recovered_count: int = 0
    removed_count: int = 0
    source_count: int = 0
    published: bool = False
    fallback: bool = False
    blocked_reason: str = ""
    output_sha256: str = ""
    primary_count: int = 0
    backup_count: int = 0
    experimental_count: int = 0

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["published"] = 1 if row["published"] else 0
        row["fallback"] = 1 if row["fallback"] else 0
        return row

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "BuildRecord":
        data = dict(row)
        data.pop("id", None)
        data["published"] = bool(data.get("published"))
        data["fallback"] = bool(data.get("fallback"))
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class DiscoveryRecord:
    url: str
    normalized_url: str = ""
    source_id: str = ""
    adapter: str = ""
    query: str = ""
    found_at: str = field(default_factory=to_iso)
    admitted: bool = False
    reason: str = ""

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["admitted"] = 1 if row["admitted"] else 0
        return row


@dataclass
class SourceEvent:
    source_id: str
    event: str
    detail: str = ""
    from_status: str = ""
    to_status: str = ""
    ts: str = field(default_factory=to_iso)

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NodeRecord:
    name: str
    region: str
    url: str = ""
    enabled: bool = True
    weight: float = 1.0
    last_seen_at: str | None = None
    last_ok_at: str | None = None
    consecutive_failures: int = 0
    note: str = ""

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["enabled"] = 1 if row["enabled"] else 0
        return row

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "NodeRecord":
        data = dict(row)
        data["enabled"] = bool(data.get("enabled"))
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


def percentile(values: Iterable[float], fraction: float) -> float | None:
    """Nearest-rank percentile - deliberately dependency-free."""
    ordered = sorted(float(v) for v in values if v is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    index = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def now_stamp() -> str:
    return to_iso(utcnow())
