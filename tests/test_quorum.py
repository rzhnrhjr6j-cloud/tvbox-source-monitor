"""Multi-region quorum (spec §12, §13)."""

from __future__ import annotations

from app.models import GlobalStatus, ProbeResult
from app.regions.quorum import decide, probe_ok, regional_score


def good(region: str, node: str = "") -> ProbeResult:
    return ProbeResult(source_id="s", region=region, node=node, http_ok=True, config_parse_success=True)


def bad(region: str, node: str = "") -> ProbeResult:
    return ProbeResult(source_id="s", region=region, node=node, http_ok=False, config_parse_success=False)


def test_probe_ok_requires_http_and_config():
    assert probe_ok(good("CN")) is True
    assert probe_ok(ProbeResult(source_id="s", region="CN", http_ok=True, config_parse_success=False)) is False
    assert probe_ok(ProbeResult(source_id="s", region="CN", http_ok=False, config_parse_success=True)) is False


def test_all_regions_ok_is_global():
    verdict = decide({"CN": [good("CN")], "JP": [good("JP")]})
    assert verdict.global_status == GlobalStatus.GLOBAL
    assert sorted(verdict.regions_ok) == ["CN", "JP"]
    assert verdict.regions_failed == []
    assert verdict.node_count == 2
    assert regional_score(verdict) == 100.0


def test_one_region_down_is_regional():
    verdict = decide({"CN": [good("CN")], "JP": [bad("JP")]})
    assert verdict.global_status == GlobalStatus.REGIONAL
    assert verdict.regions_ok == ["CN"]
    assert verdict.regions_failed == ["JP"]
    assert 0 < regional_score(verdict) < 100


def test_all_regions_down_is_failed():
    verdict = decide({"CN": [bad("CN")], "JP": [bad("JP")]})
    assert verdict.global_status == GlobalStatus.FAILED
    assert regional_score(verdict) == 0.0


def test_single_node_cannot_reach_quorum():
    """spec §13: one reporting node must not be trusted as a global verdict."""
    verdict = decide({"CN": [good("CN")]})
    assert verdict.global_status == GlobalStatus.UNKNOWN
    assert verdict.node_count == 1


def test_quorum_threshold_is_configurable():
    probes = {"CN": [good("CN")], "JP": [good("JP")], "US": [bad("US")]}
    strict = decide(probes, global_success_ratio=0.9)
    lenient = decide(probes, global_success_ratio=0.5)
    assert strict.global_status == GlobalStatus.REGIONAL
    assert lenient.global_status == GlobalStatus.GLOBAL


def test_region_ratio_uses_majority_of_nodes_in_that_region():
    verdict = decide({"CN": [good("CN", "a"), good("CN", "b"), bad("CN", "c")]})
    assert verdict.regions_ok == ["CN"]
    assert verdict.detail["CN"]["successes"] == 2


def test_empty_input_is_unknown_not_crash():
    verdict = decide({})
    assert verdict.global_status == GlobalStatus.UNKNOWN
    assert verdict.node_count == 0
