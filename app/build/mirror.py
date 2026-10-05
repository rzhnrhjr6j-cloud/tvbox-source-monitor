"""Mirror published source configs onto our own Pages host (spec §18, §37).

The problem this solves
-----------------------
Discovered configs are overwhelmingly hosted on ``raw.githubusercontent.com``.
Every probe in this project runs on a GitHub runner, so the runner always
reaches that host - while the client (影视仓 on a phone or TV box in mainland
China) cannot.  The system therefore published a tvbox.json whose every entry
was dead for the only reader that matters, and no amount of probing from the
runner could ever notice.

The fix
-------
Download each config while we are on the runner, keep the bytes under
``dist/sources/``, and publish a URL on ``<user>.github.io/<repo>`` - a host
whose reachability from the client's network has been verified.  Every URL the
client touches then lives on one host we control and can test.
"""

from __future__ import annotations

import json
import hashlib
import ipaddress
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

from ..logging_setup import get_logger
from ..models import Source

LOGGER = get_logger("build.mirror")

SOURCES_DIR = "sources"
JARS_DIR = "jars"

# How a reference ends inside the author's own text: the client's own digest,
# or the "$"-suffixed params TVBox appends.  Neither is part of the URL.
_REF_TAIL = re.compile(r"(\$.*|;md5;.*)$")
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,5}$")


def _extension(url: str) -> str:
    """Keep the author's own file suffix so nothing about the name changes."""
    suffix = os.path.splitext(urlsplit(url).path)[1]
    return suffix if _SAFE_SUFFIX.match(suffix) else ".dat"


# A crawler jar is a ZIP - or, for a couple of authors, a bare DEX.  A host
# that answers 200 with an HTML error page (a deleted repo behind a soft 404)
# looks alive to every status-based probe, and the client still reports
# "jar加载失败" the moment the source is opened.  Only the bytes can tell.
_JAR_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"dex\n")


def _looks_like_a_jar(blob: bytes) -> bool:
    """Report whether ``blob`` starts with a container a client can load."""
    return any(blob.startswith(magic) for magic in _JAR_MAGIC)

# A site whose crawler jar is gone cannot open in the client; 影视仓 reports it
# as "jar加载失败".  Only a hard 404/410 counts as gone - a timeout or a 5xx is
# as likely to be our probe as the jar, and hiding a working site is worse than
# showing a broken one, since the next nightly build gets another chance.
_DEAD_JAR_STATUS = frozenset({404, 410})

# A reference on a host that cannot be reached at all will not be reachable for
# its siblings either.  Status codes are deliberately absent: a 404 is one file
# that is gone, not a host that is.
_DEAD_HOST_ERRORS = frozenset({"DNS_ERROR", "CONNECT_ERROR", "TLS_ERROR", "SSRF_BLOCKED"})


