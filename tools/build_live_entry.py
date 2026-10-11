#!/usr/bin/env python3
"""Rebuild only ``dist/tvbox-live.json`` (the 直播入口).

The nightly build already does this; the tool exists so the entry can be
refreshed on its own, from the configs that are already published under
``dist/sources``, without paying for a full build.

    .venv/bin/python tools/build_live_entry.py [--dist dist] [--version 1.0]
    .venv/bin/python tools/build_live_entry.py --no-probe   # skip the fetch pass
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.build.live import (  # noqa: E402
    LIVE_FILE,
    MIRROR_DIR,
    SOURCES_DIR,
    build_live_entry,
    configs_from_directory,
    probe_playlists,
    rank_live_entry,
    write_live_entry,
)
from app.config import load_config  # noqa: E402
from app.utils.http_client import HttpClient  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", default="dist", help="directory holding the published site")
    parser.add_argument("--version", default=None, help="value for the entry's version field")
    parser.add_argument("--limit", type=int, default=0, help="0 means every playlist")
    parser.add_argument("--probe", dest="probe", action="store_true", default=None,
                        help="force the fetch pass on (default: follow config/app.yaml)")
    parser.add_argument("--no-probe", dest="probe", action="store_false",
                        help="skip the fetch pass and publish every playlist in source order")
    args = parser.parse_args(argv)

    dist = Path(args.dist)
    cfg = load_config()
    version = args.version or str(cfg.get("app.version", "1.0"))
    configs = configs_from_directory(dist / SOURCES_DIR)
    payload = build_live_entry(configs, version=version, max_playlists=args.limit)
    if not payload["lives"]:
        print("no live playlist found under %s - nothing written" % (dist / SOURCES_DIR))
        return 1

    probe_cfg = ((cfg.get("output.live") or {}).get("probe") or {})
    enabled = bool(probe_cfg.get("enabled", False)) if args.probe is None else args.probe
    stats: dict[str, Any] = {"probed": False}
    if enabled:
        report = probe_playlists(
            payload["lives"],
            client=HttpClient(cfg.section("http")),
            local_root=dist / MIRROR_DIR,
            workers=int(probe_cfg.get("workers", 16) or 16),
            connect_timeout=float(probe_cfg.get("connect_timeout", 5) or 5),
            read_timeout=float(probe_cfg.get("read_timeout", 8) or 8),
            max_bytes=int(probe_cfg.get("max_bytes", 1_048_576) or 1_048_576),
        )
        payload, stats = rank_live_entry(
            payload,
            report,
            drop=str(probe_cfg.get("drop", "dead") or "dead"),
            max_dead_ratio=float(probe_cfg.get("max_dead_ratio", 0.6) or 0.6),
        )
        stats["probed"] = True
        stats["details"] = {
            url: detail for url, detail in report.details.items()
            if report.verdicts.get(url) != "alive"
        }

    if not payload["lives"]:
        print("probe left no playlist - nothing written")
        return 1
    path = write_live_entry(dist / LIVE_FILE, payload)
    print(json.dumps({
        "written": str(path),
        "configs": len(configs),
        "playlists": len(payload["lives"]),
        "probe": stats,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
