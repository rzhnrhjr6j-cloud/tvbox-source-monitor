"""Discovery-time jar reachability precheck."""

from __future__ import annotations

from app.checks.jar_reachability import (
    check_jar_reachability,
    jar_references,
)
from app.utils.http_client import HttpResult


class _Client:
    connect_timeout = 3

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, int, int | None]] = []

    def probe_first_bytes(self, url, nbytes=1, **kwargs):
        self.calls.append((url, nbytes, kwargs.get("max_retries")))
        response = self.responses[url]
        if isinstance(response, HttpResult):
            return response
        return HttpResult(url=url, **response)


def _result(url: str, *, status: int = 200, content: bytes = b"PK\x03\x04jar"):
    return HttpResult(url=url, status=status, ok=status < 400, content=content)


def test_jar_references_collects_only_explicit_jar_files():
    payload = {
        "spider": "https://cdn.example/spider.jar;md5;abc",
        "sites": [
            {"api": "csp_A", "jar": "./relative.jar"},
            {"api": "https://cdn.example/other.jar$param"},
            {"api": "https://cdn.example/api.php"},
        ],
    }

    assert jar_references(payload) == [
        "./relative.jar",
        "https://cdn.example/other.jar",
        "https://cdn.example/spider.jar",
    ]


def test_all_dead_jars_reject_the_candidate():
    first = "https://dead.example/a.jar"
    second = "https://dead.example/b.jar"
    client = _Client(
        {
            first: _result(first, status=404, content=b""),
            second: _result(second, status=410, content=b""),
        }
    )

    outcome = check_jar_reachability(
        {"spider": first, "sites": [{"jar": second, "api": "csp_A"}]},
        client,
        timeout=2,
        max_probes=10,
    )

    assert outcome.rejected is True
    assert (outcome.dead, outcome.alive, outcome.unknown) == (2, 0, 0)
    assert all(call[2] == 0 for call in client.calls)


def test_one_live_jar_keeps_the_candidate():
    dead = "https://dead.example/a.jar"
    live = "https://live.example/b.jar"
    client = _Client(
        {
            dead: _result(dead, status=404, content=b""),
            live: _result(live),
        }
    )

    outcome = check_jar_reachability(
        {"spider": dead, "sites": [{"jar": live, "api": "csp_A"}]},
        client,
        timeout=2,
        max_probes=10,
    )

    assert outcome.rejected is False
    assert outcome.alive == 1


def test_timeout_is_unknown_and_does_not_reject():
    timed_out = "https://slow.example/a.jar"
    dead = "https://dead.example/b.jar"
    client = _Client(
        {
            timed_out: HttpResult(url=timed_out, error_code="TIMEOUT"),
            dead: _result(dead, status=404, content=b""),
        }
    )

    outcome = check_jar_reachability(
        {"spider": timed_out, "sites": [{"jar": dead, "api": "csp_A"}]},
        client,
        timeout=2,
        max_probes=10,
    )

    assert outcome.rejected is False
    assert outcome.unknown == 1


def test_html_soft_404_counts_as_dead():
    url = "https://soft.example/spider.jar"
    client = _Client({url: _result(url, content=b"<!doctype html><html>gone")})

    outcome = check_jar_reachability({"spider": url}, client, timeout=2, max_probes=10)

    assert outcome.rejected is True
    assert outcome.dead == 1


def test_proxied_github_blob_jar_uses_verifier_mirror_fallbacks():
    proxied = (
        "https://mirror.ghproxy.com/https://github.com/o/r/blob/main/spider.jar"
        ";md5;abc"
    )
    origin = "https://github.com/o/r/blob/main/spider.jar"
    mirrored = f"https://ghfast.top/{origin}"
    client = _Client(
        {
            origin: _result(origin, content=b"<!doctype html><html>not a jar"),
            mirrored: _result(mirrored),
        }
    )

    outcome = check_jar_reachability({"spider": proxied}, client, timeout=2, max_probes=10)

    assert outcome.rejected is False
    assert outcome.alive == 1
    assert [call[0] for call in client.calls] == [origin, mirrored]


def test_probe_cache_is_reused_between_candidates():
    url = "https://cache.example/spider.jar"
    client = _Client({url: _result(url)})
    cache: dict[str, str] = {}

    first = check_jar_reachability({"spider": url}, client, timeout=2, max_probes=10, cache=cache)
    second = check_jar_reachability({"spider": url}, client, timeout=2, max_probes=10, cache=cache)

    assert first.alive == 1
    assert second.alive == 1
    assert len(client.calls) == 1
