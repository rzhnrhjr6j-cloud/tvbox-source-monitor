"""One merged live playlist (``dist/live.m3u``).

``tvbox-live.json`` is a *list of playlists*: the client has to fetch and
parse each of them on its own, and one that is gone costs the user a whole
line of 「解析失败」.  The 直播源 field, however, also accepts a plain
playlist - which is what every working source in the wild actually ships.

This module flattens the lot into that shape:

    #EXTM3U
    #EXTINF:-1 tvg-name="CCTV1" group-title="央视",CCTV1
    http://...

So one URL gives the client one list.  A channel that is gone only loses its
own line, and a channel carried by several upstreams keeps working sources
first (the client shows them as 源1/源2/源3).

Probing note: only a mainland connection can judge a mainland stream.  A
GitHub runner that cannot reach one must never be allowed to delete it, so
only 404/410 counts as dead, and the verdicts that ship are the ones measured
locally (``data/live_probe.json``).
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..logging_setup import get_logger
from ..utils.timeutil import to_iso

LOGGER = get_logger("build.live_playlist")

ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"
CACHE_FILE = "live_probe.json"

# the order the client shows the groups in
GROUP_ORDER = ("央视", "卫视", "地方", "港澳台", "其他")
_GROUP_RANK = {name: index for index, name in enumerate(GROUP_ORDER)}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_ATTR = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
_BRACKETED = re.compile(r"[（(][^）)]*[）)]")
_DEAD_STATUS = frozenset({404, 410})
_PLAYABLE = ("http://", "https://")
# quality markers that differ between upstreams but mean the same channel
_QUALITY = ("1080P", "720P", "576I", "480P", "4K", "HD", "SD", "高清", "超清", "蓝光", "标清", "流畅")


@dataclass(frozen=True)
class Channel:
    name: str
    url: str
    group: str = "其他"
    logo: str = ""


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _clean(text: Any) -> str:
    return str(text or "").strip().strip('"').strip()


def canonical_group(name: str, group: str = "") -> str:
    """Fold the many group labels in the wild into the five we publish.

    The channel's own name decides first: upstreams file 凤凰卫视 under
    「央视频道」 often enough that the label alone cannot be trusted.
    """
    for haystack in (str(name or "").lower(), str(group or "").lower()):
        if any(word in haystack for word in ("港澳", "香港", "澳门", "台湾", "hket", "taiwan", "hongkong")):
            return "港澳台"
        if "卫视" in haystack or "satellite" in haystack:
            return "卫视"
        if "央视" in haystack or "cctv" in haystack:
            return "央视"
        if any(word in haystack for word in ("地方", "省内", "城市", "本地")):
            return "地方"
    return "其他"


def name_key(name: str) -> str:
    """Dedupe key for a channel name.

    Upstreams spell one channel a dozen ways - ``CCTV-1``, ``CCTV1``,
    ``CCTV-1 (1080p)``, ``CCTV-1HD`` - and left alone the client shows the
    same channel ten times, which is also why ``max_per_name`` never bit.
    """
    text = _BRACKETED.sub("", str(name or "")).upper()
    for marker in _QUALITY:
        text = text.replace(marker, "")
    return re.sub(r"[\s\-_.]+", "", text)


def _playable(url: str) -> str | None:
    url = _clean(url).replace("&amp;", "&")
    url = url.split("$")[0].strip()  # per-url options ($name, $format) are not ours
    if not url.lower().startswith(_PLAYABLE):
        return None
    return url


def parse_m3u(text: str) -> list[Channel]:
    """``#EXTINF`` lists, with or without attributes."""
    channels: list[Channel] = []
    pending: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            attrs = dict(_ATTR.findall(line))
            tail = line.rsplit(",", 1)[-1]
            name = _clean(tail) or _clean(attrs.get("tvg-name")) or _clean(attrs.get("tvg-id"))
            pending = {"name": name, "logo": _clean(attrs.get("tvg-logo")),
                       "group": _clean(attrs.get("group-title"))}
            continue
        if line.startswith("#"):
            continue
        if pending is None:
            continue
        url = _playable(line)
        name = pending["name"]
        if url and name:
            channels.append(Channel(
                name=name,
                url=url,
                group=canonical_group(name, pending["group"]),
                logo=pending["logo"],
            ))
        pending = None
    return channels


def parse_txt(text: str) -> list[Channel]:
    """The flat ``频道名,http://...`` list; ``#genre#`` starts a new group."""
    channels: list[Channel] = []
    group = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().endswith("#genre#"):
            group = _clean(line.split(",", 1)[0])
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        name = parts[0]
        for candidate in parts[1:]:
            for piece in candidate.split("#"):
                url = _playable(piece)
                if url and name:
                    channels.append(Channel(
                        name=name,
                        url=url,
                        group=canonical_group(name, group),
                    ))
    return channels


