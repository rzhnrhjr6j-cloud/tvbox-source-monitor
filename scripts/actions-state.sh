#!/usr/bin/env bash
set -euo pipefail

# Persist the runtime SQLite database as a compressed GitHub Release asset so
# scheduled workflows never need to commit a database that can exceed Git's
# per-file limit.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${TVBOX_STATE_DB:-${TVBOX_DB_PATH:-data/monitor.db}}"
case "$DB_PATH" in
  /*) ;;
  *) DB_PATH="$ROOT/$DB_PATH" ;;
esac

RELEASE_TAG="${TVBOX_STATE_RELEASE_TAG:-automation-state-v1}"
ASSET_PREFIX="${TVBOX_STATE_ASSET_PREFIX:-monitor.db}"
LEGACY_REF="${TVBOX_STATE_LEGACY_REF:-f65d6b8}"
KEEP_ASSETS="${TVBOX_STATE_KEEP_ASSETS:-5}"
TMP_DIR=""

log() {
  printf '[actions-state] %s\n' "$*" >&2
}

fail() {
  log "ERROR: $*"
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

cleanup() {
  if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
    rm -rf "$TMP_DIR"
  fi
}

validate_db() {
  local path="$1"
  python3 - "$path" <<'PY'
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1]).resolve()
if not path.is_file() or path.stat().st_size == 0:
    raise SystemExit(f"database is missing or empty: {path}")

uri = path.as_uri() + "?mode=ro"
try:
    conn = sqlite3.connect(uri, uri=True)
    check = conn.execute("PRAGMA quick_check(1)").fetchone()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    version = None
    if "meta" in tables:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        version = row[0] if row else None
    conn.close()
except Exception as exc:
    raise SystemExit(f"database validation failed: {exc}") from exc

if not check or check[0] != "ok":
    raise SystemExit(f"database quick_check failed: {check!r}")
missing = {"meta", "sources", "probe_results"} - tables
if missing:
    raise SystemExit(f"database schema is incomplete: missing {sorted(missing)}")
if version is None:
    raise SystemExit("database schema version is missing")
print(f"database validated: schema_version={version}")
PY
}

snapshot_db() {
  local source="$1"
  local destination="$2"
  python3 - "$source" "$destination" <<'PY'
import sqlite3
import sys

source, destination = sys.argv[1:3]
src = sqlite3.connect(source, timeout=30)
src.execute("PRAGMA query_only=ON")
dst = sqlite3.connect(destination)
try:
    src.backup(dst)
    dst.execute("PRAGMA journal_mode=DELETE")
    dst.execute("VACUUM")
finally:
    dst.close()
    src.close()
PY
}

repo_slug() {
  if [[ -n "${GH_REPO:-}" ]]; then
    printf '%s' "$GH_REPO"
    return
  fi
  if [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
    printf '%s' "$GITHUB_REPOSITORY"
    return
  fi
  gh repo view --json nameWithOwner --jq .nameWithOwner
}

restore_archive() {
  local archive="$1"
  local destination="$2"
  gzip -t "$archive" >/dev/null 2>&1 || return 1
  gzip -dc "$archive" > "$destination"
  validate_db "$destination" >/dev/null
}

install_restored_db() {
  local candidate="$1"
  local source="$2"
  if [[ -e "$DB_PATH" ]]; then
    local backup="${DB_PATH}.invalid.$(date -u +%Y%m%dT%H%M%SZ)"
    mv "$DB_PATH" "$backup"
    local suffix
    for suffix in -wal -shm; do
      if [[ -e "${DB_PATH}${suffix}" ]]; then
        mv "${DB_PATH}${suffix}" "${backup}${suffix}"
      fi
    done
    log "preserved invalid database at $backup"
  else
    rm -f "${DB_PATH}-wal" "${DB_PATH}-shm"
  fi
  mv "$candidate" "$DB_PATH"
  log "restored database from $source"
}

restore_state() {
  need python3
  mkdir -p "$(dirname "$DB_PATH")"

  if [[ -f "$DB_PATH" ]] && validate_db "$DB_PATH" >/dev/null 2>&1; then
    log "using existing database: $DB_PATH"
    return
  fi

  TMP_DIR="$(mktemp -d)"
  local candidate="$TMP_DIR/monitor.db"

  need gh
  local repo
  repo="$(repo_slug)"
  export GH_REPO="$repo"

  if gh release view "$RELEASE_TAG" >/dev/null 2>&1; then
    local asset
    asset="$(
      gh api "repos/$repo/releases/tags/$RELEASE_TAG" \
        --jq "[.assets[] | select(.name | startswith(\"$ASSET_PREFIX.\"))] | sort_by(.created_at) | reverse | .[0].name // empty"
    )"
    if [[ -n "$asset" ]]; then
      log "downloading latest release asset: $asset"
      if gh release download "$RELEASE_TAG" --pattern "$asset" --dir "$TMP_DIR"; then
        if restore_archive "$TMP_DIR/$asset" "$candidate"; then
          install_restored_db "$candidate" "release asset $asset"
          return
        fi
        log "release asset validation failed; trying legacy database"
      fi
    fi
  else
    log "release $RELEASE_TAG does not exist yet"
  fi

  need curl
  local legacy_url="https://raw.githubusercontent.com/$repo/$LEGACY_REF/data/monitor.db"
  log "downloading legacy database from $LEGACY_REF"
  if curl -fL --retry 3 --retry-delay 2 "$legacy_url" -o "$candidate"; then
    if validate_db "$candidate" >/dev/null 2>&1; then
      install_restored_db "$candidate" "legacy commit $LEGACY_REF"
      return
    fi
  fi

  fail "no valid database could be restored from an existing file, release asset, or legacy commit"
}

file_sha256() {
  python3 - "$1" <<'PY'
import hashlib
import sys
from pathlib import Path

digest = hashlib.sha256()
with Path(sys.argv[1]).open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
}

ensure_release() {
  local repo="$1"
  if gh release view "$RELEASE_TAG" >/dev/null 2>&1; then
    return
  fi
  local target="${GITHUB_SHA:-$(git rev-parse HEAD)}"
  log "creating release $RELEASE_TAG at $target"
  gh release create "$RELEASE_TAG" \
    --target "$target" \
    --title "Automation SQLite state" \
    --notes "Compressed runtime SQLite snapshots used by scheduled workflows."
}

prune_release_assets() {
  local repo="$1"
  local current_asset="$2"
  local names
  names="$(
    gh api "repos/$repo/releases/tags/$RELEASE_TAG" \
      --jq "[.assets[] | select(.name | startswith(\"$ASSET_PREFIX.\"))] | sort_by(.created_at) | reverse | .[$KEEP_ASSETS:] | .[].name"
  )"
  [[ -n "$names" ]] || return 0

  while IFS= read -r name; do
    [[ -n "$name" && "$name" != "$current_asset" ]] || continue
    log "removing old release asset: $name"
    gh release delete-asset "$RELEASE_TAG" "$name" --yes || \
      log "WARNING: could not remove old release asset: $name"
  done <<< "$names"
}

save_state() {
  local dry_run="$1"
  need python3
  need gzip
  [[ "$KEEP_ASSETS" =~ ^[1-9][0-9]*$ ]] || fail "TVBOX_STATE_KEEP_ASSETS must be a positive integer"
  [[ -f "$DB_PATH" ]] || fail "database does not exist: $DB_PATH"
  validate_db "$DB_PATH" >/dev/null

  TMP_DIR="$(mktemp -d)"
  local snapshot="$TMP_DIR/monitor.db"
  local timestamp
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local run_id="${GITHUB_RUN_ID:-local}"
  local run_attempt="${GITHUB_RUN_ATTEMPT:-0}"
  local asset="$ASSET_PREFIX.$timestamp.$run_id.$run_attempt.gz"
  local archive="$TMP_DIR/$asset"

  snapshot_db "$DB_PATH" "$snapshot"
  validate_db "$snapshot" >/dev/null
  gzip -9 -n -c "$snapshot" > "$archive"
  gzip -t "$archive"

  local size sha
  size="$(wc -c < "$archive" | tr -d ' ')"
  sha="$(file_sha256 "$archive")"
  log "snapshot validated: asset=$asset size=$size sha256=$sha"

  if [[ "$dry_run" == "true" ]]; then
    log "dry run: skipped release upload"
    return
  fi

  need gh
  local repo
  repo="$(repo_slug)"
  export GH_REPO="$repo"
  ensure_release "$repo"
  log "uploading release asset: $asset"
  gh release upload "$RELEASE_TAG" "$archive"
  prune_release_assets "$repo" "$asset"
  log "saved database as release asset: $asset"
}

usage() {
  cat <<'EOF'
Usage: scripts/actions-state.sh <restore|save> [--dry-run]

Environment:
  TVBOX_STATE_DB              SQLite path (default: TVBOX_DB_PATH or data/monitor.db)
  TVBOX_STATE_RELEASE_TAG     GitHub Release tag (default: automation-state-v1)
  TVBOX_STATE_LEGACY_REF      Bootstrap commit (default: f65d6b8)
  TVBOX_STATE_KEEP_ASSETS     Number of release snapshots to retain (default: 5)
  GH_REPO / GITHUB_REPOSITORY Repository slug used by gh
EOF
}

main() {
  trap cleanup EXIT
  local command="${1:-}"
  [[ -n "$command" ]] || {
    usage
    exit 2
  }
  shift || true

  local dry_run=false
  while (($#)); do
    case "$1" in
      --dry-run) dry_run=true ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        fail "unknown argument: $1"
        ;;
    esac
    shift
  done

  case "$command" in
    restore)
      [[ "$dry_run" == "false" ]] || fail "restore does not support --dry-run"
      restore_state
      ;;
    save)
      save_state "$dry_run"
      ;;
    -h|--help)
      usage
      ;;
    *)
      fail "unknown command: $command"
      ;;
  esac
}

main "$@"
