"""Live-TV entry point (直播入口).

``tvbox.json`` is published in ``multi`` form, so it carries nothing but
``urls``.  Clients in the tvbox family do not reliably merge ``lives`` out of
those children, which leaves the 直播 tab empty.  The very same playlists are
therefore published a second time as a tiny standalone config
(``{"lives": [...]}``) that the 直播源 field can load on its own.

Every playlist url in there has already been mirrored by
:mod:`app.build.mirror`, so it points at this repository rather than at an
upstream that may disappear.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from ..logging_setup import get_logger
from ..utils.timeutil import to_iso

LOGGER = get_logger("build.live")

LIVE_FILE = "tvbox-live.json"
SOURCES_DIR = "sources"
# playlists mirrored into the repo land in this directory, named after their
# own bytes; a url that points there can be judged from disk, not by a probe
MIRROR_DIR = "jars"

# probe verdicts.  ``unknown`` is deliberately not a soft "dead": a runner that
# cannot reach a mainland-only host must never be allowed to delete it.
ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"
_DEAD_STATUS = frozenset({404, 410})

_PLAYABLE = ("http://", "https://")
# a playlist that only its author's own machine can reach must not ship
_LOOPBACK_HOSTS = frozenset({"localhost", "0.0.0.0", "::1", "[::1]"})
# what a tvbox client understands on a live entry; everything else is noise
_FIELDS = ("name", "type", "url", "epg", "logo", "playerType", "ua", "origin", "header")


def _parse(text: Any) -> Any:
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", "replace")
    if isinstance(text, str):
        try:
            return json.loads(text)
        except ValueError:
            return None
    return text


def _playable_url(item: dict[str, Any]) -> str | None:
    url = item.get("url")
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url.lower().startswith(_PLAYABLE):
        return None
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    if host in _LOOPBACK_HOSTS or host.startswith("127."):
        return None
    return url


def _live_items(config: Any) -> list[dict[str, Any]]:
    if not isinstance(config, dict):
        return []
    items = config.get("lives")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _as_type(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def configs_from_plan(plan: Any) -> list[Any]:
    """Configs of the running build; mirror entries already hold the text."""
    entries = getattr(plan, "entries", None)
    if not isinstance(entries, dict):
        return []
    return [cfg for cfg in (_parse(entry.content) for entry in entries.values()) if cfg]


def configs_from_directory(directory: Path) -> list[Any]:
    """Configs already published under ``dist/sources``."""
    if not directory.is_dir():
        return []
    configs = []
    for path in sorted(directory.glob("*.json")):
        try:
            configs.append(_parse(path.read_bytes()))
        except OSError:
            continue
    return [cfg for cfg in configs if cfg]


def build_live_entry(
    configs: Iterable[Any],
    *,
    version: str = "1.0",
    max_playlists: int = 0,
) -> dict[str, Any]:
    """One ``lives`` list, deduped by url and by name, in source order.

    Callers that find no playlist at all get an empty list back; publishing an
    empty entry would only give the client a 直播 tab that loads nothing.
    """
    lives: list[dict[str, Any]] = []
    urls: set[str] = set()
    names: set[str] = set()
    unusable = 0
    for config in configs:
        for item in _live_items(config):
            url = _playable_url(item)
            if url is None:
                unusable += 1
                continue
            if url.rstrip("/").lower() in urls:
                continue
            name = str(item.get("name") or "").strip() or f"直播{len(lives) + 1}"
            if name in names:
                continue
            urls.add(url.rstrip("/").lower())
            names.add(name)
            entry = {
                field: item[field]
                for field in _FIELDS
                if field in item and item[field] not in (None, "")
            }
            entry["name"] = name
            entry["url"] = url
            entry["type"] = _as_type(item.get("type"))
            lives.append(entry)
    if max_playlists > 0:
        lives = lives[:max_playlists]
    LOGGER.info("live entry assembled", extra={
        "stage": "build", "check": "live",
        "playlists": len(lives), "unusable": unusable,
    })
    return {"version": version, "generated_at": to_iso(), "lives": lives}


def write_live_entry(path: Path, payload: dict[str, Any], *, pretty: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Live probing
#
# The live entry is a list of *files* the client fetches and parses.  A file
# that is gone upstream - 30 of 136 in the first real run - shows up in 影视仓
# as 解析失败, next to a perfectly good neighbour, and the user reads the whole
# list as broken.  So every playlist is fetched once during the build, the ones
# that answer with a real playlist are moved to the front, and only the ones
# that are provably gone are dropped.
# ---------------------------------------------------------------------------


def looks_like_playlist(text: str) -> bool:
    """True when *text* is something a tvbox 直播 tab can actually read.

    Two shapes ship in the wild: m3u (``#EXTINF``) and the flat txt list
    (``频道名,http://...``).  An HTML error page, an empty body and a
    ``404: Not Found`` stub all fail this test, which is the point.  A bare
    ``#EXTM3U`` with no channel under it is *not* a playlist - two real
    entries were exactly that, and both are dead in the client.
    """
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped or stripped[0] == "<":
        return False
    upper = stripped.upper()
    if "#EXTINF" in upper or "#GENRE" in upper:
        return True
    channels = [
        line for line in stripped.splitlines()
        if line.strip() and not line.strip().startswith("#") and "://" in line
    ]
    return len(channels) >= 2


def _json_looks_live(payload: Any) -> bool:
    """A live api answers with a list; an error stub answers with a code."""
    if isinstance(payload, list):
        return bool(payload)
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, (list, dict)) and value:
                return True
            if isinstance(value, str) and "://" in value:
                return True
    return False


def _classify(text: str, *, status: int | None, ok: bool) -> tuple[str, str]:
    """Turn one fetch into ``(verdict, detail)``.

    A 2xx that is not a playlist is dead (an error page or an empty stub
    served with 200).  Only 404/410 count as provably gone; every other
    failure - timeout, reset, TLS, 5xx - is ``unknown`` and is kept, because a
    GitHub runner is not the client and must not speak for it.
    """
    if ok:
        if looks_like_playlist(text):
            return ALIVE, "playlist"
        stripped = text.strip()
        if stripped[:1] in ("{", "["):
            try:
                payload = json.loads(stripped)
            except ValueError:
                return DEAD, f"broken json ({len(stripped)}b)"
            return (ALIVE, "json") if _json_looks_live(payload) else (DEAD, f"json stub ({len(stripped)}b)")
        if stripped[:1] == "<" or len(stripped) < 512:
            # an html page served with 200, or a stub: the client cannot parse
            # either one, and both are what "解析失败" is made of
            return DEAD, f"not a playlist ({len(stripped)}b)"
        # a big body we simply do not recognise - demote it, never delete it
        return UNKNOWN, f"unrecognised body ({len(stripped)}b)"
    if status in _DEAD_STATUS:
        return DEAD, f"HTTP {status}"
    return UNKNOWN, f"HTTP {status}" if status else "network error"


def _local_mirror(url: str, local_root: Path | None) -> Path | None:
    """The on-disk copy of a url that points at our own mirrored playlists."""
    if local_root is None or f"/{MIRROR_DIR}/" not in url:
        return None
    name = os.path.basename(urlparse(url).path)
    if not name:
        return None
    path = Path(local_root) / name
    return path if path.is_file() else None


@dataclass
class ProbeReport:
    verdicts: dict[str, str] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        counts = {ALIVE: 0, DEAD: 0, UNKNOWN: 0}
        for verdict in self.verdicts.values():
            counts[verdict] = counts.get(verdict, 0) + 1
        return counts

    def verdict_for(self, item: dict[str, Any]) -> str:
        return self.verdicts.get(str(item.get("url") or ""), UNKNOWN)


def _probe_one(
    item: dict[str, Any],
    client: Any,
    local_root: Path | None,
    *,
    connect_timeout: float,
    read_timeout: float,
    max_bytes: int,
) -> tuple[str, str, str]:
    url = str(item.get("url") or "")
    mirrored = _local_mirror(url, local_root)
    if mirrored is not None:
        # we host this file ourselves, so its bytes on disk *are* the answer
        try:
            text = mirrored.read_bytes().decode("utf-8", "replace")
        except OSError as exc:
            return url, UNKNOWN, f"unreadable mirror: {exc}"
        verdict, detail = _classify(text, status=200, ok=True)
        return url, verdict, f"{detail} (local mirror)"
    try:
        result = client.get(
            url,
            timeout=(connect_timeout, read_timeout),
            max_bytes=max_bytes,
            max_retries=0,
        )
    except Exception as exc:  # noqa: BLE001 - a probe must never break a build
        return url, UNKNOWN, type(exc).__name__
    status = result.status
    ok = bool(status and 200 <= status < 300)
    verdict, detail = _classify(result.text or "", status=status, ok=ok)
    return url, verdict, detail


def probe_playlists(
    lives: Iterable[dict[str, Any]],
    *,
    client: Any,
    local_root: Path | None = None,
    workers: int = 16,
    connect_timeout: float = 5.0,
    read_timeout: float = 8.0,
    max_bytes: int = 1_048_576,
) -> ProbeReport:
    """Fetch every playlist once and say whether it is still there."""
    items = [item for item in lives if isinstance(item, dict)]
    report = ProbeReport()
    if not items:
        return report
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        results = pool.map(
            lambda item: _probe_one(
                item,
                client,
                local_root,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                max_bytes=max_bytes,
            ),
            items,
        )
        for url, verdict, detail in results:
            report.verdicts[url] = verdict
            report.details[url] = detail
    counts = report.counts()
    LOGGER.info("live playlists probed", extra={
        "stage": "build", "check": "live_probe",
        "alive": counts[ALIVE], "unknown": counts[UNKNOWN], "dead": counts[DEAD],
    })
    return report


def rank_live_entry(
    payload: dict[str, Any],
    report: ProbeReport,
    *,
    drop: str = "dead",
    max_dead_ratio: float = 0.6,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Put live playlists first, then unknown ones, then the dead.

    ``drop`` selects what leaves the entry: ``dead`` (default) drops only what
    is provably gone, ``unhealthy`` also drops the unreachable, ``none`` keeps
    everything and merely reorders.  A runaway result - more than
    ``max_dead_ratio`` of the list dead, which is what a runner-side network
    outage looks like - falls back to reordering only, so a bad probe day can
    never wipe a good entry.
    """
    lives = [item for item in (payload.get("lives") or []) if isinstance(item, dict)]
    order = {ALIVE: 0, UNKNOWN: 1, DEAD: 2}
    ranked = sorted(lives, key=lambda item: order.get(report.verdict_for(item), 1))

    wanted = str(drop or "dead").lower()
    thresholds = {"none": (), "dead": (DEAD,), "unhealthy": (DEAD, UNKNOWN)}
    dropped_verdicts = thresholds.get(wanted, (DEAD,))
    counts = report.counts()
    total = len(lives)
    dead_ratio = counts[DEAD] / total if total else 0.0
    valve = dropped_verdicts and dead_ratio > float(max_dead_ratio)
    if valve:
        LOGGER.warning("live probe looked like an outage - reordering only", extra={
            "stage": "build", "check": "live_probe",
            "dead_ratio": round(dead_ratio, 3), "total": total,
        })
        dropped_verdicts = ()
    kept = [
        item for item in ranked
        if report.verdict_for(item) not in dropped_verdicts
    ]
    stats = {
        "total": total,
        "alive": counts[ALIVE],
        "unknown": counts[UNKNOWN],
        "dead": counts[DEAD],
        "dropped": total - len(kept),
        "drop": wanted if dropped_verdicts else "none",
    }
    out = dict(payload)
    out["lives"] = kept
    return out, stats
