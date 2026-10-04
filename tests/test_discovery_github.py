"""GitHub discovery: only recent, non-abandoned repos are expanded (5.1.A)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from app.discovery.github import GitHubAdapter
from app.utils.http_client import HttpResult


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _adapter(cfg) -> GitHubAdapter:
    return GitHubAdapter(cfg, client=None)


def test_recent_repo_with_star_is_expanded(cfg):
    assert _adapter(cfg)._repo_is_worth_expanding(
        {"full_name": "a/b", "stargazers_count": 12, "pushed_at": _iso(3)}
    )


def test_stale_repo_is_skipped(cfg):
    assert not _adapter(cfg)._repo_is_worth_expanding(
        {"full_name": "a/b", "stargazers_count": 50, "pushed_at": _iso(400)}
    )


def test_zero_star_backup_is_skipped(cfg):
    assert not _adapter(cfg)._repo_is_worth_expanding(
        {"full_name": "a/b", "stargazers_count": 0, "pushed_at": _iso(1)}
    )


def test_missing_pushed_at_is_skipped_when_age_filter_is_on(cfg):
    assert not _adapter(cfg)._repo_is_worth_expanding(
        {"full_name": "a/b", "stargazers_count": 3}
    )


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.connect_timeout = 1
        self.read_timeout = 1

    def request(self, *args, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def _rate_limited(resource: str = "code_search") -> HttpResult:
    return HttpResult(
        url="https://api.github.com/search/code",
        ok=False,
        status=403,
        headers={
            "x-ratelimit-resource": resource,
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(int(time.time()) + 3),
        },
    )


def _ok(payload: dict, resource: str = "code_search") -> HttpResult:
    result = HttpResult(
        url="https://api.github.com/search/code",
        ok=True,
        status=200,
        headers={
            "x-ratelimit-resource": resource,
            "x-ratelimit-remaining": "7",
            "content-type": "application/json",
        },
    )
    result.content = json.dumps(payload).encode()
    return result


def test_code_search_waits_out_the_rate_limit_and_retries(cfg):
    client = _FakeClient([_rate_limited(), _ok({"items": []})])
    adapter = GitHubAdapter(cfg, client=client)
    adapter._sleep = lambda seconds: None

    payload = adapter._get(
        "/search/code", resource="code_search", retry_on_rate_limit=True
    )

    assert payload == {"items": []}
    assert client.calls == 2


def test_code_search_is_paced_between_calls(cfg):
    adapter = GitHubAdapter(cfg, client=_FakeClient([]))
    slept: list[float] = []
    adapter._sleep = slept.append
    adapter.code_search_interval = 0.5

    adapter._throttle("code_search", 0.5)
    adapter._throttle("code_search", 0.5)

    assert slept and slept[0] > 0


def test_plain_api_failure_is_not_retried(cfg):
    client = _FakeClient([HttpResult(url="u", ok=False, status=500, headers={})])
    adapter = GitHubAdapter(cfg, client=client)

    assert adapter._get("/search/code", retry_on_rate_limit=True) is None
    assert client.calls == 1


def test_seed_repos_are_enumerated_without_any_search(cfg):
    client = _FakeClient(
        [
            _ok({"default_branch": "main"}, resource="core"),
            _ok(
                {
                    "tree": [
                        {"type": "blob", "path": "api/data.json"},
                        {"type": "blob", "path": "README.md"},
                        {"type": "tree", "path": "sub"},
                    ]
                },
                resource="core",
            ),
        ]
    )
    adapter = GitHubAdapter(cfg, client=client)
    adapter.token = "t"
    adapter.seed_repos = ["o/r"]
    adapter.code_queries = []
    adapter.repo_queries = []

    out = list(adapter.discover())

    assert [candidate.url for candidate in out] == [
        "https://raw.githubusercontent.com/o/r/main/api/data.json"
    ]
    assert out[0].query == "seed:o/r"
    assert adapter.stats["seed_files"] == 1


def test_seed_repos_include_image_suffixed_config_paths(cfg):
    client = _FakeClient(
        [
            _ok({"default_branch": "main"}, resource="core"),
            _ok(
                {
                    "tree": [
                        {"type": "blob", "path": "哈基米.png"},
                        {"type": "blob", "path": "notes.txt"},
                    ]
                },
                resource="core",
            ),
        ]
    )
    adapter = GitHubAdapter(cfg, client=client)
    adapter.token = "t"
    adapter.seed_repos = ["o/r"]

    paths = [candidate.extra["path"] for candidate in adapter._seed_candidates()]

    assert paths == ["哈基米.png"]


def test_seed_repos_stop_when_core_budget_is_low(cfg):
    adapter = GitHubAdapter(cfg, client=_FakeClient([]))
    adapter.seed_repos = ["o/r"]
    adapter._remaining["core"] = 1
    adapter.min_remaining = 5

    assert list(adapter._seed_candidates()) == []
    assert adapter.stats["seed_repos"] == 1


def test_seed_repos_respect_per_repo_cap(cfg):
    client = _FakeClient(
        [
            _ok({"default_branch": "main"}, resource="core"),
            _ok(
                {"tree": [{"type": "blob", "path": f"a{i}.json"} for i in range(10)]},
                resource="core",
            ),
        ]
    )
    adapter = GitHubAdapter(cfg, client=client)
    adapter.token = "t"
    adapter.seed_repos = ["o/r"]
    adapter.seed_max_per_repo = 3

    paths = [candidate.extra["path"] for candidate in adapter._seed_candidates()]

    assert paths == ["a0.json", "a1.json", "a2.json"]


def test_seed_repos_prioritise_shallow_paths(cfg):
    client = _FakeClient(
        [
            _ok({"default_branch": "main"}, resource="core"),
            _ok(
                {
                    "tree": [
                        {"type": "blob", "path": "deep/dir/config.json"},
                        {"type": "blob", "path": "root.json"},
                    ]
                },
                resource="core",
            ),
        ]
    )
    adapter = GitHubAdapter(cfg, client=client)
    adapter.token = "t"
    adapter.seed_repos = ["o/r"]
    adapter.seed_max_per_repo = 1

    paths = [candidate.extra["path"] for candidate in adapter._seed_candidates()]

    assert paths == ["root.json"]


def test_seed_repos_rotate_between_runs(cfg):
    adapter = GitHubAdapter(cfg, client=_FakeClient([]))
    adapter.seed_repos = [f"o/r{i}" for i in range(10)]
    adapter.seed_repos_per_run = 3

    adapter.seed_rotation_offset = 0
    first = adapter._seeds_for_run()
    adapter.seed_rotation_offset = 3
    second = adapter._seeds_for_run()
    adapter.seed_rotation_offset = 9
    wrapped = adapter._seeds_for_run()

    assert first == ["o/r0", "o/r1", "o/r2"]
    assert second == ["o/r3", "o/r4", "o/r5"]
    assert wrapped == ["o/r9", "o/r0", "o/r1"]


def test_seed_repos_run_all_when_fewer_than_per_run(cfg):
    adapter = GitHubAdapter(cfg, client=_FakeClient([]))
    adapter.seed_repos = ["o/a", "o/b"]
    adapter.seed_repos_per_run = 5

    assert adapter._seeds_for_run() == ["o/a", "o/b"]
