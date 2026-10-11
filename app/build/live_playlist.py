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

Probing note: a stream only counts as playable when it answers like a player
would ask - fetch the manifest, then fetch the first media segment.  A URL
that merely opens a socket (a 200 with an HTML error page, an HLS manifest
whose segments 404) fails the moment the user taps it, so it is dropped.
Only a mainland connection can judge a mainland stream, so the verdicts that
ship are the ones measured locally (``data/live_probe.json``).
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urljoin

from ..logging_setup import get_logger
from ..utils.timeutil import to_iso

LOGGER = get_logger("build.live_playlist")

ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"
CACHE_FILE = "live_probe.json"

# the order the client shows the groups in
GROUP_ORDER = ("央视", "卫视", "地方", "港澳台", "国外", "其他")
_GROUP_RANK = {name: index for index, name in enumerate(GROUP_ORDER)}

_UA = "okhttp/3.15"  # what the apps actually send
_ATTR = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
_BRACKETED = re.compile(r"[（(][^）)]*[）)]")
# 403/451 are geoblocks, 523 is Cloudflare's "origin unreachable": from the
# mainland connection the client uses, all of them fail on tap.
_DEAD_STATUS = frozenset({403, 404, 410, 451, 523})
_SEGMENT_DEAD = frozenset({404, 410})
_PLAYABLE = ("http://", "https://")
# quality markers that differ between upstreams but mean the same channel
_QUALITY = ("1080P", "720P", "576I", "480P", "4K", "HD", "SD", "高清", "超清", "蓝光", "标清", "流畅")

# 港澳（含澳门）台名。上游把这些台塞进「央视频道」「其他」是常事，靠分组标签
# 归不了档，名字里带这些词的一律算港澳台 - 用户点开的大头就是这几个大台。
_HK_MO = (
    "港澳", "香港", "澳门", "澳門", "港视", "港視", "hkstv", "hket", "hongkong",
    "tvb", "翡翠", "明珠", "无线", "無綫", "無线", "viutv", "viu tv", "hoy tv", "hoy",
    "rthk", "港台电视", "港台電視", "凤凰", "鳳凰", "莲花卫视", "蓮花衛視", "澳视", "澳視",
)

# 真正的外国台：一家一家的品牌，而不是「沾了外国词」。
_FOREIGN_BRANDS = (
    "hbo", "cinemax", "star movies", "starmovies", "axn", "discovery", "动物星球",
    "animal planet", "national geographic", "国家地理", "nat geo", "bbc", "cnn", "cnbc",
    "bloomberg", "fox", "abc news", "nbc", "cbs", "espn", "sky", "mtv", "cartoon network",
    "nickelodeon", "disney", "history", "tlc", "eurosport", "dazn", "nhk", "wowow",
    "tv asahi", "fuji", "tv tokyo", "kbs", "mbc", "sbs", "tvn", "jtbc", "arirang",
    "france 24", "dw ", "euronews", "al jazeera", "rai", "zdf", "ard", "rtl",
    "abc australia", "cbc", "anime",
)

# 国家/地区词，名字里带着它基本就是外国台
_FOREIGN_PLACES = (
    "日本", "日テレ", "韩国", "韩语", "韩国语", "朝鲜", "美国", "英国", "法国", "德国",
    "意大利", "西班牙", "葡萄牙", "荷兰", "瑞士", "瑞典", "挪威", "丹麦", "俄罗斯",
    "乌克兰", "土耳其", "印度", "泰国", "越南", "新加坡", "马来西亚", "印尼", "菲律宾",
    "加拿大", "澳大利亚", "新西兰", "巴西", "阿根廷", "墨西哥", "南非", "埃及", "阿拉伯",
    "以色列", "伊朗", "巴基斯坦", "蒙古", "japan", "korea", "usa", "united states",
    "united kingdom", "europe", "italia", "france", "germany", "deutsch", "espana",
    "españa", "russia", "india", "thai", "vietnam", "arabic", "africa", "euro",
)

# 上游用来装外国台的分组标签
_FOREIGN_LABEL = ("国外", "海外", "国际", "international", "foreign", "overseas")

# 「沾了外国词」的全集：名字或分组标签命中它就算外国台 —— 但大陆台名除外，
# 见 canonical_group 里的 _is_mainland。
_FOREIGN = _FOREIGN_BRANDS + _FOREIGN_PLACES + _FOREIGN_LABEL

