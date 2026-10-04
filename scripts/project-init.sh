#!/usr/bin/env bash
set -euo pipefail
./scripts/project-status.sh
./scripts/validate-project.sh || true
