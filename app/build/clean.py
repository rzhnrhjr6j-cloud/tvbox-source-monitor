"""Child-safe second entry point (user request 2026-10-11).

The primary entry (``tvbox.json``) publishes every verified config exactly as
its author wrote it.  Some of those configs carry adult sites and adult live
channels (``🔞`` / ``🈲`` rows, 麻豆 / 黄色仓库 / 色播 ...), which makes the
list unusable on a family device.  This module derives a second entry from the
first one:

* a published source whose own name matches the policy is dropped whole;
* inside every remaining source the matching ``sites`` / ``lives`` rows are
  removed, and the trimmed config is written to its own directory so the clean
  entry never points back at the full one;
* a row that merely *points at* an adult file is dropped too: a spider named
  ``黄果短剧[py]`` reveals nothing until you read the ``.py`` it loads, so the
  caller scans the mirrored files once and hands the offending names in via
  ``unsafe_refs``;
* a ``lives`` row can hide the same way behind a *remote* playlist it never
  mirrors (``🍓直播(蜂蜜)`` -> a ``.txt`` whose channels read 俄罗斯成人N台), so
  the caller also fetches those playlists once and hands the offending urls in
  through the ``live_scan`` callback;
* a source whose every site was removed is dropped too - it would open to an
  empty list, which the client reads as a broken source.

Every rule lives in ``output.clean`` (spec §35-14: nothing hard-coded), so the
block list can be extended without touching code.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# config keys that hold a list of "stations" the client shows to the user
LIST_KEYS = ("sites", "lives")

# Row fields carrying text a user can see or that reveals where the row
# points.  The adult rows inside an otherwise normal config often hide behind
# an innocent-looking name (``AV-饮水机资源`` / ``白嫖采集``), so the name
# alone is not enough: the crawler key, the api path and the live url give
# them away (``pornhub`` / ``/plugin/adult/`` / ``Adult.m3u``).
ROW_TEXT_FIELDS = ("name", "api", "key", "jar", "url")


def content_hit(text: str, keyword: str) -> bool:
    """Whether ``keyword`` occurs in ``text``, word-bounded for latin words.

    ``av`` must not hit ``have`` and ``porn`` must not hit ``pornhub`` unless it
    is spelled out, so a latin keyword matches only between non-word characters.
    A CJK keyword has no word boundaries and is matched as a plain substring.
    """
    if keyword.isascii():
        return re.search(
            r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", text
        ) is not None
    return keyword in text


# Suffixes worth reading when judging a mirrored file: a spider is a .py / .js /
# .json and a live row points at a .m3u / .m3u8 / .txt.  A .jar is compiled and
# an image is not text, so neither is opened - decoding them only produces byte
# noise (``xxx`` turns up inside a random jar) that would condemn innocent rows.
TEXT_SUFFIXES = frozenset({".json", ".js", ".py", ".txt", ".m3u", ".m3u8"})


def _content_patterns(keywords: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """One precompiled pattern per script kind, so a file is scanned twice."""
    latin = sorted((w for w in keywords if w.isascii()), key=len, reverse=True)
    other = sorted((w for w in keywords if not w.isascii()), key=len, reverse=True)
    patterns: list[re.Pattern[str]] = []
    if latin:
        patterns.append(
            re.compile(
                r"(?<![a-z0-9])(?:"
                + "|".join(re.escape(word) for word in latin)
                + r")(?![a-z0-9])",
                re.IGNORECASE,
            )
        )
    if other:
        patterns.append(re.compile("|".join(re.escape(word) for word in other)))
    return tuple(patterns)


def scan_unsafe_refs(directory: Path | None, policy: "CleanPolicy") -> frozenset[str]:
    """Names of mirrored files whose *content* is adult.

    A site called ``黄果短剧[py]`` says nothing; the ``.py`` it loads does.  The
    caller scans the mirror once per build - before ``build_clean_entry`` - and a
    row pointing at any name returned here is dropped even though its own text
    looks innocent.
    """
    if directory is None or not policy.content_keywords:
        return frozenset()
    patterns = _content_patterns(policy.content_keywords)
    if not patterns or not directory.is_dir():
        return frozenset()
    hits: set[str] = set()
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(pattern.search(text) for pattern in patterns):
            hits.add(path.name)
    return frozenset(hits)


# A live playlist is a plain channel list, and an adult channel can sit far
# down it, so the ceiling is looser than the 5 MiB default the mirror uses.
LIVE_SCAN_MAX_BYTES = 12 * 1024 * 1024
# how many playlists are fetched at once, and the ceiling per build
LIVE_SCAN_WORKERS = 8
LIVE_SCAN_BUDGET = 300


def remote_live_urls(child: dict[str, Any]) -> list[str]:
    """Remote playlists a config's ``lives`` rows point at, in row order."""
    urls: list[str] = []
    for row in child.get("lives") or []:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            urls.append(url)
    return urls


