"""GitHub discovery adapter (spec §5.1.A, §26).

Searches public repositories for config-shaped JSON files.  Rate-limit aware
(reads ``x-ratelimit-remaining`` and stops early), token-authenticated, and
deliberately polite: bounded pages, bounded repos, no scraping of anything
that is not a plainly public file path.

Requires a token.  Without one GitHub's code-search API refuses to answer, so
the adapter disables itself instead of hammering the unauthenticated endpoint.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable
from urllib.parse import quote

from ..logging_setup import get_logger
from ..utils.urls import normalize_url
from .types import Candidate

LOGGER = get_logger("discovery.github")

_API_VERSION = "2022-11-28"


class GitHubAdapter:
    name = "github"

    def __init__(self, cfg, client):
        section = (cfg.section_default("discovery").get("github") or {})
        self.enabled = bool(section.get("enabled", True))
        self.api_base = str(section.get("api_base") or "https://api.github.com").rstrip("/")
        self.code_queries = [str(item) for item in (section.get("code_queries") or [])]
        self.repo_queries = [str(item) for item in (section.get("repo_queries") or [])]
        self.per_page = min(100, max(1, int(section.get("per_page", 30))))
        self.max_pages = max(1, int(section.get("max_pages", 2)))
        self.min_remaining = int(section.get("min_rate_limit_remaining", 5))
        self.path_patterns = [re.compile(item) for item in (section.get("path_patterns") or [r"\.json$"])]
        self.path_excludes = [re.compile(item) for item in (section.get("path_excludes") or [])]
        self.max_repos_for_trees = int(section.get("max_repos_for_trees", 5))
        self.client = client
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        self.rate_remaining: int | None = None
        self._branches: dict[str, str] = {}
        self.stats: dict[str, Any] = {"code_queries": 0, "repo_queries": 0, "files": 0, "skipped": 0}

    # -- plumbing ----------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.enabled and bool(self.token)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
            "Authorization": f"Bearer {self.token}",
        }

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        result = self.client.request(
            "GET",
            url,
            headers=self._headers(),
            params=params,
            timeout=(self.client.connect_timeout, self.client.read_timeout),
            max_bytes=4 * 1024 * 1024,
        )
        raw_remaining = result.headers.get("x-ratelimit-remaining")
        if raw_remaining is not None:
            try:
                self.rate_remaining = int(raw_remaining)
            except ValueError:
                pass
        if not result.ok:
            LOGGER.warning(
                "github api call failed",
                extra={"stage": "discovery", "check": path, "error": result.error_code or "", "url": url},
            )
            return None
        try:
            payload = result.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def _budget_ok(self) -> bool:
        if self.rate_remaining is None:
            return True
        return self.rate_remaining > self.min_remaining

    def _path_allowed(self, path: str) -> bool:
        lowered = path.lower()
        if any(pattern.search(lowered) for pattern in self.path_excludes):
            return False
        return any(pattern.search(path) for pattern in self.path_patterns)

    def _default_branch(self, full_name: str) -> str:
        if full_name in self._branches:
            return self._branches[full_name]
        branch = "HEAD"
        payload = self._get(f"/repos/{full_name}")
        if payload and isinstance(payload.get("default_branch"), str):
            branch = payload["default_branch"]
        self._branches[full_name] = branch
        return branch

    @staticmethod
    def _raw_url(full_name: str, branch: str, path: str) -> str:
        return f"https://raw.githubusercontent.com/{full_name}/{branch}/{path}"

    # -- adapters ----------------------------------------------------------
    def discover(self) -> list[Candidate]:
        if not self.enabled:
            return []
        if not self.token:
            LOGGER.warning(
                "github adapter disabled: no GH_TOKEN/GITHUB_TOKEN in the environment",
                extra={"stage": "discovery", "check": "github_auth"},
            )
            return []

        found: dict[str, Candidate] = {}
        for candidate in self._search_code():
            found.setdefault(normalize_url(candidate.url), candidate)
        if self._budget_ok():
            for candidate in self._search_repos():
                found.setdefault(normalize_url(candidate.url), candidate)
        return [item for item in found.values() if item.url]

    def _search_code(self) -> Iterable[Candidate]:
        for query in self.code_queries:
            self.stats["code_queries"] += 1
            for page in range(1, self.max_pages + 1):
                if not self._budget_ok():
                    LOGGER.info("github rate budget low, stopping code search",
                                extra={"stage": "discovery", "check": "code_search", "remaining": self.rate_remaining})
                    return
                payload = self._get(
                    "/search/code",
                    params={"q": query, "per_page": self.per_page, "page": page},
                )
                if not payload:
                    break
                items = payload.get("items") or []
                if not isinstance(items, list) or not items:
                    break
                for item in items:
                    candidate = self._candidate_from_code_item(item, query)
                    if candidate:
                        self.stats["files"] += 1
                        yield candidate
                if len(items) < self.per_page:
                    break

    def _candidate_from_code_item(self, item: Any, query: str) -> Candidate | None:
        if not isinstance(item, dict):
            return None
        path = str(item.get("path") or "")
        repository = item.get("repository") or {}
        full_name = str(repository.get("full_name") or "")
        if not full_name or not path or not self._path_allowed(path):
            self.stats["skipped"] += 1
            return None
        branch = self._default_branch(full_name)
        return Candidate(
            url=self._raw_url(full_name, branch, path),
            adapter=self.name,
            query=query,
            name=path.rsplit("/", 1)[-1],
            extra={"repo": full_name, "path": path},
        )

    def _search_repos(self) -> Iterable[Candidate]:
        seen_repos: list[str] = []
        for query in self.repo_queries:
            self.stats["repo_queries"] += 1
            payload = self._get("/search/repositories", params={"q": query, "per_page": self.per_page})
            if not payload:
                continue
            for item in payload.get("items") or []:
                if isinstance(item, dict) and item.get("full_name"):
                    seen_repos.append(str(item["full_name"]))

        for full_name in seen_repos[: self.max_repos_for_trees]:
            if not self._budget_ok():
                return
            branch = self._default_branch(full_name)
            if not self._budget_ok():
                return
            payload = self._get(
                f"/repos/{quote(full_name)}/git/trees/{quote(branch)}",
                params={"recursive": "1"},
            )
            if not payload:
                continue
            for entry in payload.get("tree") or []:
                if not isinstance(entry, dict) or entry.get("type") != "blob":
                    continue
                path = str(entry.get("path") or "")
                if not self._path_allowed(path):
                    continue
                self.stats["files"] += 1
                yield Candidate(
                    url=self._raw_url(full_name, branch, path),
                    adapter=self.name,
                    query=f"tree:{full_name}",
                    name=path.rsplit("/", 1)[-1],
                    extra={"repo": full_name, "path": path},
                )
