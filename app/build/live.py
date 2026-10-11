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
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from ..logging_setup import get_logger
from ..utils.timeutil import to_iso

LOGGER = get_logger("build.live")

LIVE_FILE = "tvbox-live.json"
SOURCES_DIR = "sources"

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