def parse_playlist(text: str) -> list[Channel]:
    """Whatever the upstream shipped - m3u, txt, or an html error page."""
    stripped = (text or "").lstrip("\ufeff \t\r\n")
    if not stripped or stripped[:1] == "<":
        return []
    channels = parse_m3u(stripped)
    if channels:
        return channels
    return parse_txt(stripped)


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------


def merge_channels(
    groups: Iterable[Iterable[Channel]],
    *,
    verdicts: Mapping[str, str] | None = None,
    max_per_name: int = 3,
    max_channels: int = 0,
    max_per_group: Mapping[str, int] | None = None,
) -> list[Channel]:
    """One list, deduped, working sources first, groups in a stable order."""
    verdicts = verdicts or {}
    group_caps = {str(name): int(cap) for name, cap in (max_per_group or {}).items() if int(cap or 0) > 0}
    seen: set[tuple[str, str]] = set()
    kept: list[Channel] = []
    for group in groups:
        for channel in group:
            if verdicts.get(channel.url) == DEAD:
                continue  # provably gone (404/410): it would only fail on tap
            key = (name_key(channel.name), channel.url)
            if key in seen:
                continue
            seen.add(key)
            kept.append(channel)
    kept = sort_channels(kept, verdicts)
    kept = _cap(kept, max_per_name=max_per_name, max_per_group=group_caps, verdicts=verdicts)
    if max_channels:
        kept = kept[:max_channels]
    return kept


def _cap(
    channels: list[Channel],
    *,
    max_per_name: int,
    max_per_group: Mapping[str, int],
    verdicts: Mapping[str, str],
) -> list[Channel]:
    """Drop the weakest surplus: extra sources of one channel, bloated groups.

    The list arrives sorted (group, name, alive first), so a plain walk keeps
    the first ``max_per_name`` sources of every channel - the reachable ones -
    and the first ``cap`` channels of a group.
    """
    per_name: dict[str, int] = {}
    per_group: dict[str, int] = {}
    kept: list[Channel] = []
    for channel in channels:
        key = name_key(channel.name)
        cap = max_per_group.get(channel.group, 0)
        if max_per_name and per_name.get(key, 0) >= max_per_name:
            continue
        if cap and per_group.get(channel.group, 0) >= cap:
            continue
        per_name[key] = per_name.get(key, 0) + 1
        per_group[channel.group] = per_group.get(channel.group, 0) + 1
        kept.append(channel)
    return kept


def sort_channels(channels: Iterable[Channel], verdicts: Mapping[str, str] | None = None) -> list[Channel]:
    """Groups in :data:`GROUP_ORDER`, then by name, with alive sources first.

    Group contiguity matters: the client builds its group list in the order
    the channels appear, so a file that interleaves groups shows the same
    group twice.
    """
    verdicts = verdicts or {}

    def key(channel: Channel) -> tuple:
        alive = 0 if verdicts.get(channel.url) == ALIVE else 1
        return (
            _GROUP_RANK.get(channel.group, len(GROUP_ORDER)),
            channel.group,
            name_key(channel.name),
            alive,
            channel.url,
        )

    return sorted(channels, key=key)


def render_m3u(channels: Iterable[Channel], *, epg: str = "", ua: str = "") -> str:
    head = "#EXTM3U"
    if epg:
        head += f' x-tvg-url="{epg}"'
    lines = [head]
    for channel in channels:
        attrs = [f'tvg-name="{channel.name}"']
        if channel.logo:
            attrs.append(f'tvg-logo="{channel.logo}"')
        attrs.append(f'group-title="{channel.group}"')
        if ua:
            attrs.append(f'http-user-agent="{ua}"')
        lines.append(f"#EXTINF:-1 {' '.join(attrs)},{channel.name}")
        lines.append(channel.url)
    return "\n".join(lines) + "\n"