def _is_address_literal(host: str) -> bool:
    """A bare address is not a host we can reason about.

    ``127.0.0.1:9978`` is the client's own file server and a LAN address is a
    box next to the user - a runner failing to reach either says nothing about
    the client, so neither may poison the host cache for its siblings.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True

# [607KB/s|1290ms|稳] 360资源  ->  360资源
_BRACKET_PREFIX = re.compile(r"^\s*[\[\(【（][^\]\)】）]{0,40}[\]\)】）]\s*")
# pictographs, dingbats, flags, variation selectors - cosmetic noise in names
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U0001f1e6-\U0001f1ff\U00002600-\U000027bf"
    "\U00002b00-\U00002bff\U00002190-\U000021ff\ufe0f\u200d]"
)
_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")


def _clean_name(raw: str) -> str:
    """Strip the decoration TVBox authors put in site names."""
    text = _BRACKET_PREFIX.sub("", str(raw))
    text = _EMOJI.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" -·|｜/\\")
    return text


def _site_count(config: Any) -> int:
    """How many rows the config offers.

    A single-warehouse config carries sites; a multi-warehouse one carries
    child configs in ``urls``; a few ship a bare array.  All three are what the
    client ends up listing, so all three count.
    """
    if isinstance(config, list):
        return len(config)
    if isinstance(config, dict):
        for key in ("sites", "urls"):
            rows = config.get(key)
            if isinstance(rows, list):
                return len(rows)
    return 0


def _pick_name(config: Any, url: str, source: Source) -> str:
    """A short, human, ideally-Chinese label for the 影视仓 list.

    A config is a bundle of sites, so the most honest label is the source from
    which it came.  We prefer a usable Chinese row name - and for a
    multi-warehouse config that means a child row, which is Chinese far more
    often than the GitHub path is.  Failing that we fall back to ``owner/repo``
    for GitHub configs, and to the host otherwise.
    """
    rows: list = []
    if isinstance(config, list):
        rows = config
    elif isinstance(config, dict):
        for key in ("sites", "urls"):
            value = config.get(key)
            if isinstance(value, list):
                rows.extend(value)
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate = _clean_name(row.get("name") or "")
        if 2 <= len(candidate) <= 14 and _HAS_CJK.search(candidate):
            return candidate
    if isinstance(config, dict):
        for key in ("name", "title"):
            candidate = _clean_name(config.get(key) or "")
            if 2 <= len(candidate) <= 24:
                return candidate

    match = re.match(r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/", url)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    match = re.match(r"^https?://([^/]+)", url)
    if match:
        return match.group(1)
    return (source.name or source.id[:8]).strip()


# A config does not only live at one URL.  In the wild it points at more
# configs (``urls[]``), live playlists (``lives[]``) and scripts - measured on a
# real run, 86 such references across 24 configs, all on
# raw.githubusercontent.com.  Mirroring only the outer file would still leave
# every one of those dead for the client, so the host is rewritten through an
# acceleration proxy instead (cheaper than pulling ~20 MB of playlists a day).
_RAW_HOST = re.compile(r"https://raw\.githubusercontent\.com/")

# TVBox authors ship the crawler jar next to the config and point at it
# relatively - "./spider.jar", "./jars/xm.jar;md5;...", sometimes disguised as
# .txt or .png.  Measured on the published set: 277 such references across 24
# sources.  We republish the config from a different directory, so the relative
# path stops landing on the jar and the client reports "jar加载失败".
#
# ``api`` is the same story when it names a script.  The csp pass leaves that
# key alone on purpose - most of the time it is a live JSON endpoint, and
# snapshotting an endpoint pins a live service - but "./lib/drpy2.min.js" is a
# file, and a relative path points the client at *our* directory.  Measured on
# the published set: 1011 such references across 31 sources, every one a 404.
_RELATIVE_REF = re.compile(
    r'("(?:[A-Za-z_][A-Za-z0-9_]*)"\s*:\s*")(\.{1,2}/[^"]*)"'
)


# A multi-warehouse child names its crawler, its csp scripts and its site jars
# the same way the parent does, and those references are just as relative.  We
# republish the child from our own host, so every one of them has to be resolved
# and re-served or the child is a list of rows that cannot open.
_CHILD_REF = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"(\s*:\s*")([^"]*)"')

# A drpy script is a module, and its ``import`` specifiers are relative to the
# script's own URL.  We republish the script under a name taken from its bytes,
# in another directory, so "./cheerio.min.js" landed on a file that does not
# exist.  Measured on the published set: 66 of 1082 scripts import a sibling
# that way, 190 references in total.  The siblings travel too, and the
# specifier becomes absolute so the answer cannot depend on which directory the
# client believes the script lives in.
_SCRIPT_REF = re.compile(
    r"""((?:\bfrom|\bimport|\brequire\s*\()\s*\(?\s*)(["'])(\.{1,2}/[^"']+)\2"""
)

# A host that answers 200 with an error page is the shape a deleted author
# repository leaves behind.  The client does not read HTML as a config; it
# reports 解析配置失败 and moves on.
_HTML_HEADS = (b"<!doctype", b"<html", b"<head", b"<!--", b"<meta", b"<body")


def _looks_like_html(blob: bytes) -> bool:
    if blob[:3] == b"\xef\xbb\xbf":
        blob = blob[3:]
    return blob.lstrip()[:64].lower().startswith(_HTML_HEADS)


# The other two shapes a host retries into when the path it used to serve is
# gone: a cover image, or a CDN's one-pixel placeholder.
_IMAGE_HEADS = (
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF", b"WEBP",
)

# Below this a body cannot be a config.  An empty one is 12 bytes of JSON
# (``{"sites":[]}``), and the two greetings an expired CDN answered with on the
# live set - "你好！" and "后会有期！！" - are 9 and 19 bytes.  The threshold
# sits just above them so a short live playlist still travels.
_MIN_CHILD_BYTES = 24


_PROXY_HOP = re.compile(r"https?://[^\s\"'<>/\\]+/(?=(?:https?://|raw\.githubusercontent\.com/))")


def _unwrap_proxy(url: str) -> str:
    """Drop one acceleration hop so a relative path resolves against the file.

    ``urljoin`` cannot be trusted with a hop in front of a URL: it collapses
    the second ``//``, so "./cheerio.min.js" against
    ``https://<proxy>/https://raw…/x.js`` resolves to
    ``https://<proxy>/https:/raw…/cheerio.min.js`` - a URL that was never well
    formed and that the proxy answers 404 for.
    """
    match = _PROXY_HOP.match(url)
    if not match:
        return url
    rest = url[match.end():]
    return rest if rest.startswith("http") else f"https://{rest}"


def _strip_json_noise(text: str) -> str:
    """Take out what a lenient parser skips and a strict one chokes on.

    Strings are copied through untouched, so a ``https://`` inside one is never
    mistaken for the start of a comment.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == '"':
            end = index + 1
            while end < length:
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == '"':
                    break
                end += 1
            out.append(text[index:end + 1])
            index = end + 1
            continue
        if char == "/" and text.startswith("//", index):
            stop = text.find("\n", index)
            index = length if stop < 0 else stop
            continue
        if char == "/" and text.startswith("/*", index):
            stop = text.find("*/", index + 2)
            index = length if stop < 0 else stop + 2
            continue
        out.append(char)
        index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _load_config(text: str) -> Any:
    """Parse a config the way the client parses one.

    Authors put a ``//`` header in front of the JSON, comment out a dead
    ``"spider"`` line and leave trailing commas behind; the client strips all
    three before it parses, so a strict parse here would refuse files that open
    perfectly well.  It also decides whether a multi-warehouse parent ever gets
    its children opened at all - the comment-headed ones published with their
    children still naming "./lib/drpy2.min.js".
    """
    return json.loads(_strip_json_noise(text.lstrip("\ufeff")))


def _looks_like_a_config(blob: bytes) -> bool:
    """Report whether ``blob`` is a shape the client can open as a config.

    A multi-warehouse child URL that has been taken down rarely answers 404 -
    measured on the live twelve multi-warehouse sources, 139 children answered
    200 with 5 HTML error pages, 8 two-character greetings, a cover image and
    three pictures.  The client reads none of them as a config and reports
    解析配置失败, so the child is dropped instead of republished.

    Everything else is deliberately left alone.  Authors ship configs the
    client decrypts itself - ``key**base64``, the ``$#246#$`` envelope, a ``//``
    comment header in front of the JSON - and dropping those would hide a
    source that opens perfectly well.
    """
    if _looks_like_html(blob):
        return False
    if any(blob.startswith(magic) for magic in _IMAGE_HEADS):
        return False
    return len(blob.lstrip()) >= _MIN_CHILD_BYTES


def rewrite_relative(text: str, origin: str) -> tuple[str, int]:
    """Resolve "./x.jar" references against the config's own origin URL.

    Only paths that stay inside the origin directory are resolved, so a config
    cannot point us at something above the directory it was published from.
    """
    if not origin:
        return text, 0
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        reference = match.group(2)
        if reference.startswith(".."):
            return match.group(0)
        path, sep, digest = reference.partition(";md5;")
        absolute = urljoin(origin, path)
        if not absolute.startswith("http"):
            return match.group(0)
        if sep:
            absolute = f"{absolute};md5;{digest}"
        count += 1
        return f'{match.group(1)}{absolute}"'

    return _RELATIVE_REF.sub(replace, text), count


# An already-proxied reference reads ``https://ghfast.top/https://raw.github…``,
# so what marks it is the immediate ``<scheme>://<host>/`` prefix.  Anything
# shorter would let a proxied neighbour earlier in the file shelter the next
# bare reference from being rewritten.
_PROXIED_PREFIX = re.compile(r"https?://[^\s\"'<>/\\]+/$")

# An author's own proxy hop arrives in two shapes: wrapping the whole URL
# (``https://<host>/https://raw.github…``) or standing in for the scheme
# (``https://<host>/raw.github…``).  Measured on the published set: 21 such
# references through 5 hosts, and the busiest of them (``daili.korice.eu.org``,
# 12 references) had gone dark - the client reported "jar加载失败" for each one.
# A hop we cannot vouch for is therefore replaced, not trusted.
_PROXIED_REF = re.compile(
    r"https?://(?P<host>[^\s\"'<>/\\]+)/(?:https?://)?(?=raw\.githubusercontent\.com/)"
)


def rewrite_inner(text: str, proxy: str) -> tuple[str, int]:
    """Point every raw.githubusercontent.com URL at ``proxy``.

    Returns the new text and how many references were rewritten.  A reference
    already sitting behind somebody else's proxy is re-pointed at ``proxy``
    rather than left alone; our own prefix is left untouched so a second run
    cannot wrap it twice.
    """
    if not proxy:
        return text, 0

    ours = urlsplit(proxy).netloc
    hits = 0

    def normalize(match: re.Match[str]) -> str:
        nonlocal hits
        # a prefix that ends in "://" already carries the inner scheme, so it
        # is in the shape we want - but only if it is our own proxy
        if match.group("host") == ours and match.group(0).endswith("://"):
            return match.group(0)
        hits += 1
        return f"{proxy}https://"

    text = _PROXIED_REF.sub(normalize, text)

    parts: list[str] = []
    cursor = 0
    bare = 0
    for match in _RAW_HOST.finditer(text):
        start = match.start()
        if _PROXIED_PREFIX.search(text[max(0, start - 80):start]):
            continue
        parts.append(text[cursor:start])
        parts.append(proxy)
        cursor = start
        bare += 1
    if bare:
        parts.append(text[cursor:])
        text = "".join(parts)
    return text, hits + bare


@dataclass
class MirrorEntry:
    source_id: str
    slug: str
    name: str
    url: str
    site_count: int
    content: bytes
    origin: str
    inner_rewrites: int = 0
    hosted: int = 0


@dataclass
class MirrorPlan:
    """What the build should publish, and what it must leave out."""

    enabled: bool = False
    entries: dict[str, MirrorEntry] = field(default_factory=dict)
    dropped: set[str] = field(default_factory=set)
    jars: dict[str, bytes] = field(default_factory=dict)

    def url_for(self, source_id: str) -> str | None:
        entry = self.entries.get(source_id)
        return entry.url if entry else None

    def name_for(self, source_id: str) -> str | None:
        entry = self.entries.get(source_id)
        return entry.name if entry else None


class ConfigMirror:
    """Fetches configs and re-serves them from our own Pages host."""

    def __init__(self, cfg, dist_dir: Path, http):
        self.cfg = cfg
        self.dist_dir = Path(dist_dir)
        self.http = http
        self.settings = dict(cfg.section("output").get("mirror") or {})

    # -- configuration -----------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False)) and bool(self.public_base)

    @property
    def public_base(self) -> str:
        configured = str(self.settings.get("public_base") or "").strip()
        if configured:
            return configured.rstrip("/")
        repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if "/" not in repository:
            return ""
        owner, repo = repository.split("/", 1)
        # Measured from a mainland connection: github.io serves a 460KB crawler
        # jar at ~50KB/s - 9.5s on average and 34s at worst - while the same
        # bytes through a GitHub mirror come back in 1.5s.  The client opens a
        # source by downloading its spider, so the slow host is what "解析配置失败"
        # actually was.  Every entry, child config and jar therefore goes
        # through the mirror; <owner>.github.io stays as the fallback entry.
        mirror = str(self.settings.get("git_mirror") or "").strip()
        if mirror:
            ref = str(self.settings.get("git_ref") or "main").strip() or "main"
            return (f"{mirror.rstrip('/')}/https://raw.githubusercontent.com/"
                    f"{owner}/{repo}/{ref}/dist")
        return f"https://{owner}.github.io/{repo}"

    @property
    def on_failure(self) -> str:
        return str(self.settings.get("on_failure", "drop")).lower()

    @property
    def alt_base(self) -> str:
        """A second host to publish the same list under, or "" for none.

        github.io answers a GitHub runner and answers this machine, but carrier
        DNS in mainland China resolves it to hijacked pages often enough that a
        client never sees our JSON and reports "解析配置失败" instead.  jsDelivr
        fronts the same repository through domestic CDN nodes, so the published
        list is also served from there with every url swapped over.
        """
        configured = str(self.settings.get("alt_base", "auto")).strip()
        if configured.lower() in ("", "off", "none", "false"):
            return ""
        if configured.lower() != "auto":
            return configured.rstrip("/")
        repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if "/" not in repository:
            return ""
        owner, repo = repository.split("/", 1)
        # fastly answered in 0.40s from a mainland connection where the bare
        # cdn.jsdelivr.net host averaged 7.07s with 24s spikes
        return f"https://fastly.jsdelivr.net/gh/{owner}/{repo}@main/dist"

    @property
    def inner_proxy(self) -> str:
        if not bool(self.settings.get("rewrite_inner", True)):
            return ""
        return str(self.settings.get("inner_proxy") or "").strip()

    @property
    def max_bytes(self) -> int:
        return int(self.settings.get("max_bytes", 2 * 1024 * 1024))

    @property
    def prune_dead_jars(self) -> bool:
        """Drop sites whose crawler jar is definitively gone (see _DEAD_JAR_STATUS)."""
        return bool(self.settings.get("prune_dead_jars", True))

    @property
    def host_jars(self) -> bool:
        """Re-serve the proxied crawler jars from our own host (see _localise_jars)."""
        return bool(self.settings.get("host_jars", True))

    @property
    def jar_max_bytes(self) -> int:
        return int(self.settings.get("jar_max_bytes", 12 * 1024 * 1024))

    @property
    def jar_total_bytes(self) -> int:
        return int(self.settings.get("jar_total_bytes", 256 * 1024 * 1024))

    @property
    def host_seconds(self) -> float:
        """Wall-clock ceiling for the whole hosting pass.

        A reference that hangs costs a full timeout, and a config can name a
        few hundred of them.  Once the ceiling is reached the rest keep the
        author's URL: a partially hosted list still opens, a nightly build that
        runs into the job limit publishes nothing.
        """
        return float(self.settings.get("host_seconds", 900))

    @property
    def jar_timeout(self) -> float:
        return float(self.settings.get("jar_timeout", 15))

    @property
    def max_jar_probes(self) -> int:
        return int(self.settings.get("max_jar_probes", 300))

    @property
    def max_total_bytes(self) -> int:
        return int(self.settings.get("max_total_bytes", 32 * 1024 * 1024))

    # -- jar liveness ------------------------------------------------------
    def _jar_alive(self, reference: str, cache: dict[str, bool]) -> bool:
        """Is the crawler jar behind ``reference`` still downloadable?

        A ``;md5;`` suffix and anything that is not plain HTTP (``clan://``, a
        bare ``csp_`` key, the client's own 127.0.0.1:9978 file server) is not
        ours to test, so those are reported alive and left untouched.
        """
        url = reference.split(";md5;")[0].strip()
        if not url.startswith(("http://", "https://")):
            return True
        if url in cache:
            return cache[url]
        if len(cache) >= self.max_jar_probes:
            return True
        client = self.http
        result = client.probe_first_bytes(
            url, 1, timeout=(client.connect_timeout, self.jar_timeout)
        )
        alive = result.status not in _DEAD_JAR_STATUS
        cache[url] = alive
        if not alive:
            LOGGER.info("jar gone", extra={
                "stage": "build", "check": "mirror", "jar": url,
                "status": result.status})
        return alive

    def _prune_dead_jars(
        self, text: str, cache: dict[str, bool]
    ) -> tuple[str, int, int | None]:
        """Remove sites the client could not open.

        Returns ``(text, dropped, remaining)``.  ``remaining`` is ``None`` when
        the config is not a shape we can prune, so the caller falls back to its
        own count instead of reporting zero.
        """
        if not self.prune_dead_jars:
            return text, 0, None
        try:
            config = _load_config(text)
        except ValueError:
            return text, 0, None
        if not isinstance(config, dict) or not isinstance(config.get("sites"), list):
            return text, 0, None

        # a dead top-level spider takes every csp_* site down with it, even
        # though those sites carry no jar of their own
        spider = config.get("spider")
        spider_dead = isinstance(spider, str) and not self._jar_alive(spider, cache)

        kept: list[Any] = []
        dropped = 0
        for site in config["sites"]:
            if not isinstance(site, dict):
                kept.append(site)
                continue
            api = str(site.get("api") or "").strip()
            jar = site.get("jar")
            if isinstance(jar, str):
                # a jar the site names itself is authoritative - the client
                # loads it whatever the api looks like
                dead = not self._jar_alive(jar, cache)
            else:
                # a site that talks to an http endpoint needs no crawler, so a
                # dead spider is only fatal to one that inherits it
                dead = spider_dead and not api.startswith(("http://", "https://"))
            if dead:
                dropped += 1
                LOGGER.info("site dropped for a dead jar", extra={
                    "stage": "build", "check": "mirror",
                    "site_name": str(site.get("name") or "")[:60],
                    "site_key": str(site.get("key") or "")[:40]})
                continue
            kept.append(site)
        if not dropped and not spider_dead:
            return text, 0, len(config["sites"])
        config["sites"] = kept
        if spider_dead:
            # every site that needed the crawler has just been pruned, so the
            # field would only make the client report "jar加载失败" the moment
            # the source is opened
            config.pop("spider", None)
            LOGGER.info("dead spider removed", extra={
                "stage": "build", "check": "mirror", "spider": str(spider)[:120]})
        return json.dumps(config, ensure_ascii=False, separators=(",", ":")), dropped, len(kept)

    # -- jar hosting -------------------------------------------------------
    def _fetch_blob(self, url: str, state: dict[str, Any]) -> bytes | None:
        """Fetch a reference we intend to re-serve.

        Every check that stops us paying for the same dead file twice lives
        here, so the jar pass and the child-config pass cannot drift apart.
        """
        if url in state.get("unavailable", ()):
            # the same dead jar sits in dozens of configs; paying its timeout
            # once per config is what emptied the hosting budget
            return None
        host = urlsplit(url).hostname or ""
        if host and not _is_address_literal(host) and host in state.get("dead_hosts", ()):
            # a host that does not resolve, refuses the connection or fails the
            # TLS handshake will not serve its *other* files either.  One
            # timeout per host, not one per jar on it.
            state.setdefault("unavailable", set()).add(url)
            return None
        if state["used"] >= self.jar_total_bytes:
            return None
        if state.get("started") is None:
            state["started"] = time.monotonic()
        elif time.monotonic() - state["started"] > self.host_seconds:
            if not state.get("warned"):
                state["warned"] = 1
                LOGGER.warning("hosting budget spent", extra={
                    "stage": "build", "check": "mirror",
                    "error": f"host_seconds>{self.host_seconds}"})
            return None
        result = self.http.get(url, max_bytes=self.jar_max_bytes)
        if not result.ok or not result.content:
            # Cache the reference so no later config pays the same timeout, but
            # do not confuse "we could not fetch it" with "it is gone".  Only a
            # response that settles the question - a 404/410, or a 2xx with an
            # empty body - may take the row away.  A timeout, a 5xx or a
            # refused connection is as likely to be our runner (or the proxy it
            # dials through) as the reference: the phone reaches hosts the
            # runner cannot, and treating a jitter as death silently dropped
            # sources that played fine once published.  Everything else keeps
            # the author's own URL and gets another chance next build.
            state.setdefault("unavailable", set()).add(url)
            if (host and not _is_address_literal(host)
                    and result.error_code in _DEAD_HOST_ERRORS):
                state.setdefault("dead_hosts", set()).add(host)
            if result.status in _DEAD_JAR_STATUS or (result.ok and not result.content):
                state.setdefault("gone", set()).add(url)
            LOGGER.warning("reference could not be fetched", extra={
                "stage": "build", "check": "mirror", "reference": url,
                "error": result.error_code or f"HTTP_{result.status}"})
            return None
        return result.content

    def _publish(self, blob: bytes, url: str, plan: MirrorPlan, state: dict[str, Any],
                 *, suffix: str | None = None) -> str | None:
        if state["used"] + len(blob) > self.jar_total_bytes:
            return None
        name = f"{hashlib.sha256(blob).hexdigest()[:20]}{suffix or _extension(url)}"
        plan.jars[name] = blob
        state["used"] += len(blob)
        return name

    def _localise_script_refs(self, blob: bytes, url: str, plan: MirrorPlan,
                              state: dict[str, Any], depth: int) -> bytes:
        """Re-serve the sibling modules a script imports."""
        base = self.public_base
        if not base:
            return blob
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            return blob
        cache: dict[str, str | None] = state.setdefault("script_refs", {})
        count = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal count
            specifier = match.group(3)
            if specifier.startswith(".."):
                return match.group(0)
            target = urljoin(_unwrap_proxy(url), specifier)
            if not target.startswith("http"):
                return match.group(0)
            if target not in cache:
                cache[target] = self._host_file(target, plan, state, depth - 1)
            name = cache[target]
            if not name:
                return match.group(0)
            count += 1
            quote = match.group(2)
            return f"{match.group(1)}{quote}{base}/{JARS_DIR}/{name}{quote}"

        rewritten = _SCRIPT_REF.sub(replace, text)
        return rewritten.encode("utf-8") if count else blob

    def _host_file(self, url: str, plan: MirrorPlan, state: dict[str, Any],
                   script_depth: int = 1) -> str | None:
        """Download ``url`` and publish it under a name derived from its bytes."""
        blob = self._fetch_blob(url, state)
        if blob is None:
            return None
        if _extension(url) == ".jar" and not _looks_like_a_jar(blob):
            # a 200 that is not a jar is a broken row, not a working one
            state.setdefault("unavailable", set()).add(url)
            state.setdefault("gone", set()).add(url)
            LOGGER.warning("reference is not a jar", extra={
                "stage": "build", "check": "mirror", "reference": url,
                "error": "NOT_A_JAR"})
            return None
        if script_depth > 0 and _extension(url) == ".js":
            blob = self._localise_script_refs(blob, url, plan, state, script_depth)
        return self._publish(blob, url, plan, state)

    def _host_child(self, url: str, plan: MirrorPlan, state: dict[str, Any],
                    depth: int) -> str | None:
        """Publish a multi-warehouse child so the client can really open it.

        A child is a config in its own right: its crawler, its csp scripts and
        its jars are named the way the parent names them, and half of those
        references are relative to the directory the child was published from.
        Copying the bytes to our own host without touching them moves the file
        and breaks every reference in it - which is what turned all twelve
        multi-warehouse sources into "解析配置失败" the moment they were opened.
        """
        blob = self._fetch_blob(url, state)
        if blob is None:
            return None
        if not _looks_like_a_config(blob):
            state.setdefault("unavailable", set()).add(url)
            # remembered apart from "we could not fetch it": this one is
            # provably not a config, so the parent drops the row rather than
            # leaving the client a child that can only fail
            state.setdefault("not_a_config", set()).add(url)
            state.setdefault("gone", set()).add(url)
            LOGGER.warning("child config is not a config", extra={
                "stage": "build", "check": "mirror", "reference": url,
                "error": "CHILD_IS_HTML" if _looks_like_html(blob)
                         else "CHILD_IS_NOT_A_CONFIG"})
            return None
        origin = _unwrap_proxy(url)
        original = blob.decode("utf-8-sig", "replace")
        text, hosted = self._localise_child_refs(original, origin, plan, state)
        if depth > 0:
            text, deeper = self._localise_structured(text, plan, state, depth - 1)
            hosted += deeper
        if text != original:
            # compare against the text, not against the hosted count: a
            # relative reference we could not re-serve is still rewritten to
            # its absolute origin, and dropping that change would put the
            # relative path back on our own directory
            blob = text.encode("utf-8")
        if hosted:
            # the same pruning the top-level config gets, so a child never
            # lists a row whose jar we could not mirror
            pruned, dropped, drop_child = self._prune_unavailable(
                blob.decode("utf-8", "replace"), state)
            if drop_child:
                LOGGER.warning("child config dropped for an unreachable spider", extra={
                    "stage": "build", "check": "mirror", "reference": url,
                    "error": "CHILD_SPIDER_UNAVAILABLE"})
                return None
            if dropped:
                blob = pruned.encode("utf-8")
        starts_json = blob.lstrip()[:1] in (b"{", b"[")
        return self._publish(blob, url, plan, state,
                             suffix=".json" if starts_json else None)

    def _localise_child_refs(self, text: str, origin: str, plan: MirrorPlan,
                             state: dict[str, Any]) -> tuple[str, int]:
        """Re-serve the files a child config names, relative or not.

        ``spider`` and ``jar`` are always files.  ``api`` and ``ext`` are only
        files when written as a relative path (``./js/drpy.min.js``,
        ``./1226lib/闪雷影视.js``); anything else is a spider key, a JSON
        endpoint or a live-site parameter, and snapshotting one pins a live
        service and breaks VIP playback - the exact mistake the ``parses`` pass
        made once already.

        ``ext`` is how a js spider names its script, and on the published set it
        was the last relative reference the child pass left behind: 71 of them
        across two children of one multi-warehouse source, every one a 404 on
        our own host because the child was republished into another directory.
        """
        base = self.public_base
        if not base:
            return text, 0
        cache: dict[str, str | None] = {}
        count = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal count
            key, sep, value = match.group(1), match.group(2), match.group(3)
            head, tail = value, ""
            found = _REF_TAIL.search(value)
            if found:
                head, tail = value[:found.start()], found.group(0)
            url = head.strip()
            relative = url.startswith(("./", "../"))
            if relative:
                if url.startswith(".."):
                    return match.group(0)
                absolute = urljoin(origin, url)
                if not absolute.startswith("http"):
                    return match.group(0)
                url = absolute
            elif key not in ("spider", "jar") or not url.startswith(("http://", "https://")):
                # an absolute api/ext/url/logo is a live endpoint, a spider key
                # or a remote picture, and snapshotting one pins a live service
                return match.group(0)
            if url.startswith(base + "/"):
                return match.group(0)
            if url not in cache:
                cache[url] = self._host_file(url, plan, state)
            name = cache[url]
            if not name:
                # we cannot re-serve it, but the client may still reach the
                # origin; a relative path would land on *our* directory instead
                return f'"{key}"{sep}{url}{tail}"' if relative else match.group(0)
            count += 1
            return f'"{key}"{sep}{base}/{JARS_DIR}/{name}{tail}"'

        return _CHILD_REF.sub(replace, text), count

    def _localise_refs(self, text: str, plan: MirrorPlan, state: dict[str, int]) -> tuple[str, int]:
        """Serve every proxied reference from our own host.

        A third party in the path is one more thing that can be blocked, and on
        a real client it was the whole difference: a spider served from
        gitee.com loaded, while everything served through an acceleration proxy
        came back "jar加载失败".  The client already reaches the host the config
        itself comes from, so the files go there too - crawlers, live playlists
        and child configs alike - under a name taken from their bytes, which
        keeps any ``;md5;`` check intact because the bytes are never touched.
        """
        if not self.host_jars:
            return text, 0
        base = self.public_base
        prefix = self.inner_proxy
        if not base or not prefix:
            return text, 0
        count = 0
        seen: dict[str, str | None] = {}

        def replace(match: re.Match[str]) -> str:
            nonlocal count
            reference = match.group(1)
            tail = ""
            found = _REF_TAIL.search(reference)
            if found:
                reference, tail = reference[:found.start()], found.group(0)
            url = reference.strip()
            if not url.startswith("http"):
                return match.group(0)
            if f"{prefix}{url}".startswith(base + "/"):
                # our own published copy.  A child we just re-served carries
                # the same raw.githubusercontent prefix, and fetching it back
                # would ask the site for a file this run has not pushed yet -
                # a 404 that then prunes the child we just finished hosting.
                return match.group(0)
            # fetch through the proxy hop the text actually carries: the bare
            # origin is the one host the client - and often we - cannot reach
            name = seen.get(url) if url in seen else self._host_file(f"{prefix}{url}", plan, state)
            seen[url] = name
            if not name:
                return match.group(0)
            count += 1
            return f"{base}/{JARS_DIR}/{name}{tail}"

        pattern = re.compile(re.escape(prefix) + r"([^\s\"'<>]+)")
        return pattern.sub(replace, text), count

    def _localise_structured(
        self, text: str, plan: MirrorPlan, state: dict[str, Any], depth: int = 1
    ) -> tuple[str, int]:
        """Serve every file reference the client loads from our own host.

        ``_localise_refs`` only rewrites references that already carry a proxy
        prefix.  A reference straight at ``kstore.space``, ``bgithub.xyz`` or
        ``hz.cz`` is just as likely to be unreachable from the client, and the
        client reports the source as 解析失败 / jar加载失败 the moment it is
        opened - four fifths of the catalogue were crawler sites, so that one
        field decided whether the whole list worked.

        A runner cannot see the client's network, so we stop guessing: whatever
        the source names as a *file* - the crawler jar or the spider config -
        is fetched here and handed back on a host we have confirmed the client
        reaches.

        Only those two.  ``parses[]`` looks like a file list and is not one:
        a ``type: 0`` entry is a web parser that takes a ``?url=`` at play time,
        and snapshotting it breaks VIP playback for every site that uses it.
        For the same reason ``api`` / ``ext`` are never rewritten.
        """
        if not self.host_jars:
            return text, 0
        base = self.public_base
        if not base:
            return text, 0
        try:
            config = _load_config(text)
        except ValueError:
            return text, 0

        cache: dict[str, str | None] = {}
        count = 0

        def localise(value: Any) -> str | None:
            if not isinstance(value, str):
                return None
            head, tail = value, ""
            found = _REF_TAIL.search(value)
            if found:
                head, tail = value[:found.start()], found.group(0)
            url = head.strip()
            if not url.startswith(("http://", "https://")):
                return None
            if url.startswith(base + "/"):
                return None
            if url not in cache:
                cache[url] = self._host_file(url, plan, state)
            name = cache[url]
            if not name:
                return None
            return f"{base}/{JARS_DIR}/{name}{tail}"

        def swap(container: dict[str, Any], key: str) -> None:
            nonlocal count
            replacement = localise(container.get(key))
            if replacement:
                container[key] = replacement
                count += 1

        def swap_children(container: dict[str, Any]) -> None:
            """Descend one level: a child is a config, not an opaque file."""
            nonlocal count
            children = container.get("urls")
            if not isinstance(children, list):
                return
            kept: list[Any] = []
            not_a_config = state.get("not_a_config") or set()
            for child in children:
                target = child.get("url") if isinstance(child, dict) else child
                if not isinstance(target, str) or not target.startswith(("http://", "https://")):
                    kept.append(child)
                    continue
                if target.startswith(base + "/"):
                    kept.append(child)
                    continue
                if target not in cache:
                    cache[target] = self._host_child(target, plan, state, depth)
                name = cache[target]
                if not name:
                    if target in not_a_config:
                        # an error page or a picture, not a config: the row
                        # could only ever report 解析配置失败, so it does not
                        # travel with the list
                        count += 1
                        continue
                    # we could not reach it, but the client still might: the
                    # author's own URL beats no row at all
                    kept.append(child)
                    continue
                if isinstance(child, dict):
                    child["url"] = f"{base}/{JARS_DIR}/{name}"
                    kept.append(child)
                else:
                    kept.append(f"{base}/{JARS_DIR}/{name}")
                count += 1
            if len(kept) != len(children):
                container["urls"] = kept

        rows: list[Any]
        wrapped = False
        if isinstance(config, list):
            # a few authors ship a bare array; wrap it so the client's config
            # parser sees the object shape it expects
            rows = config
            for row in rows:
                if isinstance(row, dict):
                    swap(row, "jar")
            config = {"sites": rows}
            wrapped = True
        elif isinstance(config, dict):
            swap(config, "spider")
            for row in config.get("sites") or []:
                if isinstance(row, dict):
                    swap(row, "jar")
            # a multi-warehouse config hands the client a second list; those
            # children are configs in their own right and get opened up
            swap_children(config)
        else:
            return text, 0

        if not count and not wrapped:
            return text, 0
        return json.dumps(config, ensure_ascii=False, separators=(",", ":")), count

    def _prune_unavailable(
        self, text: str, state: dict[str, Any]
    ) -> tuple[str, int, bool]:
        """Drop only what is *proven* gone, so the client never shows it.

        ``state["gone"]`` holds the references we saw a final answer for - a
        404/410, a jar whose bytes are not a jar, a child that is not a config.
        ``state["unavailable"]`` is the *fetch cache* (a timeout we paid once so
        the next config does not pay it again); it is deliberately not consulted
        here, because "we could not reach it" is not "it is dead" - the phone
        reaches hosts the runner cannot.

        Returns ``(text, dropped_sites, drop_source)``.  A crawler the client
        loads on open decides whether the whole entry works - 影视仓 answers
        "解析配置失败" - so a gone spider takes the source with it.  A crawler a
        single row names is only that row's problem.
        """
        gone_refs = state.get("gone") or set()
        if not gone_refs:
            return text, 0, False
        try:
            config = _load_config(text)
        except ValueError:
            return text, 0, False

        def gone(reference: Any) -> bool:
            if not isinstance(reference, str) or not reference.startswith(("http://", "https://")):
                return False
            return reference.split(";md5;")[0].strip() in gone_refs

        dropped = 0
        if isinstance(config, list):
            kept = [row for row in config
                    if not (isinstance(row, dict) and gone(row.get("jar")))]
            dropped = len(config) - len(kept)
            if not dropped:
                return text, 0, False
            config = {"sites": kept}
        elif isinstance(config, dict):
            if gone(config.get("spider")):
                return text, 0, True
            children = config.get("urls")
            if isinstance(children, list):
                kept_children = [
                    child for child in children
                    if not gone(child.get("url") if isinstance(child, dict) else child)
                ]
                dropped += len(children) - len(kept_children)
                config["urls"] = kept_children
            rows = config.get("sites")
            if isinstance(rows, list):
                kept = [row for row in rows
                        if not (isinstance(row, dict) and gone(row.get("jar")))]
                dropped = len(rows) - len(kept)
                if not dropped:
                    return text, 0, False
                config["sites"] = kept
            # everything the config pointed at is gone, so there is nothing
            # left for the client to open
            if not config.get("sites") and not config.get("urls"):
                return text, 0, True
            if not config.get("sites") and gone(config.get("spider")):
                config.pop("spider", None)
        if not dropped:
            return text, 0, False
        return json.dumps(config, ensure_ascii=False, separators=(",", ":")), dropped, False

    # -- work --------------------------------------------------------------
    def prepare(self, sources: Iterable[Source]) -> MirrorPlan:
        plan = MirrorPlan()
        if not self.enabled:
            return plan
        # A whitelisted config is backed by a real-device verdict, while the
        # shared jar cache below is only as reliable as this runner's network.
        # A single earlier probe can therefore poison a later config that the
        # phone already plays.  Spend the clean cache and hosting budget on
        # user-verified entries first.
        sources = sorted(
            sources,
            key=lambda source: (0 if source.whitelisted else 1, source.id),
        )
        plan.enabled = True
        base = self.public_base
        budget = self.max_total_bytes
        used = 0
        jar_cache: dict[str, bool] = {}
        jar_state: dict[str, Any] = {"used": 0}

        for source in sources:
            url = (source.raw_url or source.url or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            if url.startswith(base + "/"):
                # already mirrored; a re-run must not double-wrap it
                continue

            result = self.http.get(url, max_bytes=self.max_bytes)
            if not result.ok or not result.content:
                self._failed(source, url, result.error_code or f"HTTP_{result.status}")
                if source.whitelisted:
                    # A real-device verdict outranks a runner-side fetch failure.
                    # Reuse the last good copy when we have one; otherwise leave
                    # the original URL in place rather than dropping the source.
                    self._restore_previous_mirror(source, url, base, plan)
                    continue
                if self.on_failure != "keep":
                    plan.dropped.add(source.id)
                continue
            if used + len(result.content) > budget:
                LOGGER.warning("mirror budget exhausted", extra={
                    "stage": "build", "check": "mirror", "source_id": source.id,
                    "error": f"total>{budget}"})
                if self._keep_whitelisted_source(source, url, base, plan, "BUDGET_EXHAUSTED"):
                    continue
                plan.dropped.add(source.id)
                continue

            text = result.content.decode("utf-8", "replace")
            try:
                config = _load_config(text)
            except ValueError:
                config = None

            # name extraction reads the origin text; the published copy gets
            # its sibling references resolved, then proxied
            text, resolved = rewrite_relative(text, url)
            text, rewrites = rewrite_inner(text, self.inner_proxy)
            text, pruned, remaining = self._prune_dead_jars(text, jar_cache)
            if remaining == 0:
                LOGGER.warning("every site had a dead jar", extra={
                    "stage": "build", "check": "mirror", "source_id": source.id,
                    "url": url, "error": "DEAD_JARS_ONLY"})
                if self._keep_whitelisted_source(source, url, base, plan, "DEAD_JARS_ONLY"):
                    continue
                if self.on_failure != "keep":
                    plan.dropped.add(source.id)
                continue
            # the children go first.  _localise_refs snapshots every URL that
            # carries the proxy hop, and after rewrite_inner above that is
            # exactly what a multi-warehouse child URL looks like: running it
            # first republished the child's bytes verbatim under our own path,
            # which moved the directory its "./jar/x.jar" references were
            # written against.  Twelve shells shipped that way, and every one
            # answered 解析配置失败 the moment the client opened it.
            text, bare_hosted = self._localise_structured(text, plan, jar_state)
            text, hosted = self._localise_refs(text, plan, jar_state)
            hosted += bare_hosted
            if self.prune_dead_jars and self.host_jars:
                text, unfetched, drop_source = self._prune_unavailable(text, jar_state)
                if drop_source:
                    LOGGER.warning("source dropped for an unreachable spider", extra={
                        "stage": "build", "check": "mirror", "source_id": source.id,
                        "url": url, "error": "SPIDER_UNAVAILABLE"})
                    if self._keep_whitelisted_source(
                        source, url, base, plan, "SPIDER_UNAVAILABLE"
                    ):
                        continue
                    if self.on_failure != "keep":
                        plan.dropped.add(source.id)
                    continue
                if unfetched:
                    pruned += unfetched
                    remaining = (remaining - unfetched) if remaining is not None else None
                if remaining == 0:
                    LOGGER.warning("every site had an unreachable jar", extra={
                        "stage": "build", "check": "mirror", "source_id": source.id,
                        "url": url, "error": "UNREACHABLE_JARS_ONLY"})
                    if self._keep_whitelisted_source(
                        source, url, base, plan, "UNREACHABLE_JARS_ONLY"
                    ):
                        continue
                    if self.on_failure != "keep":
                        plan.dropped.add(source.id)
                    continue
            final = None
            try:
                final = _load_config(text)
            except ValueError:
                final = None
            if isinstance(final, dict) and not (
                    final.get("sites") or final.get("lives") or final.get("urls")):
                # every child was an error page, or every row lost its jar:
                # there is nothing left for the client to open
                LOGGER.warning("source has nothing the client can open", extra={
                    "stage": "build", "check": "mirror", "source_id": source.id,
                    "url": url, "error": "NO_OPENABLE_ROWS"})
                if self._keep_whitelisted_source(
                    source, url, base, plan, "NO_OPENABLE_ROWS"
                ):
                    continue
                if self.on_failure != "keep":
                    plan.dropped.add(source.id)
                continue
            content = text.encode("utf-8")

            used += len(content)
            slug = source.id[:16]
            plan.entries[source.id] = MirrorEntry(
                source_id=source.id,
                slug=slug,
                name=_pick_name(config, url, source),
                url=f"{base}/{SOURCES_DIR}/{slug}.json",
                site_count=remaining if remaining is not None else _site_count(config),
                content=content,
                origin=url,
                inner_rewrites=resolved + rewrites,
                hosted=hosted,
            )

        self._drop_duplicate_content(plan)
        self._dedupe_names(plan)
        return plan

    def _keep_whitelisted_source(
        self, source: Source, url: str, base: str, plan: MirrorPlan, reason: str
    ) -> bool:
        """Never drop a user-verified source for a runner-side mirror failure."""
        if not source.whitelisted:
            return False
        if self._restore_previous_mirror(source, url, base, plan):
            return True
        LOGGER.info("mirror kept the original URL for a verified source", extra={
            "stage": "build", "check": "mirror", "source_id": source.id,
            "source_name": source.name, "url": url, "error": reason,
        })
        return True

    def _restore_previous_mirror(
        self, source: Source, url: str, base: str, plan: MirrorPlan
    ) -> bool:
        """Keep a verified source alive when this run cannot fetch it.

        The phone has already proved the config works, while the runner's
        failure (TLS, DNS, a flaky proxy) says nothing about the client.  A
        previously published copy is still the best artifact we can send.
        """
        slug = source.id[:16]
        path = self.dist_dir / SOURCES_DIR / f"{slug}.json"
        try:
            content = path.read_bytes()
        except OSError:
            return False
        if not content:
            return False
        try:
            config = _load_config(content.decode("utf-8", "replace"))
        except ValueError:
            config = None
        plan.entries[source.id] = MirrorEntry(
            source_id=source.id,
            slug=slug,
            name=_pick_name(config, url, source),
            url=f"{base}/{SOURCES_DIR}/{slug}.json",
            site_count=_site_count(config),
            content=content,
            origin=url,
        )
        LOGGER.info("mirror kept a verified source from its previous copy", extra={
            "stage": "build", "check": "mirror", "source_id": source.id,
            "source_name": source.name, "url": url,
        })
        return True

    def _failed(self, source: Source, url: str, reason: str) -> None:
        LOGGER.warning("mirror fetch failed", extra={
            "stage": "build", "check": "mirror", "source_id": source.id,
            "source_name": source.name, "url": url, "error": reason,
        })

    @staticmethod
    def _drop_duplicate_content(plan: MirrorPlan) -> None:
        """Publish identical bytes once.

        Several discovered sources mirror the same upstream file.  Listing it
        twice only pads the client's list with entries that behave identically.
        """
        seen: dict[str, str] = {}
        for source_id in list(plan.entries):
            entry = plan.entries[source_id]
            digest = hashlib.sha256(entry.content).hexdigest()
            first = seen.get(digest)
            if first is None:
                seen[digest] = source_id
                continue
            LOGGER.info("mirror dropped a duplicate config", extra={
                "stage": "build", "check": "mirror", "source_id": source_id,
                "source_name": entry.name, "error": f"identical to {first}"})
            del plan.entries[source_id]
            plan.dropped.add(source_id)

    @staticmethod
    def _dedupe_names(plan: MirrorPlan) -> None:
        counts: dict[str, int] = {}
        for entry in plan.entries.values():
            counts[entry.name] = counts.get(entry.name, 0) + 1
        seen: dict[str, int] = {}
        for entry in plan.entries.values():
            if counts[entry.name] > 1:
                seen[entry.name] = seen.get(entry.name, 0) + 1
                entry.name = f"{entry.name} #{seen[entry.name]}"
            if entry.site_count:
                entry.name = f"{entry.name}（{entry.site_count}站）"

    def write(self, plan: MirrorPlan) -> Path | None:
        """Persist the mirrored configs next to tvbox.json."""
        if not plan.enabled:
            return None
        target = self.dist_dir / SOURCES_DIR
        target.mkdir(parents=True, exist_ok=True)
        written = set()
        for entry in plan.entries.values():
            path = target / f"{entry.slug}.json"
            path.write_bytes(entry.content)
            written.add(path.name)
        # a config that is no longer published must not linger on the site
        for stale in target.glob("*.json"):
            if stale.name not in written:
                try:
                    stale.unlink()
                except OSError:
                    pass
        self._write_jars(plan)
        return target

    def _write_jars(self, plan: MirrorPlan) -> None:
        """Publish the hosted jars, and drop the ones nothing points at."""
        jdir = self.dist_dir / JARS_DIR
        if plan.jars:
            jdir.mkdir(parents=True, exist_ok=True)
        if not jdir.exists():
            return
        for name, blob in plan.jars.items():
            path = jdir / name
            if not path.exists() or path.stat().st_size != len(blob):
                path.write_bytes(blob)
        for stale in jdir.glob("*.jar"):
            if stale.name not in plan.jars:
                try:
                    stale.unlink()
                except OSError:
                    pass
