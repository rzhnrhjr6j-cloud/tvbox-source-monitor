"""SQLite schema (spec §24).

Forward-only, additive migrations.  ``meta.schema_version`` records what has
been applied; a fresh database is created straight at the current version.
"""

from __future__ import annotations

import sqlite3

from ..utils.timeutil import to_iso

SCHEMA_VERSION = 3

_CORE_DDL = """
CREATE TABLE IF NOT EXISTS sources (
    id                        TEXT PRIMARY KEY,
    url                       TEXT NOT NULL,
    raw_url                   TEXT DEFAULT '',
    name                      TEXT DEFAULT '',
    type                      TEXT DEFAULT 'single',
    parent_id                 TEXT,
    schema                    TEXT DEFAULT '',
    status                    TEXT DEFAULT 'candidate',
    tier                      TEXT DEFAULT 'none',
    first_seen_at             TEXT,
    last_seen_at              TEXT,
    last_check_at             TEXT,
    last_success_at           TEXT,
    last_failure_at           TEXT,
    last_recovery_at          TEXT,
    last_outage_at            TEXT,
    updated_at                TEXT,
    score                     REAL DEFAULT 0,
    stability_score           REAL DEFAULT 0,
    search_score              REAL DEFAULT 0,
    playback_score            REAL DEFAULT 0,
    latency_score             REAL DEFAULT 0,
    regional_score            REAL DEFAULT 0,
    freshness_score           REAL DEFAULT 0,
    availability_score        REAL DEFAULT 0,
    candidate_score           REAL DEFAULT 0,
    previous_score            REAL,
    consecutive_success       INTEGER DEFAULT 0,
    consecutive_failure       INTEGER DEFAULT 0,
    max_consecutive_failure   INTEGER DEFAULT 0,
    max_consecutive_success   INTEGER DEFAULT 0,
    total_checks              INTEGER DEFAULT 0,
    total_successes           INTEGER DEFAULT 0,
    total_failures            INTEGER DEFAULT 0,
    global_status             TEXT DEFAULT 'UNKNOWN',
    regions_ok                TEXT DEFAULT '[]',
    regions_failed            TEXT DEFAULT '[]',
    node_count                INTEGER DEFAULT 0,
    health_level              TEXT DEFAULT 'UNKNOWN',
    tags                      TEXT DEFAULT '[]',
    note                      TEXT DEFAULT '',
    whitelisted               INTEGER DEFAULT 0,
    blacklisted               INTEGER DEFAULT 0,
    paused                    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status);
CREATE INDEX IF NOT EXISTS idx_sources_parent ON sources(parent_id);
CREATE INDEX IF NOT EXISTS idx_sources_score  ON sources(score DESC);

CREATE TABLE IF NOT EXISTS probe_results (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id                 TEXT NOT NULL,
    region                    TEXT DEFAULT '',
    node                      TEXT DEFAULT '',
    ts                        TEXT NOT NULL,
    http_ok                   INTEGER DEFAULT 0,
    status_code               INTEGER,
    dns_ok                    INTEGER DEFAULT 0,
    tls_ok                    INTEGER DEFAULT 0,
    response_ms               INTEGER,
    config_parse_success      INTEGER DEFAULT 0,
    schema_detected           TEXT DEFAULT '',
    search_success            INTEGER DEFAULT 0,
    search_result_count       INTEGER DEFAULT 0,
    search_first_ms           INTEGER,
    detail_success            INTEGER DEFAULT 0,
    detail_has_playlist       INTEGER DEFAULT 0,
    playback_url_obtained     INTEGER DEFAULT 0,
    playback_probe_success    INTEGER DEFAULT 0,
    playback_content_type     TEXT DEFAULT '',
    playback_first_byte_ms    INTEGER,
    health_level              TEXT DEFAULT 'UNKNOWN',
    score                     REAL DEFAULT 0,
    error_code                TEXT,
    error_message             TEXT,
    error_stage               TEXT DEFAULT '',
    checks_version            INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_probe_source_ts ON probe_results(source_id, ts);
CREATE INDEX IF NOT EXISTS idx_probe_region_ts ON probe_results(region, ts);

CREATE TABLE IF NOT EXISTS daily_stats (
    source_id                 TEXT NOT NULL,
    region                    TEXT NOT NULL,
    day                       TEXT NOT NULL,
    checks                    INTEGER DEFAULT 0,
    successes                 INTEGER DEFAULT 0,
    failures                  INTEGER DEFAULT 0,
    availability_percent      REAL DEFAULT 0,
    avg_response_ms           REAL,
    p50_ms                    REAL,
    p95_ms                    REAL,
    search_success_percent    REAL DEFAULT 0,
    playback_success_percent  REAL DEFAULT 0,
    PRIMARY KEY (source_id, region, day)
);
CREATE INDEX IF NOT EXISTS idx_daily_day ON daily_stats(day);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     TEXT,
    region        TEXT DEFAULT '',
    kind          TEXT DEFAULT '',
    severity      TEXT DEFAULT 'warning',
    title         TEXT DEFAULT '',
    message       TEXT DEFAULT '',
    fingerprint   TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    suppressed    INTEGER DEFAULT 0,
    channels      TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_alerts_fp ON alerts(fingerprint, created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(resolved_at, created_at);

CREATE TABLE IF NOT EXISTS discoveries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT NOT NULL,
    normalized_url TEXT DEFAULT '',
    source_id      TEXT DEFAULT '',
    adapter        TEXT DEFAULT '',
    query          TEXT DEFAULT '',
    found_at       TEXT NOT NULL,
    admitted       INTEGER DEFAULT 0,
    reason         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_discoveries_norm ON discoveries(normalized_url, found_at);

CREATE TABLE IF NOT EXISTS source_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    TEXT NOT NULL,
    event        TEXT NOT NULL,
    detail       TEXT DEFAULT '',
    from_status  TEXT DEFAULT '',
    to_status    TEXT DEFAULT '',
    ts           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_source ON source_events(source_id, ts);

CREATE TABLE IF NOT EXISTS build_history (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id           TEXT NOT NULL,
    generated_at       TEXT NOT NULL,
    git_sha            TEXT DEFAULT '',
    active_count       INTEGER DEFAULT 0,
    degraded_count     INTEGER DEFAULT 0,
    failed_count       INTEGER DEFAULT 0,
    new_count          INTEGER DEFAULT 0,
    recovered_count    INTEGER DEFAULT 0,
    removed_count      INTEGER DEFAULT 0,
    source_count       INTEGER DEFAULT 0,
    published          INTEGER DEFAULT 0,
    fallback           INTEGER DEFAULT 0,
    blocked_reason     TEXT DEFAULT '',
    output_sha256      TEXT DEFAULT '',
    primary_count      INTEGER DEFAULT 0,
    backup_count       INTEGER DEFAULT 0,
    experimental_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_build_gen ON build_history(generated_at DESC);

CREATE TABLE IF NOT EXISTS nodes (
    name                  TEXT PRIMARY KEY,
    region                TEXT NOT NULL,
    url                   TEXT DEFAULT '',
    enabled               INTEGER DEFAULT 1,
    weight                REAL DEFAULT 1.0,
    last_seen_at          TEXT,
    last_ok_at            TEXT,
    consecutive_failures  INTEGER DEFAULT 0,
    note                  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
"""


def get_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def migrate(conn: sqlite3.Connection) -> int:
    """Create or upgrade the schema.  Returns the resulting version."""
    conn.executescript(_CORE_DDL)

    version = get_version(conn)

    # --- v1 -> v2: keep a rolling window of probe rows ---------------------
    if version < 2:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_probe_ts ON probe_results(ts)")

    # --- v2 -> v3: provenance for outputs and alerts -----------------------
    if version < 3:
        _add_column(conn, "build_history", "output_sha256", "TEXT DEFAULT ''")
        _add_column(conn, "build_history", "primary_count", "INTEGER DEFAULT 0")
        _add_column(conn, "build_history", "backup_count", "INTEGER DEFAULT 0")
        _add_column(conn, "build_history", "experimental_count", "INTEGER DEFAULT 0")

    if version != SCHEMA_VERSION:
        conn.execute(
            "INSERT INTO meta(key, value, updated_at) VALUES('schema_version', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (str(SCHEMA_VERSION), to_iso()),
        )
    conn.commit()
    return SCHEMA_VERSION


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
