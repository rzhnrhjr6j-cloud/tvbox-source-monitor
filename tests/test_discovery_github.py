"""GitHub discovery: only recent, non-abandoned repos are expanded (5.1.A)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.discovery.github import GitHubAdapter


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
