"""GitHub discovery adapter (spec §5.1.A, §26).

Searches public repositories for config-shaped JSON files.  Rate-limit aware:
it paces itself per bucket (code search is 10 requests/minute, repository
search 30) and waits out a reset instead of dropping the queries that come
after the quota empties.  Token-authenticated and deliberately polite: bounded
pages, bounded repos, no scraping of anything that is not a plainly public
file path.

Requires a token.  Without one GitHub's code-search API refuses to answer, so
the adapter disables itself instead of hammering the unauthenticated endpoint.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote

from ..logging_setup import get_logger
from ..utils.urls import normalize_url
from .types import Candidate

LOGGER = get_logger("discovery.github")

_API_VERSION = "2022-11-28"

# GitHub caps code search at 10 requests/minute for token-authenticated users
# (``/rate_limit`` reports ``code_search`` separately from ``search``).  The
# query list is deliberately long, so firing it all at once used to burn the
# budget in the first few seconds and silently drop every later query.
_DEFAULT_SEARCH_RPM = 9.0


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
        raw_rpm = section.get("search_requests_per_minute", _DEFAULT_SEARCH_RPM)
        try:
            search_rpm = float(raw_rpm)
        except (TypeError, ValueError):
            search_rpm = _DEFAULT_SEARCH_RPM
        search_rpm = min(30.0, max(1.0, search_rpm))
        # Search endpoints use two buckets: code search is 10/min, repository
        # search 30/min.  Pace each one on its own interval.
        self.code_search_interval = 60.0 / min(search_rpm, 10.0)
        self.repo_search_interval = 60.0 / search_rpm
        self.max_search_wait_seconds = float(section.get("max_search_wait_seconds", 75))
        self.max_code_search_seconds = float(section.get("max_code_search_seconds", 780))
        self.path_patterns = [re.compile(item) for item in (section.get("path_patterns") or [r"\.json$"])]
        self.path_excludes = [re.compile(item) for item in (section.get("path_excludes") or [])]
        self.max_repos_for_trees = int(section.get("max_repos_for_trees", 5))
        # Repos we already know publish live configs.  Enumerating their trees
        # is far higher-yield than adding yet another search phrase, and it
        # costs only core API budget (5000/h), not the scarce code-search one.
        self.seed_repos = [
            str(item).strip() for item in (section.get("seed_repos") or []) if str(item).strip()
        ]
        # A single busy repo can contain thousands of JSON files; without a
        # per-repo cap it would eat the whole 300-candidate run budget and the
        # other seed repos would never be admitted.  0 means "no cap".
        self.seed_max_per_repo = int(section.get("seed_max_per_repo", 40))
        # How many seed repos one run enumerates.  With the 300-candidate run
        # budget, expanding every seed repo every run would starve the search
        # adapters and freeze on the same files forever, so they rotate.
        self.seed_repos_per_run = int(section.get("seed_repos_per_run", 5))
        # Tests pin the rotation; production leaves it None and uses the date.
        self.seed_rotation_offset: int | None = None
        self.min_stars = max(0, int(section.get("min_stars", 0)))
        raw_age = section.get("max_repo_age_days")
        self.max_repo_age_days = max(0, int(raw_age)) if raw_age not in (None, "") else 0
        self.client = client
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        self.rate_remaining: int | None = None
        self._remaining: dict[str, int] = {}
        self._reset: dict[str, int] = {}
        self._last_call: dict[str, float] = {}
        self._sleep = time.sleep
        self._branches: dict[str, str] = {}
        self.stats: dict[str, Any] = {
            "code_queries": 0,
            "repo_queries": 0,
            "files": 0,
            "skipped": 0,
            "skipped_repos": 0,
            "seed_repos": 0,
            "seed_files": 0,
        }

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

    def _record_rate_limit(self, headers: Any, *, default_resource: str) -> None:
        """Track each rate-limit bucket the API reports separately."""
        resource = str(headers.get("x-ratelimit-resource") or default_resource)
        raw_remaining = headers.get("x-ratelimit-remaining")
        if raw_remaining is not None:
            try:
                value = int(raw_remaining)
            except (TypeError, ValueError):
                value = None
            if value is not None:
                self._remaining[resource] = value
                self.rate_remaining = value
        raw_reset = headers.get("x-ratelimit-reset")
        if raw_reset is not None:
            try:
                self._reset[resource] = int(raw_reset)
            except (TypeError, ValueError):
                pass

    def _throttle(self, resource: str, interval: float) -> None:
        """Keep search calls under the bucket's per-minute quota."""
        if interval <= 0:
            return
        last = self._last_call.get(resource)
        if last is not None:
            wait = interval - (time.monotonic() - last)
            if wait > 0:
                self._sleep(min(wait, self.max_search_wait_seconds))
        self._last_call[resource] = time.monotonic()

    def _wait_until_reset(self, resource: str) -> float:
        reset = self._reset.get(resource)
        if reset is None:
            return 0.0
        wait = reset - time.time() + 1.0
        if wait <= 0:
            return 0.0
        return min(wait, self.max_search_wait_seconds)

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        resource: str = "core",
        interval: float = 0.0,
        retry_on_rate_limit: bool = False,
    ) -> dict[str, Any] | None:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        for attempt in range(2):
            if interval > 0:
                self._throttle(resource, interval)
            result = self.client.request(
                "GET",
                url,
                headers=self._headers(),
                params=params,
                timeout=(self.client.connect_timeout, self.client.read_timeout),
                max_bytes=4 * 1024 * 1024,
            )
            self._record_rate_limit(result.headers, default_resource=resource)
            if result.ok:
                try:
                    payload = result.json()
                except ValueError:
                    return None
                return payload if isinstance(payload, dict) else None
            remaining = self._remaining.get(resource)
            rate_limited = (
                (result.status or 0) in (403, 429)
                and remaining is not None
                and remaining <= 0
            )
            if retry_on_rate_limit and rate_limited and attempt == 0:
                waited = self._wait_until_reset(resource)
                if waited > 0:
                    LOGGER.info(
                        "github rate limit hit, waiting for reset",
                        extra={
                            "stage": "discovery",
                            "check": resource,
                            "wait_seconds": round(waited, 1),
                        },
                    )
                    self._sleep(waited)
                    continue
            LOGGER.warning(
                "github api call failed",
                extra={"stage": "discovery", "check": path, "error": result.error_code or "", "url": url},
            )
            return None
        return None

    def _budget_ok(self, resource: str = "core") -> bool:
        remaining = self._remaining.get(resource)
        if remaining is None:
            return True
        return remaining > self.min_remaining

    def _path_allowed(self, path: str) -> bool:
        lowered = path.lower()
        if any(pattern.search(lowered) for pattern in self.path_excludes):
            return False
        return any(pattern.search(path) for pattern in self.path_patterns)

    def _repo_is_worth_expanding(self, item: dict[str, Any]) -> bool:
        """Reject repos that are too quiet to contain live endpoints.

        A config from a repository nobody has pushed to in months is almost
        always a graveyard of dead APIs; the client reads those as
        "解析失败".  Missing metadata is treated permissively so a search that
        does not report a field never filters a real candidate out.
        """
        stars = item.get("stargazers_count")
        if isinstance(stars, int) and stars < self.min_stars:
            return False
        if self.max_repo_age_days > 0:
            pushed = str(item.get("pushed_at") or "")
            if not pushed:
                return False
            try:
                when = datetime.strptime(pushed, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                return True
            if datetime.now(timezone.utc) - when > timedelta(days=self.max_repo_age_days):
                return False
        return True

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
        # Seed repos go first: they carry real-device playback evidence, so
        # they must never be crowded out by the 300-candidate per-run cap or
        # by a search bucket that ran dry before reaching them.
        for candidate in self._seed_candidates():
            found.setdefault(normalize_url(candidate.url), candidate)
        for candidate in self._search_code():
            found.setdefault(normalize_url(candidate.url), candidate)
        if self._budget_ok("core"):
            for candidate in self._search_repos():
                found.setdefault(normalize_url(candidate.url), candidate)
        return [item for item in found.values() if item.url]

    def _search_code(self) -> Iterable[Candidate]:
        deadline = time.monotonic() + max(0.0, self.max_code_search_seconds)
        for query in self.code_queries:
            self.stats["code_queries"] += 1
            for page in range(1, self.max_pages + 1):
                if time.monotonic() >= deadline:
                    LOGGER.info(
                        "github code search time budget exhausted",
                        extra={"stage": "discovery", "check": "code_search", "queries_run": self.stats["code_queries"]},
                    )
                    return
                payload = self._get(
                    "/search/code",
                    params={"q": query, "per_page": self.per_page, "page": page},
                    resource="code_search",
                    interval=self.code_search_interval,
                    retry_on_rate_limit=True,
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
            payload = self._get(
                "/search/repositories",
                params={"q": query, "per_page": self.per_page},
                resource="search",
                interval=self.repo_search_interval,
                retry_on_rate_limit=True,
            )
            if not payload:
                continue
            for item in payload.get("items") or []:
                if not isinstance(item, dict) or not item.get("full_name"):
                    continue
                if not self._repo_is_worth_expanding(item):
                    self.stats["skipped_repos"] += 1
                    continue
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

    def _seed_candidates(self) -> Iterable[Candidate]:
        """Enumerate hand-picked repos that are known to publish live configs."""
        for full_name in self._seeds_for_run():
            self.stats["seed_repos"] += 1
            if not self._budget_ok():
                LOGGER.warning(
                    "github core budget low, stopping seed repo expansion",
                    extra={"stage": "discovery", "check": "seed_repos", "repo": full_name},
                )
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
            entries = [
                entry
                for entry in (payload.get("tree") or [])
                if isinstance(entry, dict)
                and entry.get("type") == "blob"
                and self._path_allowed(str(entry.get("path") or ""))
            ]
            # Shallow paths first: the repo-root config is the one a user
            # actually opens.  Deep backup/lib folders are still eligible but
            # cannot crowd out the root files.
            entries.sort(key=lambda entry: (str(entry["path"]).count("/"), str(entry["path"])))
            limit = self.seed_max_per_repo if self.seed_max_per_repo > 0 else len(entries)
            for entry in entries[:limit]:
                path = str(entry["path"])
                self.stats["files"] += 1
                self.stats["seed_files"] += 1
                yield Candidate(
                    url=self._raw_url(full_name, branch, path),
                    adapter=self.name,
                    query=f"seed:{full_name}",
                    name=path.rsplit("/", 1)[-1],
                    extra={"repo": full_name, "path": path},
                )

    def _seeds_for_run(self) -> list[str]:
        """Return the rotating slice of seed repos for this run."""
        repos = self.seed_repos
        if self.seed_repos_per_run <= 0 or len(repos) <= self.seed_repos_per_run:
            return list(repos)
        offset = self.seed_rotation_offset
        if offset is None:
            offset = datetime.now(timezone.utc).toordinal()
        offset %= len(repos)
        doubled = repos + repos
        return doubled[offset : offset + self.seed_repos_per_run]