def http_fetcher(
    client: Any, *, max_bytes: int = LIVE_SCAN_MAX_BYTES
) -> Callable[[str], str | None]:
    """``fetch`` adapter for :func:`make_live_scanner` over an ``HttpClient``."""

    def fetch(url: str) -> str | None:
        try:
            result = client.get(url, max_bytes=max_bytes)
        except Exception:  # a build must never die on one unreachable playlist
            return None
        if result is None or not getattr(result, "ok", False):
            return None
        return result.text

    return fetch


def make_live_scanner(
    policy: "CleanPolicy",
    fetch: Callable[[str], str | None],
    *,
    budget: int = LIVE_SCAN_BUDGET,
    workers: int = LIVE_SCAN_WORKERS,
) -> Callable[[dict[str, Any]], frozenset[str]]:
    """Callback that judges the *playlist* a ``lives`` row points at.

    The row name says nothing and the playlist is not mirrored, so it has to be
    fetched: the keywords that condemn a mirrored file condemn it just the same.
    Each url is fetched once per build; a playlist that cannot be read is kept
    (a transient failure must not delete a legitimate live source) and counted
    in ``stats`` so the build can report what it could *not* verify.
    """
    patterns = _content_patterns(policy.content_keywords)
    stats: dict[str, int] = {"checked": 0, "failed": 0, "flagged": 0, "skipped": 0}
    seen: dict[str, bool] = {}

    def scanner(child: dict[str, Any]) -> frozenset[str]:
        if not patterns:
            return frozenset()
        fresh = [u for u in dict.fromkeys(remote_live_urls(child)) if u not in seen]
        if len(fresh) > budget:
            stats["skipped"] += len(fresh) - budget
            fresh = fresh[:budget]
        if fresh:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                bodies = list(pool.map(fetch, fresh))
            for url, text in zip(fresh, bodies):
                if text is None:
                    stats["failed"] += 1
                    seen[url] = False
                    continue
                stats["checked"] += 1
                seen[url] = any(pattern.search(text) for pattern in patterns)
                stats["flagged"] += bool(seen[url])
        return frozenset(url for url, unsafe in seen.items() if unsafe)

    scanner.stats = stats  # type: ignore[attr-defined]
    return scanner


