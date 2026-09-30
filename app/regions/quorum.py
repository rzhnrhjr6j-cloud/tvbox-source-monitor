"""Regional quorum (spec §13).

"one region succeeded" is NOT "globally available".  A source is GLOBAL only
when enough *responding* regions succeeded, REGIONAL when only some did, and
FAILED when none did.  When fewer than ``min_nodes_for_quorum`` nodes reported,
the verdict is UNKNOWN rather than a confident wrong answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from ..models import GlobalStatus, ProbeResult


@dataclass
class RegionVerdict:
    global_status: str = GlobalStatus.UNKNOWN
    regions_ok: list[str] = field(default_factory=list)
    regions_failed: list[str] = field(default_factory=list)
    region_ratio: float = 0.0
    node_count: int = 0
    reporting_regions: int = 0
    detail: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "global_status": self.global_status,
            "regions_ok": list(self.regions_ok),
            "regions_failed": list(self.regions_failed),
            "region_ratio": round(self.region_ratio, 4),
            "node_count": self.node_count,
            "reporting_regions": self.reporting_regions,
            "detail": self.detail,
        }


def probe_ok(probe: ProbeResult) -> bool:
    """What counts as "this node saw the source working" (spec §13)."""
    return bool(probe.http_ok and probe.config_parse_success)


def decide(
    probes_by_region: Mapping[str, Iterable[ProbeResult]],
    *,
    global_success_ratio: float = 0.75,
    region_success_ratio: float = 0.5,
    min_nodes_for_quorum: int = 2,
) -> RegionVerdict:
    verdict = RegionVerdict()
    total_nodes = 0
    ok_regions: list[str] = []
    failed_regions: list[str] = []

    for region, probes in sorted(probes_by_region.items()):
        probes = list(probes)
        if not probes:
            continue
        successes = sum(1 for probe in probes if probe_ok(probe))
        ratio = successes / len(probes)
        total_nodes += len(probes)
        verdict.detail[region] = {
            "nodes": len(probes),
            "successes": successes,
            "ratio": round(ratio, 4),
        }
        (ok_regions if ratio >= region_success_ratio else failed_regions).append(region)

    verdict.regions_ok = ok_regions
    verdict.regions_failed = failed_regions
    verdict.node_count = total_nodes
    verdict.reporting_regions = len(ok_regions) + len(failed_regions)

    if total_nodes < max(1, min_nodes_for_quorum):
        verdict.global_status = GlobalStatus.UNKNOWN
        verdict.region_ratio = (len(ok_regions) / verdict.reporting_regions) if verdict.reporting_regions else 0.0
        return verdict

    verdict.region_ratio = len(ok_regions) / verdict.reporting_regions if verdict.reporting_regions else 0.0

    if not ok_regions:
        verdict.global_status = GlobalStatus.FAILED
    elif verdict.region_ratio >= global_success_ratio:
        verdict.global_status = GlobalStatus.GLOBAL
    else:
        verdict.global_status = GlobalStatus.REGIONAL
    return verdict


def regional_score(verdict: RegionVerdict) -> float:
    """0-100 consistency score handed to the scoring engine (spec §9)."""
    if verdict.global_status == GlobalStatus.GLOBAL:
        return 100.0
    if verdict.global_status == GlobalStatus.REGIONAL:
        return round(max(1.0, verdict.region_ratio * 100.0), 3)
    if verdict.global_status == GlobalStatus.FAILED:
        return 0.0
    # UNKNOWN: not enough observers - stay neutral instead of punishing
    return 50.0
