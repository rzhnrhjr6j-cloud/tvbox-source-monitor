"""SQLite persistence layer (spec §24).

One connection guarded by a re-entrant lock: the pipeline writes from the main
thread while worker threads only ever hand finished results back, so a single
WAL connection is both sufficient and the least surprising thing to reason
about.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..logging_setup import get_logger
from ..models import (
    Alert,
    BuildRecord,
    DailyStats,
    DiscoveryRecord,
    NodeRecord,
    ProbeResult,
    Source,
    SourceEvent,
    percentile,
)
from ..utils.timeutil import to_iso, utcnow
from .migrations import SCHEMA_VERSION, migrate

LOGGER = get_logger("storage")


class Store:
    def __init__(self, path: str | Path, logger=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.log = logger or LOGGER
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.schema_version = migrate(self._conn)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- low level ---------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- meta --------------------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return default if row is None else row["value"]

    def set_meta(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO meta(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value if isinstance(value, (str, type(None))) else str(value), to_iso()),
        )

    # -- sources -----------------------------------------------------------
    _SOURCE_COLUMNS = (
        "id", "url", "raw_url", "name", "type", "parent_id", "schema", "status", "tier",
        "first_seen_at", "last_seen_at", "last_check_at", "last_success_at", "last_failure_at",
        "last_recovery_at", "last_outage_at", "updated_at",
        "score", "stability_score", "search_score", "playback_score", "latency_score",
        "regional_score", "freshness_score", "availability_score", "candidate_score", "previous_score",
        "consecutive_success", "consecutive_failure", "max_consecutive_failure", "max_consecutive_success",
        "total_checks", "total_successes", "total_failures",
        "global_status", "regions_ok", "regions_failed", "node_count",
        "health_level", "tags", "note", "whitelisted", "blacklisted", "paused",
    )

    def upsert_source(self, source: Source) -> None:
        row = source.to_row()
        columns = list(self._SOURCE_COLUMNS)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "id")
        self.execute(
            f"INSERT INTO sources ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            [row.get(column) for column in columns],
        )

    def upsert_sources(self, sources: Iterable[Source]) -> int:
        count = 0
        for source in sources:
            self.upsert_source(source)
            count += 1
        return count

    def get_source(self, source_id: str) -> Source | None:
        row = self.query_one("SELECT * FROM sources WHERE id = ?", (source_id,))
        return Source.from_row(row) if row else None

    def get_source_by_url(self, url: str) -> Source | None:
        row = self.query_one("SELECT * FROM sources WHERE url = ?", (url,))
        return Source.from_row(row) if row else None

    def list_sources(
        self,
        statuses: Sequence[str] | None = None,
        include_blacklisted: bool = False,
        limit: int | None = None,
    ) -> list[Source]:
        sql = "SELECT * FROM sources"
        clauses, params = [], []
        if statuses:
            clauses.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        if not include_blacklisted:
            clauses.append("blacklisted = 0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY score DESC, name ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [Source.from_row(row) for row in self.query(sql, params)]

    def count_sources(self, statuses: Sequence[str] | None = None) -> int:
        if statuses:
            row = self.query_one(
                f"SELECT COUNT(*) AS n FROM sources WHERE status IN ({', '.join('?' for _ in statuses)})",
                list(statuses),
            )
        else:
            row = self.query_one("SELECT COUNT(*) AS n FROM sources")
        return int(row["n"]) if row else 0

    def count_by_status(self) -> dict[str, int]:
        rows = self.query("SELECT status, COUNT(*) AS n FROM sources GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}

    # -- probes ------------------------------------------------------------
    _PROBE_COLUMNS = (
        "source_id", "region", "node", "ts", "http_ok", "status_code", "dns_ok", "tls_ok",
        "response_ms", "config_parse_success", "schema_detected",
        "search_success", "search_result_count", "search_first_ms",
        "detail_success", "detail_has_playlist",
        "playback_url_obtained", "playback_probe_success", "playback_content_type",
        "playback_first_byte_ms", "health_level", "score", "error_code", "error_message",
        "error_stage", "checks_version",
    )

    def record_probe(self, probe: ProbeResult) -> None:
        self.record_probes([probe])

    def record_probes(self, probes: Iterable[ProbeResult]) -> int:
        rows = []
        for probe in probes:
            row = probe.to_row()
            rows.append([row.get(column) for column in self._PROBE_COLUMNS])
        self.executemany(
            f"INSERT INTO probe_results ({', '.join(self._PROBE_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in self._PROBE_COLUMNS)})",
            rows,
        )
        return len(rows)

    def probes_for(
        self,
        source_id: str,
        *,
        days: float | None = None,
        region: str | None = None,
        limit: int | None = None,
    ) -> list[ProbeResult]:
        sql = "SELECT * FROM probe_results WHERE source_id = ?"
        params: list[Any] = [source_id]
        if region:
            sql += " AND region = ?"
            params.append(region)
        if days is not None:
            from ..utils.timeutil import days_ago

            sql += " AND ts >= ?"
            params.append(days_ago(days))
        sql += " ORDER BY ts DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [ProbeResult.from_row(row) for row in self.query(sql, params)]

    def count_probes(self, since_iso: str | None = None) -> int:
        if since_iso:
            row = self.query_one("SELECT COUNT(*) AS n FROM probe_results WHERE ts >= ?", (since_iso,))
        else:
            row = self.query_one("SELECT COUNT(*) AS n FROM probe_results")
        return int(row["n"]) if row else 0

    def prune_probes(self, keep_days: float) -> int:
        from ..utils.timeutil import days_ago

        cursor = self.execute("DELETE FROM probe_results WHERE ts < ?", (days_ago(keep_days),))
        return cursor.rowcount or 0

    # -- daily stats -------------------------------------------------------
    def upsert_daily_stats(self, stats: Iterable[DailyStats]) -> None:
        rows = []
        for stat in stats:
            row = stat.to_row()
            rows.append([
                row["source_id"], row["region"], row["day"], row["checks"], row["successes"],
                row["failures"], row["availability_percent"], row["avg_response_ms"],
                row["p50_ms"], row["p95_ms"], row["search_success_percent"],
                row["playback_success_percent"],
            ])
        self.executemany(
            "INSERT INTO daily_stats (source_id, region, day, checks, successes, failures, "
            "availability_percent, avg_response_ms, p50_ms, p95_ms, search_success_percent, "
            "playback_success_percent) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_id, region, day) DO UPDATE SET "
            "checks = excluded.checks, successes = excluded.successes, failures = excluded.failures, "
            "availability_percent = excluded.availability_percent, avg_response_ms = excluded.avg_response_ms, "
            "p50_ms = excluded.p50_ms, p95_ms = excluded.p95_ms, "
            "search_success_percent = excluded.search_success_percent, "
            "playback_success_percent = excluded.playback_success_percent",
            rows,
        )

    def rebuild_daily_stats(self, source_id: str, region: str, day: str) -> DailyStats:
        """Recompute one (source, region, day) aggregate from raw probes."""
        rows = self.query(
            "SELECT * FROM probe_results WHERE source_id = ? AND region = ? AND substr(ts,1,10) = ?",
            (source_id, region, day),
        )
        checks = len(rows)
        successes = sum(1 for row in rows if row["http_ok"] and row["config_parse_success"])
        latencies = [row["response_ms"] for row in rows if row["response_ms"] is not None]
        search_ok = sum(1 for row in rows if row["search_success"])
        playback_ok = sum(1 for row in rows if row["playback_probe_success"])
        stat = DailyStats(
            source_id=source_id,
            region=region,
            day=day,
            checks=checks,
            successes=successes,
            failures=checks - successes,
            availability_percent=round(100.0 * successes / checks, 3) if checks else 0.0,
            avg_response_ms=round(sum(latencies) / len(latencies), 2) if latencies else None,
            p50_ms=percentile(latencies, 0.5),
            p95_ms=percentile(latencies, 0.95),
            search_success_percent=round(100.0 * search_ok / checks, 3) if checks else 0.0,
            playback_success_percent=round(100.0 * playback_ok / checks, 3) if checks else 0.0,
        )
        self.upsert_daily_stats([stat])
        return stat

    def daily_stats(self, source_id: str, days: float = 30, region: str | None = None) -> list[DailyStats]:
        from ..utils.timeutil import days_ago

        sql = "SELECT * FROM daily_stats WHERE source_id = ? AND day >= ?"
        params: list[Any] = [source_id, days_ago(days)[:10]]
        if region:
            sql += " AND region = ?"
            params.append(region)
        sql += " ORDER BY day DESC"
        return [DailyStats.from_row(row) for row in self.query(sql, params)]

    def availability(self, source_id: str, days: float, region: str | None = None) -> float | None:
        stats = self.daily_stats(source_id, days, region)
        checks = sum(stat.checks for stat in stats)
        if not checks:
            return None
        successes = sum(stat.successes for stat in stats)
        return round(100.0 * successes / checks, 3)

    def average_latency(self, source_id: str, days: float = 7) -> float | None:
        from ..utils.timeutil import days_ago

        row = self.query_one(
            "SELECT AVG(response_ms) AS avg_ms, COUNT(response_ms) AS n FROM probe_results "
            "WHERE source_id = ? AND ts >= ? AND response_ms IS NOT NULL",
            (source_id, days_ago(days)),
        )
        if not row or not row["n"]:
            return None
        return float(row["avg_ms"])

    def region_health(self, days: float = 1) -> dict[str, dict[str, float]]:
        stats = self.daily_stats_for_all(days)
        buckets: dict[str, dict[str, float]] = {}
        for stat in stats:
            bucket = buckets.setdefault(stat.region or "?", {"checks": 0.0, "successes": 0.0})
            bucket["checks"] += stat.checks
            bucket["successes"] += stat.successes
        return {
            region: {
                "checks": int(value["checks"]),
                "successes": int(value["successes"]),
                "availability_percent": round(100.0 * value["successes"] / value["checks"], 2)
                if value["checks"] else 0.0,
            }
            for region, value in sorted(buckets.items())
        }

    def daily_stats_for_all(self, days: float = 1) -> list[DailyStats]:
        from ..utils.timeutil import days_ago

        rows = self.query(
            "SELECT * FROM daily_stats WHERE day >= ? ORDER BY day DESC",
            (days_ago(days)[:10],),
        )
        return [DailyStats.from_row(row) for row in rows]

    # -- alerts ------------------------------------------------------------
    def record_alert(self, alert: Alert) -> int:
        row = alert.to_row()
        cursor = self.execute(
            "INSERT INTO alerts (source_id, region, kind, severity, title, message, fingerprint, "
            "created_at, resolved_at, suppressed, channels) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["source_id"], row["region"], row["kind"], row["severity"], row["title"],
                row["message"], row["fingerprint"], row["created_at"], row["resolved_at"],
                row["suppressed"], row["channels"],
            ),
        )
        return int(cursor.lastrowid or 0)

    def recent_alert(self, fingerprint: str, hours: float) -> Alert | None:
        from ..utils.timeutil import days_ago

        row = self.query_one(
            "SELECT * FROM alerts WHERE fingerprint = ? AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (fingerprint, days_ago(hours / 24.0)),
        )
        return Alert.from_row(row) if row else None

    def open_alerts(self, kind: str | None = None) -> list[Alert]:
        sql = "SELECT * FROM alerts WHERE resolved_at IS NULL"
        params: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC"
        return [Alert.from_row(row) for row in self.query(sql, params)]

    def resolve_alert(self, alert_id: int) -> None:
        self.execute("UPDATE alerts SET resolved_at = ? WHERE id = ?", (to_iso(), alert_id))

    def resolve_alerts_by_fingerprint(self, fingerprint: str) -> int:
        cursor = self.execute(
            "UPDATE alerts SET resolved_at = ? WHERE fingerprint = ? AND resolved_at IS NULL",
            (to_iso(), fingerprint),
        )
        return cursor.rowcount or 0

    def resolve_alerts_for_source(self, source_id: str, kind: str | None = None) -> int:
        sql = "UPDATE alerts SET resolved_at = ? WHERE source_id = ? AND resolved_at IS NULL"
        params: list[Any] = [to_iso(), source_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        cursor = self.execute(sql, params)
        return cursor.rowcount or 0

    def list_alerts(self, limit: int = 50) -> list[Alert]:
        return [
            Alert.from_row(row)
            for row in self.query("SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,))
        ]

    # -- builds ------------------------------------------------------------
    def record_build(self, build: BuildRecord) -> None:
        row = build.to_row()
        columns = list(row)
        self.execute(
            f"INSERT INTO build_history ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [row[column] for column in columns],
        )

    def last_build(self) -> BuildRecord | None:
        row = self.query_one("SELECT * FROM build_history ORDER BY generated_at DESC LIMIT 1")
        return BuildRecord.from_row(row) if row else None

    def last_published_build(self) -> BuildRecord | None:
        row = self.query_one(
            "SELECT * FROM build_history WHERE published = 1 ORDER BY generated_at DESC LIMIT 1"
        )
        return BuildRecord.from_row(row) if row else None

    def list_builds(self, limit: int = 20) -> list[BuildRecord]:
        return [
            BuildRecord.from_row(row)
            for row in self.query("SELECT * FROM build_history ORDER BY generated_at DESC LIMIT ?", (limit,))
        ]

    # -- discoveries -------------------------------------------------------
    def record_discoveries(self, records: Iterable[DiscoveryRecord]) -> int:
        rows = []
        for record in records:
            row = record.to_row()
            rows.append([
                row["url"], row["normalized_url"], row["source_id"], row["adapter"],
                row["query"], row["found_at"], row["admitted"], row["reason"],
            ])
        self.executemany(
            "INSERT INTO discoveries (url, normalized_url, source_id, adapter, query, found_at, "
            "admitted, reason) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    def seen_recently(self, normalized_url: str, hours: float) -> bool:
        from ..utils.timeutil import days_ago

        row = self.query_one(
            "SELECT 1 AS hit FROM discoveries WHERE normalized_url = ? AND found_at >= ? LIMIT 1",
            (normalized_url, days_ago(hours / 24.0)),
        )
        return row is not None

    def recent_discoveries(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(row) for row in self.query(
            "SELECT * FROM discoveries ORDER BY found_at DESC LIMIT ?", (limit,)
        )]

    # -- events ------------------------------------------------------------
    def record_event(self, event: SourceEvent) -> None:
        self.record_events([event])

    def record_events(self, events: Iterable[SourceEvent]) -> int:
        rows = []
        for event in events:
            row = event.to_row()
            rows.append([row["source_id"], row["event"], row["detail"], row["from_status"], row["to_status"], row["ts"]])
        self.executemany(
            "INSERT INTO source_events (source_id, event, detail, from_status, to_status, ts) "
            "VALUES (?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    def events_for(self, source_id: str, limit: int = 50) -> list[SourceEvent]:
        return [
            SourceEvent(**{k: row[k] for k in ("source_id", "event", "detail", "from_status", "to_status", "ts")})
            for row in self.query(
                "SELECT * FROM source_events WHERE source_id = ? ORDER BY ts DESC LIMIT ?",
                (source_id, limit),
            )
        ]

    # -- nodes -------------------------------------------------------------
    def upsert_node(self, node: NodeRecord) -> None:
        row = node.to_row()
        self.execute(
            "INSERT INTO nodes (name, region, url, enabled, weight, last_seen_at, last_ok_at, "
            "consecutive_failures, note) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET region = excluded.region, url = excluded.url, "
            "enabled = excluded.enabled, weight = excluded.weight, last_seen_at = excluded.last_seen_at, "
            "last_ok_at = excluded.last_ok_at, consecutive_failures = excluded.consecutive_failures, "
            "note = excluded.note",
            (
                row["name"], row["region"], row["url"], row["enabled"], row["weight"],
                row["last_seen_at"], row["last_ok_at"], row["consecutive_failures"], row["note"],
            ),
        )

    def list_nodes(self) -> list[NodeRecord]:
        return [NodeRecord.from_row(row) for row in self.query("SELECT * FROM nodes ORDER BY region, name")]

    # -- maintenance -------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sources": self.count_sources(),
            "probes": self.count_probes(),
            "alerts": len(self.list_alerts(limit=100000)),
            "builds": len(self.list_builds(limit=100000)),
            "generated_at": to_iso(utcnow()),
            "path": str(self.path),
        }

    def checkpoint(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