def iter_strings(node: Any):
    """Yield every string nested anywhere inside ``node``."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from iter_strings(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from iter_strings(value)


def _marks(value: Any, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """YAML value -> tuple of non-empty strings. ``None`` means "use default"."""
    if value is None:
        return default
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return default
    return tuple(str(item).strip() for item in value if str(item).strip())


@dataclass(frozen=True)
class CleanPolicy:
    """What the child-safe entry must not contain."""

    enabled: bool = False
    output_file: str = "tvbox-clean.json"
    directory: str = "clean"
    # a name carrying one of these marks is adult by definition
    block_emoji: tuple[str, ...] = ("🔞", "🈲")
    # plain substrings, matched case-insensitively
    block_keywords: tuple[str, ...] = ()
    # substrings matched against *every* string of a row, including the nested
    # category lists (``ext`` / ``categories``).  A whole adult config can be
    # renamed to 杂交水稻 / 樱花 / 女神视频 and stay invisible to the name
    # check while its category list still reads 乱伦 / 无码 / 探花 - so a row is
    # unsafe when anything it carries matches.  This is the narrow set that
    # never appears in a legitimate category list (no 激情 / 桃花 / 学生), so
    # matching it this deep does not drop ordinary sources.
    block_keywords_deep: tuple[str, ...] = ()
    # substrings that only ever appear on an adult live row - its name or the
    # playlist it points at.  Matched against ``lives`` rows alone, so a word
    # like 经典 cannot sink an ordinary VOD source that happens to use it.
    block_keywords_live: tuple[str, ...] = ()
    # tokens used to classify a *referenced file* (a mirrored spider or
    # playlist) as adult.  The caller reads those files and passes the names
    # that matched in via ``unsafe_refs`` - see ``Builder._unsafe_refs``.
    content_keywords: tuple[str, ...] = ()
    # regular expressions, for names that a substring would mis-hit
    block_regex: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: Any) -> "CleanPolicy":
        if not isinstance(settings, dict) or not settings:
            return cls()
        return cls(
            enabled=bool(settings.get("enabled", False)),
            output_file=str(settings.get("output_file") or "tvbox-clean.json"),
            directory=str(
                settings.get("directory") or settings.get("dir") or "clean"
            ).strip("/") or "clean",
            block_emoji=_marks(settings.get("block_emoji"), default=("🔞", "🈲")),
            block_keywords=_marks(settings.get("block_keywords")),
            block_keywords_deep=_marks(settings.get("block_keywords_deep")),
            block_keywords_live=_marks(settings.get("block_keywords_live")),
            content_keywords=_marks(settings.get("content_keywords")),
            block_regex=_marks(settings.get("block_regex")),
        )

    # -- matching -----------------------------------------------------------
    def blocks(self, name: Any) -> bool:
        text = str(name or "")
        if not text.strip():
            return False
        return self._matches(text) or any(
            word.lower() in text.lower() for word in self.block_keywords
        )

    def _matches(self, text: str) -> bool:
        if any(mark in text for mark in self.block_emoji):
            return True
        for pattern in self.block_regex:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return True
            except re.error:
                continue
        return False

    # -- editing ------------------------------------------------------------
    def unsafe_row(
        self,
        row: dict[str, Any],
        *,
        live: bool = False,
        unsafe_refs: frozenset[str] | None = None,
    ) -> bool:
        """True when a ``sites`` / ``lives`` row must not reach the client."""
        text = " ".join(str(row.get(key) or "") for key in ROW_TEXT_FIELDS)
        if self.blocks(text):
            return True
        if live and self.block_keywords_live:
            low = text.lower()
            if any(word.lower() in low for word in self.block_keywords_live):
                return True
        deep = tuple(word.lower() for word in self.block_keywords_deep)
        if not deep and not unsafe_refs:
            return False
        for value in iter_strings(row):
            if deep and any(word in value.lower() for word in deep):
                return True
            if unsafe_refs and any(ref in value for ref in unsafe_refs):
                return True
            if self._matches(value):
                return True
        return False

    def strip_rows(
        self,
        payload: dict[str, Any],
        *,
        unsafe_refs: frozenset[str] | None = None,
    ) -> int:
        """Drop matching ``sites`` / ``lives`` rows in place; return how many."""
        removed = 0
        for key in LIST_KEYS:
            rows = payload.get(key)
            if not isinstance(rows, list):
                continue
            live = key == "lives"
            kept = []
            for row in rows:
                if isinstance(row, dict) and self.unsafe_row(
                    row, live=live, unsafe_refs=unsafe_refs
                ):
                    removed += 1
                    continue
                kept.append(row)
            if len(kept) != len(rows):
                payload[key] = kept
        return removed

    def is_empty(self, payload: dict[str, Any]) -> bool:
        """True when nothing playable is left, so the entry must be dropped."""
        if any(payload.get(key) for key in LIST_KEYS):
            return False
        return not payload.get("parses")


@dataclass
class CleanResult:
    payload: Any
    # slug -> trimmed config that must be written beside the clean entry
    children: dict[str, dict[str, Any]] = field(default_factory=dict)
    dropped_sources: list[str] = field(default_factory=list)
    removed_rows: int = 0
    rewritten: int = 0


def build_clean_entry(
    payload: Any,
    policy: CleanPolicy,
    read_child: Callable[[str], Any | None],
    *,
    source_prefix: str,
    clean_prefix: str,
    unsafe_refs: frozenset[str] | None = None,
    live_scan: Callable[[dict[str, Any]], frozenset[str]] | None = None,
) -> CleanResult:
    """Derive the child-safe entry from an already rendered one.

    ``read_child`` receives the slug of a mirrored config and returns its
    parsed JSON (or ``None`` when we cannot read it).  ``source_prefix`` is the
    URL prefix those configs are published under; ``clean_prefix`` is where the
    trimmed copies go.  A child that had nothing removed keeps its original URL,
    so the clean entry duplicates only what it actually had to edit.

    ``unsafe_refs`` is the set of mirrored file names whose *content* is adult;
    a row that references one is dropped even when its own name looks innocent.

    ``live_scan`` receives each surviving config and returns the *remote*
    playlists its ``lives`` rows point at that turned out to be adult; those
    rows are dropped the same way.
    """
    if isinstance(payload, list):
        entries: Any = payload
    elif isinstance(payload, dict):
        entries = payload.get("urls")
    else:
        return CleanResult(payload=payload)
    if not isinstance(entries, list):
        return CleanResult(payload=payload)

    result = CleanResult(payload=payload)
    clean_entries: list[Any] = []
    for item in entries:
        if not isinstance(item, dict):
            clean_entries.append(item)
            continue
        name = str(item.get("name") or "")
        if policy.blocks(name):
            result.dropped_sources.append(name)
            continue
        url = item.get("url")
        slug = ""
        if isinstance(url, str) and source_prefix and url.startswith(source_prefix):
            slug = url[len(source_prefix):]
        child = None
        if slug and "/" not in slug:
            child = read_child(slug)
        if isinstance(child, dict):
            if live_scan is not None:
                unsafe_refs = (unsafe_refs or frozenset()) | live_scan(child)
            removed = policy.strip_rows(child, unsafe_refs=unsafe_refs)
            if removed:
                result.removed_rows += removed
                if policy.is_empty(child):
                    result.dropped_sources.append(name)
                    continue
                result.children[slug] = child
                item = dict(item)
                item["url"] = f"{clean_prefix}{slug}"
                result.rewritten += 1
        clean_entries.append(item)

    if isinstance(payload, dict):
        result.payload = dict(payload)
        result.payload["urls"] = clean_entries
    else:
        result.payload = clean_entries
    return result
