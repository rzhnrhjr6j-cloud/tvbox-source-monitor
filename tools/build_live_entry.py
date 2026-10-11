#!/usr/bin/env python3
"""Rebuild only ``dist/tvbox-live.json`` (the 直播入口).

The nightly build already does this; the tool exists so the entry can be
refreshed on its own, from the configs that are already published under
``dist/sources``, without paying for a full build.

    .venv/bin/python tools/build_live_entry.py [--dist dist] [--version 1.0]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.build.live import (  # noqa: E402
    LIVE_FILE,
    SOURCES_DIR,
    build_live_entry,
    configs_from_directory,
    write_live_entry,
)
from app.config import load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", default="dist", help="directory holding the published site")
    parser.add_argument("--version", default=None, help="value for the entry's version field")
    parser.add_argument("--limit", type=int, default=0, help="0 means every playlist")
    args = parser.parse_args(argv)

    dist = Path(args.dist)
    version = args.version or str(load_config().get("app.version", "1.0"))
    configs = configs_from_directory(dist / SOURCES_DIR)
    payload = build_live_entry(configs, version=version, max_playlists=args.limit)
    if not payload["lives"]:
        print("no live playlist found under %s - nothing written" % (dist / SOURCES_DIR))
        return 1
    path = write_live_entry(dist / LIVE_FILE, payload)
    print(json.dumps({
        "written": str(path),
        "configs": len(configs),
        "playlists": len(payload["lives"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