def render_txt(channels: Iterable[Channel]) -> str:
    """The flat list, for clients that prefer txt over m3u."""
    lines: list[str] = []
    current = None
    for channel in channels:
        if channel.group != current:
            current = channel.group
            lines.append(f"{current},#genre#")
        lines.append(f"{channel.name},{channel.url}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------


def load_verdicts(path: Path) -> dict[str, str]:
    """``{url: verdict}`` from the committed cache; a missing file is not an error."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    verdicts = payload.get("verdicts") if isinstance(payload, dict) else payload
    if not isinstance(verdicts, dict):
        return {}
    out: dict[str, str] = {}
    for url, value in verdicts.items():
        if isinstance(value, dict):
            value = value.get("verdict")
        if isinstance(value, str):
            out[str(url)] = value
    return out


def save_verdicts(path: Path, verdicts: Mapping[str, str]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": to_iso(),
        "verdicts": {url: {"verdict": verdict} for url, verdict in sorted(verdicts.items())},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _probe_one(url: str, *, connect_timeout: float, read_timeout: float) -> tuple[str, str]:
    import requests

    try:
        response = requests.get(
            url,
            headers={"User-Agent": _UA, "Range": "bytes=0-2047", "Accept": "*/*"},
            timeout=(connect_timeout, read_timeout),
            stream=True,
            allow_redirects=True,
        )
        status = response.status_code
        response.close()
    except Exception:  # noqa: BLE001 - a probe must never break a build
        return url, UNKNOWN
    if status in _DEAD_STATUS:
        return url, DEAD
    if 200 <= status < 400:
        return url, ALIVE
    return url, UNKNOWN


def probe_channels(
    channels: Iterable[Channel],
    *,
    workers: int = 64,
    connect_timeout: float = 4.0,
    read_timeout: float = 6.0,
) -> dict[str, str]:
    """One ranged fetch per URL: is this stream still answering?"""
    urls = sorted({channel.url for channel in channels})
    if not urls:
        return {}
    verdicts: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for url, verdict in pool.map(
            lambda url: _probe_one(url, connect_timeout=connect_timeout, read_timeout=read_timeout),
            urls,
        ):
            verdicts[url] = verdict
    counts = {name: sum(1 for value in verdicts.values() if value == name)
              for name in (ALIVE, UNKNOWN, DEAD)}
    LOGGER.info("live channels probed", extra={
        "stage": "build", "check": "live_channel_probe", "counts": counts,
    })
    return verdicts


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def fetch_channels(
    sources: Iterable[Mapping[str, Any]],
    fetch: Callable[[str, str], str],
) -> tuple[list[Channel], list[str]]:
    """Every upstream list, in order.  A broken one is skipped, never fatal."""
    channels: list[Channel] = []
    failed: list[str] = []
    for source in sources:
        url = str(source.get("url") or "")
        if not url:
            continue
        mode = str(source.get("fetch") or "direct")
        name = str(source.get("name") or url)
        try:
            text = fetch(url, mode)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}")
            continue
        parsed = parse_playlist(text)
        if not parsed:
            failed.append(f"{name}: not a playlist")
            continue
        channels.extend(parsed)
    return channels, failed


def write_playlists(
    dist_dir: Path,
    channels: list[Channel],
    *,
    m3u_name: str = "live.m3u",
    txt_name: str = "live.txt",
    epg: str = "",
) -> list[Path]:
    dist_dir = Path(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    payloads = [(m3u_name, render_m3u(channels, epg=epg)), (txt_name, render_txt(channels))]
    for name, text in payloads:
        if not name:
            continue
        path = dist_dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        written.append(path)
    return written


def merged_entry(count: int, base: str, *, name: str, filename: str = "live.m3u") -> dict[str, Any]:
    """The ``lives`` item that points the client at the merged file."""
    return {"name": name, "type": 1, "url": f"{base.rstrip('/')}/{filename}"}


def assemble(
    *,
    dist_dir: Path,
    out_cfg: Mapping[str, Any] | None = None,
    verdicts: Mapping[str, str] | None = None,
    sources: Iterable[Mapping[str, Any]] | None = None,
    fetch: Callable[[str, str], str] | None = None,
    channels: Iterable[Channel] | None = None,
) -> tuple[list[Channel], list[str]]:
    """Fetch every upstream list, merge them, write the two files.

    Shared by the nightly build and ``tools/build_live_playlist.py`` so both
    publish byte-identical shapes.  Returns the channels and the sources that
    could not be read; an empty list means nothing was written.  Pass
    ``channels`` instead of ``sources``/``fetch`` to skip the download when the
    caller already has them.
    """
    out_cfg = out_cfg or {}
    failed: list[str] = []
    if channels is None:
        channels, failed = fetch_channels(sources or [], fetch)
    merged = merge_channels(
        [channels],
        verdicts=verdicts,
        max_per_name=int(out_cfg.get("max_per_name", 3) or 3),
        max_channels=int(out_cfg.get("max_channels", 0) or 0),
        max_per_group=out_cfg.get("max_per_group") or {},
    )
    if merged:
        write_playlists(
            dist_dir,
            merged,
            m3u_name=str(out_cfg.get("file") or "live.m3u"),
            txt_name=str(out_cfg.get("txt_file") or "live.txt"),
        )
    return merged, failed