# 大陆台的招牌词。上游给这些台起名常带「国际」（广西国际、浙江国际、
# 中国国际教育1），不加这道闸它们全被划进「国外」，用户点开一片大陆台。
_MAINLAND = (
    "cctv", "央视", "中国", "中華", "中华", "china", "chinese", "cgtn",
    "浙江", "东方", "东方卫视", "湖南", "北京", "上海", "广东", "廣東", "江苏", "深圳",
    "广西", "河南", "山东", "湖北", "安徽", "福建", "四川", "天津", "重庆", "陕西",
    "辽宁", "吉林", "黑龙江", "云南", "贵州", "江西", "山西", "河北", "内蒙古", "新疆",
    "西藏", "甘肃", "青海", "宁夏", "海南", "厦门", "大连", "青岛", "苏州", "无锡",
    "芒果", "东南", "海峡", "延边", "兵团", "三沙", "中教", "教育台", "zhejiang", "hunan",
    "guangdong", "jiangsu", "shanghai", "beijing", "shenzhen", "guangxi", "henan",
    "shandong", "hubei", "anhui", "fujian", "sichuan", "tianjin",
)


def _is_mainland(name: str) -> bool:
    """名字读起来像大陆台吗（大陆台名里的「国际」不算外国台）。"""
    return any(word in name for word in _MAINLAND)


@dataclass(frozen=True)
class Channel:
    name: str
    url: str
    group: str = "其他"
    logo: str = ""
    # 1 = 随仓库一起发、人工实测过的线路（``fetch: local``）；见 _cap。
    priority: int = 0


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _clean(text: Any) -> str:
    return str(text or "").strip().strip('"').strip()


def canonical_group(name: str, group: str = "") -> str:
    """Fold the many group labels in the wild into the five we publish.

    The channel's own name decides first: upstreams file 凤凰卫视 under
    「央视频道」 often enough that the label alone cannot be trusted.

    Order matters.  央视 and 卫视 are checked before 国外 because the
    mainland channels are the ones with stray foreign-looking words in
    their names (``CCTV-4 国际频道``, ``东方卫视``); a Japanese or Korean
    channel carries a country word and no 卫视 at all.
    """
    name_l = str(name or "").lower()
    group_l = str(group or "").lower()
    mainland = _is_mainland(name_l)

    def foreign(haystack: str) -> bool:
        # 大陆台名里带「国际」的一律不算外国台：广西国际 / 浙江国际 / 中国国际教育1
        return not mainland and any(word in haystack for word in _FOREIGN)

    for haystack in (name_l, group_l):
        if any(word in haystack for word in _HK_MO) or any(
            word in haystack for word in ("台湾", "台灣", "taiwan")
        ):
            return "港澳台"
        if "央视" in haystack or "cctv" in haystack:
            return "央视"
        if "卫视" in haystack or "satellite" in haystack:
            return "卫视"
        if foreign(haystack):
            return "国外"
        if any(word in haystack for word in ("地方", "省内", "城市", "本地")):
            return "地方"
    if mainland:
        # 大陆台名带「国际」的（广西国际、浙江国际）该回地方组，不该占着国外
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
    require_alive_groups: Iterable[str] | None = None,
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
    kept = _drop_without_alive(kept, require_alive_groups or (), verdicts)
    kept = _cap(kept, max_per_name=max_per_name, max_per_group=group_caps, verdicts=verdicts)
    if max_channels:
        kept = kept[:max_channels]
    return kept


