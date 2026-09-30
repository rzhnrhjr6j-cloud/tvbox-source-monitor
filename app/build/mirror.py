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
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..logging_setup import get_logger
from ..models import Source

LOGGER = get_logger("build.mirror")

SOURCES_DIR = "sources"

# [607KB/s|1290ms|稳] 360资源  ->  360资源
_BRACKET_PREFIX = re.compile(r"^\s*[\[\(【][^\]\)】]{0,40}[\]\)】]\s*")
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
    if isinstance(config, dict):
        sites = config.get("sites")
        if isinstance(sites, list):
            return len(sites)
    return 0


def _pick_name(config: Any, url: str, source: Source) -> str:
    """A short, human, ideally-Chinese label for the 影视仓 list.

    A config is a bundle of sites, so the most honest label is the source from
    which it came.  We prefer a usable Chinese site name (that is what the
    client actually sees inside), fall back to ``owner/repo`` for GitHub
    configs, and to the host otherwise.
    """
    sites = config.get("sites") if isinstance(config, dict) else None
    if isinstance(sites, list):
        for site in sites:
            if not isinstance(site, dict):
                continue
            candidate = _clean_name(site.get("name") or "")
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


@dataclass
class MirrorEntry:
    source_id: str
    slug: str
    name: str
    url: str
    site_count: int
    content: bytes
    origin: str


@dataclass
class MirrorPlan:
    """What the build should publish, and what it must leave out."""

    enabled: bool = False
    entries: dict[str, MirrorEntry] = field(default_factory=dict)
    dropped: set[str] = field(default_factory=set)

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
        # <owner>.github.io/<repo> is where the pages workflow publishes dist/
        repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if "/" in repository:
            owner, repo = repository.split("/", 1)
            return f"https://{owner}.github.io/{repo}"
        return ""

    @property
    def on_failure(self) -> str:
        return str(self.settings.get("on_failure", "drop")).lower()

    @property
    def max_bytes(self) -> int:
        return int(self.settings.get("max_bytes", 2 * 1024 * 1024))

    @property
    def max_total_bytes(self) -> int:
        return int(self.settings.get("max_total_bytes", 32 * 1024 * 1024))

    # -- work --------------------------------------------------------------
    def prepare(self, sources: Iterable[Source]) -> MirrorPlan:
        plan = MirrorPlan()
        if not self.enabled:
            return plan
        plan.enabled = True
        base = self.public_base
        budget = self.max_total_bytes
        used = 0

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
                if self.on_failure != "keep":
                    plan.dropped.add(source.id)
                continue
            if used + len(result.content) > budget:
                LOGGER.warning("mirror budget exhausted", extra={
                    "stage": "build", "check": "mirror", "source_id": source.id,
                    "error": f"total>{budget}"})
                plan.dropped.add(source.id)
                continue

            try:
                config = json.loads(result.content.decode("utf-8", "replace"))
            except ValueError:
                config = None

            used += len(result.content)
            slug = source.id[:16]
            plan.entries[source.id] = MirrorEntry(
                source_id=source.id,
                slug=slug,
                name=_pick_name(config, url, source),
                url=f"{base}/{SOURCES_DIR}/{slug}.json",
                site_count=_site_count(config),
                content=result.content,
                origin=url,
            )

        self._dedupe_names(plan)
        return plan

    def _failed(self, source: Source, url: str, reason: str) -> None:
        LOGGER.warning("mirror fetch failed", extra={
            "stage": "build", "check": "mirror", "source_id": source.id,
            "source_name": source.name, "url": url, "error": reason,
        })

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
        return target

