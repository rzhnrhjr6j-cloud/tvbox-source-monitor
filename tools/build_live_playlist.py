#!/usr/bin/env python3
"""Rebuild the merged 直播 playlist (``dist/live.m3u`` + ``dist/live.txt``).

Usage::

    .venv/bin/python tools/build_live_playlist.py             # 抓清单 + 实测每条频道
    .venv/bin/python tools/build_live_playlist.py --no-probe  # 只合并，用本地缓存排序

实测只有在大陆网络里跑才准：GitHub 构建机够不到大陆的流。跑完的结论落在
``data/live_probe.json``（提交进仓库），构建时按它把能播的排前面。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.build.live_playlist import (  # noqa: E402
    ALIVE,
    CACHE_FILE,
    DEAD,
    assemble,
    fetch_channels,
    load_verdicts,
    merge_channels,
    probe_channels,
    save_verdicts,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "live.yaml"


def _config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


def _fetch(session: requests.Session, prefix: str, timeout: float):
    def fetch(url: str, mode: str) -> str:
        target = f"{prefix}{url}" if mode == "proxy" and prefix else url
        response = session.get(
            target,
            timeout=timeout,
            headers={"User-Agent": "tvbox-source-monitor/1.0"},
        )
        response.raise_for_status()
        return response.text

    return fetch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", default="dist")
    parser.add_argument("--no-probe", dest="probe", action="store_false", default=None)
    args = parser.parse_args(argv)

    cfg = _config()
    out_cfg = cfg.get("output") or {}
    probe_cfg = cfg.get("probe") or {}
    cache = ROOT / str(probe_cfg.get("cache") or CACHE_FILE)
    verdicts = load_verdicts(cache)

    session = requests.Session()
    fetch = _fetch(session, str(cfg.get("proxy_prefix") or ""), float(cfg.get("fetch_timeout", 20) or 20))
    channels, failed = fetch_channels(cfg.get("sources") or [], fetch)
    print(f"上游清单：{len(cfg.get('sources') or [])} 个，抓到频道 {len(channels)} 条"
          + (f"，失败 {failed}" if failed else ""))
    if not channels:
        print("一条频道都没抓到，什么都没写")
        return 1

    def _merged(verdicts: dict[str, str], *, require_alive: bool = True):
        return merge_channels(
            [channels],
            verdicts=verdicts,
            max_per_name=int(out_cfg.get("max_per_name", 3) or 3),
            max_channels=int(out_cfg.get("max_channels", 0) or 0),
            max_per_group=out_cfg.get("max_per_group") or {},
            require_alive_groups=(out_cfg.get("require_alive_groups") or ()) if require_alive else (),
        )

    enabled = bool(probe_cfg.get("enabled", True)) if args.probe is None else args.probe
    if enabled:
        workers = int(probe_cfg.get("workers", 24) or 24)
        connect_timeout = float(probe_cfg.get("connect_timeout", 5) or 5)
        read_timeout = float(probe_cfg.get("read_timeout", 12) or 12)
        # 只实测合并后真正要发出去的线路，省掉一大半无用请求。
        # 注意必须「不套 require_alive_groups」地取候选：一条从没测过的新线路
        # 在那种规则下会被当成「没有实测能播的线路」提前踢掉，永远轮不到实测。
        candidates = _merged(verdicts, require_alive=False)
        fresh = probe_channels(
            candidates,
            workers=workers,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
        # 「必须能播」的那两组（港澳台 / 国外）要求连着测两遍都算能播才认。
        # 这些台大多挂在小 CDN 上，单测一遍会把「2xx 其实是错误页」「清单在、分片
        # 404」的假活当成活发出去，用户点进去正好是「播放失败」。
        strict = {str(group) for group in (out_cfg.get("require_alive_groups") or ())}
        retry = [c for c in candidates if c.group in strict and fresh.get(c.url) == ALIVE]
        if retry:
            second = probe_channels(
                retry,
                workers=workers,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            )
            flaky = {url for url, verdict in second.items() if verdict != ALIVE}
            for url in flaky:
                fresh[url] = second[url]
            print(f"复测：严格组里第一遍算能播的 {len(retry)} 条，复测掉 {len(flaky)} 条")
        verdicts.update(fresh)
        save_verdicts(cache, verdicts)
        alive = sum(1 for value in fresh.values() if value == ALIVE)
        print(f"实测：{len(fresh)} 条地址，能连上 {alive} 条（结论写进 {cache.relative_to(ROOT)}）")

    dead = {channel.url for channel in channels if verdicts.get(channel.url) == DEAD}
    merged, _ = assemble(
        dist_dir=ROOT / args.dist,
        out_cfg=out_cfg,
        verdicts=verdicts,
        channels=channels,
    )
    if not merged:
        print("合并后一条都不剩，什么都没写")
        return 1
    written = [ROOT / args.dist / str(out_cfg.get("file") or "live.m3u"),
               ROOT / args.dist / str(out_cfg.get("txt_file") or "live.txt")]
    names = len({channel.name for channel in merged})
    print(f"写出 {len(merged)} 条线路 / {names} 个频道，踢掉已确认 404 的 {len(dead)} 条")

    bases = [str(base).rstrip("/") for base in (out_cfg.get("bases") or [])]
    m3u = str(out_cfg.get("file") or "live.m3u")
    print(json.dumps({
        "written": [str(path.relative_to(ROOT)) for path in written],
        "channels": names,
        "lines": len(merged),
        "urls": [f"{base}/{m3u}" for base in bases],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
