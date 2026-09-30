#!/usr/bin/env python3
"""Thin shim: ``python scripts/bootstrap.py`` == ``python -m app.main bootstrap``."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["bootstrap", *sys.argv[1:]]))