def _drop_without_alive(
    channels: list[Channel],
    groups: Iterable[str],
    verdicts: Mapping[str, str],
) -> list[Channel]:
    """Whole groups where a channel is only worth showing if one source plays.

    Upstreams carry dozens of dead 港澳台 lines.  Showing 东森新闻 with three
    sources that all fail is worse than not showing it: the user taps, gets
    「播放失败」, and concludes the whole list is broken.

    The test is per exact name, not per dedupe key: 「凤凰中文高清」 is its own
    entry in the client, so it does not get to ride on 「凤凰中文」 having a
    working line.
    """
    wanted = {str(group) for group in groups}
    if not wanted:
        return channels
    playable = {
        channel.name
        for channel in channels
        if channel.group in wanted and verdicts.get(channel.url) == ALIVE
    }
    return [
        channel for channel in channels
        if channel.group not in wanted or channel.name in playable
    ]


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

    Channels we shipped ourselves (``priority``) ignore both caps.  A crowded
    group like 国外 runs to a thousand lines and the cap sits at 600: without
    this, a curated line that happens to sort late is cut before it is ever
    probed - and stays cut on every later build, since it never earns a
    verdict that would move it up.
    """
    per_name: dict[str, int] = {}
    per_group: dict[str, int] = {}
    kept: list[Channel] = []
    for channel in channels:
        key = name_key(channel.name)
        cap = max_per_group.get(channel.group, 0)
        if not channel.priority:
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
            -channel.priority,
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


_SESSION: Any = None
_STREAM_TYPES = ("video", "audio", "octet-stream", "mp2t", "mpegurl")


def probe_session() -> Any:
    """One pooled session for the whole scan; a fresh socket per URL is slow."""
    global _SESSION
    if _SESSION is None:
        import requests
        from requests.adapters import HTTPAdapter

        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _SESSION = session
    return _SESSION


def _probe_headers(*, ranged: bool = False) -> dict[str, str]:
    headers = {"User-Agent": _UA, "Accept": "*/*", "Connection": "close"}
    if ranged:
        headers["Range"] = "bytes=0-4095"
    return headers


def _read_head(response: Any, limit: int = 4096) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(1024):
        chunks.append(chunk)
        total += len(chunk)
        if total >= limit:
            break
    return b"".join(chunks)


def _looks_like_html(body: bytes) -> bool:
    head = body[:512].lstrip().lower()
    return head.startswith(b"<!doctype") or b"<html" in head or b"<head" in head


def _hls_verdict(session: Any, url: str, text: str, timeout: tuple[float, float]) -> str:
    """A manifest is only alive when its first segment answers as well."""
    try:
        for _ in range(2):  # master -> variant -> media
            if "#EXT-X-STREAM-INF" not in text:
                break
            target = next((line.strip() for line in text.splitlines()
                           if line.strip() and not line.strip().startswith("#")), "")
            if not target:
                return UNKNOWN
            url = urljoin(url, target)
            variant = session.get(url, headers=_probe_headers(), timeout=timeout,
                                  stream=True, allow_redirects=True)
            text = _read_head(variant, 200_000).decode("utf-8", "ignore")
            variant.close()
        segment = next((line.strip() for line in text.splitlines()
                        if line.strip() and not line.strip().startswith("#")), "")
        if not segment:
            return UNKNOWN
        response = session.get(urljoin(url, segment), headers=_probe_headers(ranged=True),
                               timeout=timeout, stream=True, allow_redirects=True)
        status = response.status_code
        body = _read_head(response, 1024)
        response.close()
    except Exception:  # noqa: BLE001 - a probe must never break a build
        return UNKNOWN
    if status in _SEGMENT_DEAD or not body:
        return DEAD
    return ALIVE if 200 <= status < 400 else UNKNOWN


def _probe_one(url: str, *, connect_timeout: float, read_timeout: float) -> tuple[str, str]:
    """Ask the stream the way a player does, and judge on what comes back."""
    session = probe_session()
    timeout = (connect_timeout, read_timeout)
    try:
        response = session.get(url, headers=_probe_headers(ranged=True),
                               timeout=timeout, stream=True, allow_redirects=True)
    except Exception:  # noqa: BLE001 - a probe must never break a build
        return url, UNKNOWN
    status = response.status_code
    content_type = (response.headers.get("Content-Type") or "").lower()
    final_url = response.url
    try:
        body = _read_head(response)
    except Exception:  # noqa: BLE001
        body = b""
    response.close()

    if status in _DEAD_STATUS:
        return url, DEAD
    if _looks_like_html(body):
        return url, DEAD  # a 200 that is really an error page
    if b"#EXTM3U" in body[:2048]:
        try:
            manifest = session.get(final_url, headers=_probe_headers(), timeout=timeout,
                                   stream=True, allow_redirects=True)
            text = _read_head(manifest, 200_000).decode("utf-8", "ignore")
            manifest.close()
        except Exception:  # noqa: BLE001
            return url, UNKNOWN
        return url, _hls_verdict(session, final_url, text, timeout)
    if body[:3] == b"FLV":
        return url, ALIVE
    if 200 <= status < 400 and body and any(kind in content_type for kind in _STREAM_TYPES):
        return url, ALIVE
    return url, UNKNOWN


def probe_channels(
    channels: Iterable[Channel],
    *,
    workers: int = 24,
    connect_timeout: float = 5.0,
    read_timeout: float = 12.0,
) -> dict[str, str]:
    """Ask every URL whether it plays, then report the tally.

    Keep ``workers`` modest: these are hand-run mainland servers, and a burst
    of 64 parallel sockets gets throttled into the very "unknown" verdicts the
    scan is trying to avoid.
    """
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
    """Every upstream list, in order.  A broken one is skipped, never fatal.

    ``fetch: local`` reads a file that ships with the repo instead of a URL.
    Those are the channels we hand-verified from a mainland connection and
    that no public list carries reliably (the 翡翠台/明珠台 lines, for
    instance): a curated file beats hoping an upstream keeps them.
    """
    channels: list[Channel] = []
    failed: list[str] = []
    for source in sources:
        url = str(source.get("url") or "")
        if not url:
            continue
        mode = str(source.get("fetch") or "direct")
        name = str(source.get("name") or url)
        try:
            text = Path(url).read_text(encoding="utf-8") if mode == "local" else fetch(url, mode)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}")
            continue
        parsed = parse_playlist(text)
        if not parsed:
            failed.append(f"{name}: not a playlist")
            continue
        if mode == "local":
            parsed = [replace(channel, priority=1) for channel in parsed]
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
        require_alive_groups=out_cfg.get("require_alive_groups") or (),
    )
    if merged:
        write_playlists(
            dist_dir,
            merged,
            m3u_name=str(out_cfg.get("file") or "live.m3u"),
            txt_name=str(out_cfg.get("txt_file") or "live.txt"),
        )
    return merged, failed
