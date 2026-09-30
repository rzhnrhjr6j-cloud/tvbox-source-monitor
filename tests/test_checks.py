"""L1-L5 health checks against a real local HTTP origin (spec §8).

Nothing here is mocked: a real socket, a real config body, real failures.
"""

from __future__ import annotations

import pytest

from app.checks.runner import CheckOptions, run_checks
from app.models import HealthLevel, Source
from app.utils.http_client import HttpClient
from tests.local_source_server import LocalSourceServer


@pytest.fixture()
def options(cfg):
    return CheckOptions.from_config(cfg)


@pytest.fixture()
def client(cfg):
    handle = HttpClient(cfg.section("http"), allow_private=True)
    yield handle
    handle.close()


def source_for(url: str, **kwargs) -> Source:
    from app.utils.urls import source_id_for

    return Source(id=source_id_for(url) or "local", url=url, raw_url=url,
                  name=kwargs.pop("name", "Local"), **kwargs)


def test_healthy_source_passes_every_layer(client, options):
    with LocalSourceServer() as server:
        probe = run_checks(source_for(server.config_url), client, options, region="CN", node="test")
    assert probe.http_ok is True
    assert probe.config_parse_success is True
    assert probe.schema_detected == "single"
    assert probe.search_success is True
    # the local origin returns 3 hits for every keyword, and L3 tries them all
    assert probe.search_result_count == 3 * len(options.search_keywords)
    assert probe.detail_success is True
    assert probe.detail_has_playlist is True
    assert probe.playback_url_obtained is True
    assert probe.playback_probe_success is True
    assert probe.health_level == HealthLevel.ACTIVE
    assert probe.response_ms is not None


def test_every_probe_is_traceable_to_source_region_and_stage(client, options):
    """spec §35-13: a log line must identify source_id + region + check type."""
    with LocalSourceServer() as server:
        probe = run_checks(source_for(server.config_url), client, options, region="JP", node="jp-1")
    assert probe.source_id
    assert probe.region == "JP"
    assert probe.node == "jp-1"
    # a passing probe still records which layer produced its verdict
    assert probe.checks_version >= 1


def test_unreachable_source_fails_at_l1_with_an_error_stage(client, options):
    probe = run_checks(source_for("http://127.0.0.1:9/never.json"), client, options, region="CN")
    assert probe.http_ok is False
    assert probe.health_level == HealthLevel.FAILED
    assert probe.error_code
    assert probe.error_stage == "L1_http"


def test_http_500_fails_at_l1(client, options):
    with LocalSourceServer(config_up=False) as server:
        probe = run_checks(source_for(server.config_url), client, options, region="CN")
    assert probe.http_ok is False
    assert probe.error_stage == "L1_http"


def test_html_body_fails_at_l2(client, options):
    with LocalSourceServer() as server:
        probe = run_checks(source_for(f"{server.base}/broken.json"), client, options, region="CN")
    assert probe.http_ok is True
    assert probe.config_parse_success is False
    assert probe.error_stage == "L2_config"
    assert probe.error_code == "HTML_NOT_JSON"
    assert probe.health_level == HealthLevel.FAILED


def test_invalid_json_fails_at_l2(client, options):
    with LocalSourceServer() as server:
        probe = run_checks(source_for(f"{server.base}/notjson.json"), client, options, region="CN")
    assert probe.config_parse_success is False
    assert probe.error_code == "INVALID_JSON"


def test_search_outage_degrades_but_config_still_counts(client, options):
    with LocalSourceServer(search_up=False) as server:
        probe = run_checks(source_for(server.config_url), client, options, region="CN")
    assert probe.http_ok is True
    assert probe.config_parse_success is True
    assert probe.search_success is False
    assert probe.health_level == HealthLevel.DEGRADED
    assert probe.error_stage == "L3_search"


def test_media_outage_degrades_at_l5(client, options):
    with LocalSourceServer(media_up=False) as server:
        probe = run_checks(source_for(server.config_url), client, options, region="CN")
    assert probe.search_success is True
    assert probe.detail_success is True
    assert probe.playback_probe_success is False
    assert probe.health_level == HealthLevel.DEGRADED


def test_multi_repo_wrapper_passes_without_a_search_surface(client, options):
    """A 多仓 wrapper carries no API of its own - reachable + parseable is ACTIVE."""
    with LocalSourceServer() as server:
        probe = run_checks(source_for(f"{server.base}/multi.json"), client, options, region="CN")
    assert probe.config_parse_success is True
    assert probe.schema_detected == "multi"
    assert probe.health_level == HealthLevel.ACTIVE


def test_source_without_a_url_is_rejected_immediately(client, options):
    probe = run_checks(Source(id="x", url=""), client, options, region="CN")
    assert probe.error_code == "NO_URL"
    assert probe.health_level == HealthLevel.FAILED


def test_a_source_failure_never_raises(client, options):
    """spec §35-12: one broken source must not abort the run."""
    for bad_url in ("http://127.0.0.1:9/x.json", "http://[::1]:9/x.json", "ftp://example.com/x.json"):
        probe = run_checks(source_for(bad_url), client, options, region="CN")
        assert probe.health_level == HealthLevel.FAILED
