"""Optional web discovery adapter (spec §5.1, §26).

Disabled by default.  When enabled it:

* fetches ``robots.txt`` and honours it via :mod:`urllib.robotparser`
* sleeps ``request_delay_seconds`` between requests
* only ever reads the seed pages an operator listed in config
* never crawls recursively and never scans ports
"""

from __future__ import annotations

import time
from typing import Iterable
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from ..logging_setup import get_logger
from ..utils.urls import extract_urls, normalize_url
from .types import Candidate

LOGGER = get_logger("discovery.web")


class WebAdapter:
    name = "web"

    def __init__(self, cfg, client):
        section = (cfg.section_default("discovery").get("web") or {})
        self.enabled = bool(section.get("enabled", False))
        self.respect_robots = bool(section.get("respect_robots", True))
        self.seed_pages = [str(item) for item in (section.get("seed_pages") or [])]
        self.delay = float(section.get("request_delay_seconds", 2.0))
        self.user_agent = str(section.get("user_agent") or client.user_agent)
        self.client = client
        self._robots: dict[str, RobotFileParser | None] = {}
        self.stats: dict[str, int] = {"pages": 0, "urls": 0, "blocked": 0}

    def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        parser: RobotFileParser | None = None
        result = self.client.get(
            urljoin(origin, "/robots.txt"),
            timeout=(self.client.connect_timeout, self.client.read_timeout),
            max_bytes=256 * 1024,
        )
        if result.ok and result.text:
            parser = RobotFileParser()
            parser.parse(result.text.splitlines())
        self._robots[origin] = parser
        return parser

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            # no robots.txt -> nothing published to respect, allow
            return True
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:  # noqa: BLE001 - a broken robots.txt must not break discovery
            return True

    def discover(self) -> Iterable[Candidate]:
        if not self.enabled or not self.seed_pages:
            return []
        seen: dict[str, Candidate] = {}
        for index, page in enumerate(self.seed_pages):
            if index and self.delay:
                time.sleep(self.delay)
            parsed = urlsplit(page)
            if parsed.scheme not in ("http", "https"):
                continue
            if not self._allowed(page):
                self.stats["blocked"] += 1
                LOGGER.info("robots.txt disallowed seed page", extra={"stage": "discovery", "url": page})
                continue
            result = self.client.get(
                page,
                timeout=(self.client.connect_timeout, self.client.read_timeout),
                max_bytes=1024 * 1024,
            )
            self.stats["pages"] += 1
            if not result.ok:
                continue
            for url in extract_urls(result.text):
                key = normalize_url(url)
                if not key or key in seen:
                    continue
                seen[key] = Candidate(url=url, adapter=self.name, query=f"seed:{page}", name=page)
        self.stats["urls"] = len(seen)
        return list(seen.values())
