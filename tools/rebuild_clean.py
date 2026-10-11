"""Regenerate the child-safe entry without running the whole (network) build.

``dist/tvbox-clean.json`` is normally produced by ``Builder._publish_clean`` at
the end of a full build.  When only the ``output.clean`` policy changed, the
full pipeline would need a fresh probe/ mirror pass that has nothing to do with
the filter.  This driver replays exactly that one step against the already
published ``dist/tvbox.json`` + ``dist/sources/*.json``, using the same writer,
so the committed artefacts are byte-identical to what CI would emit.

    .venv/bin/python tools/rebuild_clean.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app.build.builder import SOURCES_DIR, _atomic_write_json  # noqa: E402
from app.build.clean import (  # noqa: E402
    CleanPolicy,
    build_clean_entry,
    http_fetcher,
    make_live_scanner,
    scan_unsafe_refs,
)
from app.build.mirror import JARS_DIR, _load_config  # noqa: E402
from app.utils.http_client import HttpClient  # noqa: E402


def main() -> int:
    dist = ROOT / "dist"
    config = yaml.safe_load((ROOT / "config" / "app.yaml").read_text(encoding="utf-8"))
    output_cfg = config.get("output") or {}
    policy = CleanPolicy.from_settings(output_cfg.get("clean"))
    if not policy.enabled:
        print("output.clean.enabled is false - nothing to do")
        return 1

    entry_path = dist / "tvbox.json"
    data = _load_config(entry_path.read_text(encoding="utf-8", errors="replace"))
    urls = [item.get("url") for item in data.get("urls", []) if isinstance(item, dict)]
    first = next((u for u in urls if isinstance(u, str) and f"/{SOURCES_DIR}/" in u), None)
    if not first:
        print("cannot derive the source-url prefix from dist/tvbox.json")
        return 1
    base = first[: first.rindex(f"/{SOURCES_DIR}/")]
    pretty = bool(output_cfg.get("pretty", True))

    def read_child(slug: str):
        path = dist / SOURCES_DIR / slug
        if not path.is_file():
            return None
        try:
            child = _load_config(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
        return child if isinstance(child, dict) else None

    client = HttpClient(config.get("http") or {})
    live_scan = make_live_scanner(policy, http_fetcher(client))
    try:
        result = build_clean_entry(
            data,
            policy,
            read_child,
            source_prefix=f"{base}/{SOURCES_DIR}/",
            clean_prefix=f"{base}/{policy.directory}/{SOURCES_DIR}/",
            unsafe_refs=scan_unsafe_refs(dist / JARS_DIR, policy),
            live_scan=live_scan,
        )
    finally:
        client.close()

    target = dist / policy.directory / SOURCES_DIR
    target.mkdir(parents=True, exist_ok=True)
    for stale in target.glob("*.json"):
        if stale.name not in result.children:
            stale.unlink()
    for slug, child in result.children.items():
        _atomic_write_json(target / slug, child, pretty=pretty)
    _atomic_write_json(dist / policy.output_file, result.payload, pretty=pretty)

    total = len(data.get("urls", []))
    print(f"full entry items : {total}")
    print(f"clean entry items: {len(result.payload.get('urls', []))}")
    print(f"dropped sources  : {len(result.dropped_sources)}")
    for name in result.dropped_sources:
        print(f"  - {name}")
    print(f"rows removed     : {result.removed_rows}")
    print(f"configs rewritten: {result.rewritten}")
    print(f"live playlists   : {live_scan.stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
