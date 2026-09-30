"""Whitelist / blacklist policy (spec §5.1, §27).

Whitelisted sources are never auto-retired; blacklisted ones never even enter
the probe pool.  Matching is by normalised URL, exact host, URL prefix or
``source_id`` so an operator does not have to chase every spelling of a URL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .logging_setup import get_logger
from .utils.urls import normalize_url, parse_url, source_id_for

LOGGER = get_logger("policy")


@dataclass
class Policy:
    urls: set[str] = field(default_factory=set)
    hosts: set[str] = field(default_factory=set)
    source_ids: set[str] = field(default_factory=set)
    url_prefixes: tuple[str, ...] = ()
    reasons: dict[str, str] = field(default_factory=dict)
    name: str = ""

    def __bool__(self) -> bool:
        return bool(self.urls or self.hosts or self.source_ids or self.url_prefixes)

    def matches(self, url: str, normalized: str | None = None, source_id: str | None = None) -> bool:
        if not self:
            return False
        normalized = normalized if normalized is not None else normalize_url(url)
        if not normalized:
            return False
        if normalized in self.urls:
            return True
        if source_id and source_id in self.source_ids:
            return True
        host = parse_url(normalized)
        hostname = host.host if host else ""
        if hostname and hostname in self.hosts:
            return True
        return any(normalized.startswith(prefix) for prefix in self.url_prefixes)

    def reason(self, url: str) -> str:
        normalized = normalize_url(url)
        return self.reasons.get(normalized) or self.reasons.get(parse_url(url).host if parse_url(url) else "") or ""


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def load_policy(path: Path, name: str = "") -> Policy:
    """Load a whitelist/blacklist file.  A missing file is an empty policy."""
    if not path.is_file():
        return Policy(name=name or path.stem)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("policy file unreadable, treating as empty", extra={"stage": "policy", "url": str(path), "error": str(exc)[:200]})
        return Policy(name=name or path.stem)
    if not isinstance(data, dict):
        return Policy(name=name or path.stem)

    urls = {normalize_url(item) for item in _as_list(data.get("urls"))}
    hosts = {str(item).strip().lower().lstrip(".") for item in _as_list(data.get("hosts"))}
    source_ids = {str(item).strip() for item in _as_list(data.get("source_ids"))}
    prefixes = tuple(sorted({normalize_url(item) for item in _as_list(data.get("url_prefixes"))} - {""}))
    reasons = {
        str(key): str(value)
        for key, value in (data.get("reason") or {}).items()
        if isinstance(value, (str, int, float))
    }
    return Policy(
        urls={item for item in urls if item},
        hosts={item for item in hosts if item},
        source_ids={item for item in source_ids if item},
        url_prefixes=prefixes,
        reasons=reasons,
        name=name or path.stem,
    )


class PolicySet:
    def __init__(self, cfg):
        data_dir = cfg.path("app.data_dir")
        self.whitelist = load_policy(data_dir / "whitelist.json", "whitelist")
        self.blacklist = load_policy(data_dir / "blacklist.json", "blacklist")
        self.aliases = self._load_aliases(data_dir / "source_aliases.json")

    @staticmethod
    def _load_aliases(path: Path) -> dict[str, str]:
        """extra equivalence rules: any URL in a group -> that group's canonical id."""
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        mapping: dict[str, str] = {}
        for canonical, members in (data.get("aliases") or {}).items():
            anchor = source_id_for(str(canonical))
            if not anchor:
                continue
            mapping[normalize_url(str(canonical))] = anchor
            for member in _as_list(members):
                key = normalize_url(member)
                if key:
                    mapping[key] = anchor
        return mapping

    def canonical_id(self, url: str) -> str:
        normalized = normalize_url(url)
        if not normalized:
            return ""
        return self.aliases.get(normalized) or source_id_for(normalized)

    def is_blacklisted(self, url: str) -> bool:
        normalized = normalize_url(url)
        return self.blacklist.matches(url, normalized, source_id_for(normalized))

    def is_whitelisted(self, url: str) -> bool:
        normalized = normalize_url(url)
        return self.whitelist.matches(url, normalized, source_id_for(normalized))

    def blacklist_reason(self, url: str) -> str:
        return self.blacklist.reason(url) or "blacklisted"

    def describe(self) -> dict[str, Any]:
        return {
            "whitelist": {
                "urls": len(self.whitelist.urls),
                "hosts": len(self.whitelist.hosts),
                "ids": len(self.whitelist.source_ids),
                "prefixes": len(self.whitelist.url_prefixes),
            },
            "blacklist": {
                "urls": len(self.blacklist.urls),
                "hosts": len(self.blacklist.hosts),
                "ids": len(self.blacklist.source_ids),
                "prefixes": len(self.blacklist.url_prefixes),
            },
            "aliases": len(self.aliases),
        }
