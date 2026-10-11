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
* a source whose every site was removed is dropped too - it would open to an
  empty list, which the client reads as a broken source.

Every rule lives in ``output.clean`` (spec §35-14: nothing hard-coded), so the
block list can be extended without touching code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

# config keys that hold a list of "stations" the client shows to the user
LIST_KEYS = ("sites", "lives")


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
            block_regex=_marks(settings.get("block_regex")),
        )

    # -- matching -----------------------------------------------------------
    def blocks(self, name: Any) -> bool:
        text = str(name or "")
        if not text.strip():
            return False
        if any(mark in text for mark in self.block_emoji):
            return True
        lowered = text.lower()
        if any(word.lower() in lowered for word in self.block_keywords):
            return True
        for pattern in self.block_regex:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return True
            except re.error:
                continue
        return False

    # -- editing ------------------------------------------------------------
    def strip_rows(self, payload: dict[str, Any]) -> int:
        """Drop matching ``sites`` / ``lives`` rows in place; return how many."""
        removed = 0
        for key in LIST_KEYS:
            rows = payload.get(key)
            if not isinstance(rows, list):
                continue
            kept = []
            for row in rows:
                if isinstance(row, dict) and self.blocks(row.get("name")):
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
) -> CleanResult:
    """Derive the child-safe entry from an already rendered one.

    ``read_child`` receives the slug of a mirrored config and returns its
    parsed JSON (or ``None`` when we cannot read it).  ``source_prefix`` is the
    URL prefix those configs are published under; ``clean_prefix`` is where the
    trimmed copies go.  A child that had nothing removed keeps its original URL,
    so the clean entry duplicates only what it actually had to edit.
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
            removed = policy.strip_rows(child)
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
