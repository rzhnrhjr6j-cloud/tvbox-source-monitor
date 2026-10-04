#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo '=== V1.2 PROJECT AUDIT ==='
"$ROOT/scripts/project-status.sh" || true
python3 "$ROOT/scripts/validate_project.py" || true
python3 "$ROOT/scripts/check_traceability.py" || true
python3 "$ROOT/scripts/check_drift.py" || true
python3 "$ROOT/scripts/check_change_integrity.py" || true
