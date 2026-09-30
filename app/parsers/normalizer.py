"""Source normalisation helpers.

Turns loosely-shaped site / spider / config objects into something the checks
can work with, and knows how to spell the common 苹果CMS style search & detail
requests without guessing at random blog formats.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote, urlsplit, urlunsplit

SPIDER_TYPES = {
    0: "xml",
    1: "json",
    3: "cms",
    4: "cms",       # 苹果CMS v10 family
    7: "cms",
    8: "cms",
}

PLAY_SCHEME_ALLOWLIST = ("http", "https")
PLAY_SCHEME_DENYLIST = (
    "thunder", "magnet", "ed2k", "ftp", "rtmp", "rtsp", "ppvod", "qiyi", "youku",
    "qq", "letv", "mgtv", "bilibili", "javascript", "data",
)


@dataclass
class NormalizedSite:
    key: str
    name: str
    api: str
    type: str
    type_code: int | None
    searchable: bool
    quick_search: bool
    filterable: bool
    ext: str
    raw: dict[str, Any]

    @property
    def usable(self) -> bool:
        return bool(self.api) and urlsplit(self.api).scheme in ("http", "https")


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def normalize_site(raw: dict[str, Any]) -> NormalizedSite | None:
    if not isinstance(raw, dict):
        return None
    api = str(raw.get("api") or raw.get("ext") or "").strip()
    if api and "://" not in api:
        api = "https://" + api
    api = api.rstrip()
    name = str(raw.get("name") or raw.get("title") or "").strip()
    key = str(raw.get("key") or name or api).strip()
    type_code = raw.get("type")
    try:
        type_code = int(type_code) if type_code is not None else None
    except (TypeError, ValueError):
        type_code = None
    return NormalizedSite(
        key=key,
        name=name or key,
        api=api,
        type=SPIDER_TYPES.get(type_code or -1, "unknown"),
        type_code=type_code,
        searchable=_as_bool(raw.get("searchable"), True),
        quick_search=_as_bool(raw.get("quickSearch"), True),
        filterable=_as_bool(raw.get("filterable"), True),
        ext=str(raw.get("ext") or ""),
        raw=raw,
    )


def normalize_sites(items: Iterable[dict[str, Any]]) -> list[NormalizedSite]:
    out: list[NormalizedSite] = []
    for item in items or ():
        site = normalize_site(item)
        if site and site.usable:
            out.append(site)
    return out


def _join(base: str, query: str) -> str:
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{query}"


def search_url_candidates(api: str, keyword: str) -> list[str]:
    """Ordered list of plausible search endpoints for a 苹果CMS-flavoured API.

    We try the plain ``?wd=`` form first (most common), then the explicit
    ``ac=videolist`` form.  The caller stops at the first one that returns a
    usable JSON body - no guessing beyond these two well-known spellings.
    """
    if not api:
        return []
    encoded = quote(str(keyword), safe="")
    candidates: list[str] = []
    if "ac=" in api:
        candidates.append(_join(api, f"wd={encoded}"))
    else:
        candidates.append(_join(api, f"wd={encoded}"))
        candidates.append(_join(api, f"ac=videolist&wd={encoded}"))
    seen: dict[str, None] = {}
    for url in candidates:
        seen.setdefault(url, None)
    return list(seen)


def detail_url_candidates(api: str, video_id: str) -> list[str]:
    if not api or not video_id:
        return []
    encoded = quote(str(video_id), safe="")
    candidates: list[str] = []
    if "ac=" in api:
        candidates.append(_join(api, f"ids={encoded}"))
    else:
        candidates.append(_join(api, f"ac=detail&ids={encoded}"))
        candidates.append(_join(api, f"ac=videolist&ids={encoded}"))
    seen: dict[str, None] = {}
    for url in candidates:
        seen.setdefault(url, None)
    return list(seen)


def extract_video_ids(payload: Any) -> list[str]:
    """Pull vod ids out of a 苹果CMS search response."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("list")
    if not isinstance(items, list):
        return []
    ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("vod_id") or item.get("id") or item.get("vid")
        if value is None:
            continue
        ids.append(str(value))
    return ids


def count_results(payload: Any) -> int:
    if isinstance(payload, dict):
        items = payload.get("list")
        if isinstance(items, list):
            return len(items)
        total = payload.get("total") or payload.get("count")
        try:
            return int(total)
        except (TypeError, ValueError):
            return 0
    if isinstance(payload, list):
        return len(payload)
    return 0


def has_playlist(payload: Any) -> bool:
    """L4: does the detail response carry a non-empty playlist?"""
    if not isinstance(payload, dict):
        return False
    items = payload.get("list")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        play_url = item.get("vod_play_url")
        if isinstance(play_url, str) and play_url.strip():
            return True
        if isinstance(item.get("vod_play_list"), list) and item["vod_play_list"]:
            return True
    return False


def extract_play_urls(payload: Any, limit: int = 5) -> list[str]:
    """Return up to ``limit`` directly probeable media URLs (spec §8 L5).

    Only http/https URLs are returned - custom app schemes (``ppvod://``,
    ``magnet:`` ...) are skipped rather than "resolved", because probing them
    would mean bypassing the client's own access control.
    """
    urls: list[str] = []
    if not isinstance(payload, dict):
        return urls
    items = payload.get("list")
    if not isinstance(items, list):
        return urls
    for item in items:
        if not isinstance(item, dict):
            continue
        blocks = []
        play_url = item.get("vod_play_url")
        if isinstance(play_url, str):
            blocks.append(play_url)
        play_list = item.get("vod_play_list")
        if isinstance(play_list, list):
            for entry in play_list:
                if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                    blocks.append(entry["url"])
        for block in blocks:
            # vod_play_url uses `#` between episodes and `$` between label and url;
            # multiple play-from groups are joined with `$$$`.
            for group in block.split("$$$"):
                for episode in group.split("#"):
                    _, _, url = episode.partition("$")
                    candidate = (url or episode).strip()
                    if not candidate:
                        continue
                    scheme = urlsplit(candidate).scheme.lower()
                    if scheme in PLAY_SCHEME_ALLOWLIST:
                        urls.append(candidate)
                    elif scheme in PLAY_SCHEME_DENYLIST or scheme:
                        continue
                    if len(urls) >= limit:
                        return urls
    return urls


def strip_fragment(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
