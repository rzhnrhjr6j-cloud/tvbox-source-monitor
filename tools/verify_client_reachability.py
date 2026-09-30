#!/usr/bin/env python3
"""Check the published list the way 影视仓 checks it: from *this* machine.

The health pipeline runs on GitHub runners, which reach every host in the
world.  The client runs in mainland China and does not.  That gap is why a
catalogue can be 100% "healthy" and still open nothing, so the only test that
means anything is the one run on the network the client actually uses.

Run it after every rebuild:

    python3 tools/verify_client_reachability.py

It fetches the published ``tvbox.json``, every child config, and every *file*
the client would load when a source is opened - the crawler jar and the spider
config - printing one line per source.  Exit code 1 means at least one source
would fail to open.

``api``, ``ext`` and ``parses[]`` are deliberately not checked: they are live
endpoints, not files, and a bare GET of one is not a reachability signal.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import urllib.request

DEFAULT_ENTRY = "https://rzhnrhjr6j-cloud.github.io/tvbox-source-monitor/tvbox.json"
TAIL = re.compile(r"(\$.*|;md5;.*)$")
OK = {200, 206, 301, 302, 303, 307, 308}


def strip_tail(reference: str) -> str:
    found = TAIL.search(reference)
    return reference[: found.start()] if found else reference


def fetch(url: str, timeout: float) -> tuple[int, int]:
    """Range-request ``url`` and return ``(status, bytes)``; 0 on any failure."""
    request = urllib.request.Request(url, headers={
        "Range": "bytes=0-1023",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "User-Agent": "okhttp/3.15",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, len(response.read())
    except urllib.error.HTTPError as error:
        return error.code, 0
    except Exception:
        return 0, 0


def references(config) -> list[str]:
    """Every file the client loads when the source is opened."""
    if isinstance(config, list):
        rows, spider = config, None
    else:
        rows = config.get("sites") or []
        spider = config.get("spider")
    found: list[str] = []
    if isinstance(spider, str):
        found.append(spider)
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("jar"), str):
            found.append(row["jar"])
    return [strip_tail(r) for r in found if str(r).startswith(("http://", "https://"))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", default=os.environ.get("TVBOX_ENTRY", DEFAULT_ENTRY))
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--sample", type=int, default=0,
                        help="only test this many jars per source (0 = all)")
    args = parser.parse_args()

    print(f"入口: {args.entry}")
    status, size = fetch(args.entry, args.timeout)
    if status not in OK:
        print(f"入口本身就打不开: HTTP {status}")
        return 1
    print("入口可达 ✓\n")

    with urllib.request.urlopen(args.entry, timeout=args.timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("urls") if isinstance(payload, dict) else payload

    jobs: list[tuple[str, str, str]] = []
    for row in rows or []:
        name = str(row.get("name") or "?")
        url = row.get("url")
        if not url:
            continue
        child_status, _ = fetch(url, args.timeout)
        if child_status not in OK:
            jobs.append((name, "子配置", url))
            continue
        with urllib.request.urlopen(url, timeout=args.timeout) as response:
            config = json.loads(response.read().decode("utf-8"))
        refs = references(config)
        if args.sample:
            refs = refs[: args.sample]
        for reference in refs:
            jobs.append((name, "文件", reference))

    print(f"待检查 {len(jobs)} 个引用，开始并发探测（无代理）…\n")
    results: dict[str, list[tuple[str, str, int]]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch, url, args.timeout): (name, kind, url)
                   for name, kind, url in jobs}
        for future in concurrent.futures.as_completed(futures):
            name, kind, url = futures[future]
            code, _ = future.result()
            results.setdefault(name, []).append((kind, url, code))

    broken = 0
    for name in sorted(results):
        bad = [item for item in results[name] if item[2] not in OK]
        state = "通" if not bad else f"断 {len(bad)}"
        print(f"[{state:>5}] {name}  ({len(results[name])} 个引用)")
        for kind, url, code in bad[:4]:
            print(f"          {kind} HTTP {code}  {url[:90]}")
        broken += 1 if bad else 0

    print(f"\n结论: {len(results)} 个源里 {len(results) - broken} 个在你这台机器上打得开，"
          f"{broken} 个打不开。")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
