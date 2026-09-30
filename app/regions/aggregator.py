"""Fan out probes to every region and merge the answers (spec §12, §14).

The GitHub runner itself always contributes the local region (mechanism A);
remote probe nodes add the rest (mechanism B).  A dead node is recorded and
tolerated up to ``node_failure_tolerance`` before a node-down alert is raised.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from ..logging_setup import get_logger
from ..models import ProbeResult, Source
from .node_client import NodeClient, NodeConfig, NodeOutcome

LOGGER = get_logger("regions.aggregator")


@dataclass
class RegionCollection:
    probes: dict[str, list[ProbeResult]] = field(default_factory=dict)
    node_outcomes: list[NodeOutcome] = field(default_factory=list)
    local_probe: ProbeResult | None = None
    tolerated_failures: int = 0

    def add(self, probe: ProbeResult) -> None:
        self.probes.setdefault(probe.region or "?", []).append(probe)

    @property
    def failed_nodes(self) -> list[NodeOutcome]:
        return [outcome for outcome in self.node_outcomes if not outcome.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "regions": {region: len(items) for region, items in sorted(self.probes.items())},
            "nodes": [outcome.to_dict() for outcome in self.node_outcomes],
            "failed_nodes": len(self.failed_nodes),
        }


class RegionAggregator:
    def __init__(
        self,
        cfg,
        http_client,
        *,
        local_region: str = "CN",
        local_node: str = "github-runner",
        tolerant_failures: int = 1,
        max_workers: int = 6,
    ):
        self.cfg = cfg
        self.http_client = http_client
        self.local_region = local_region
        self.local_node = local_node
        self.tolerant_failures = tolerant_failures
        self.max_workers = max_workers
        self.node_client = NodeClient(http_client)
        self.nodes: list[NodeConfig] = [n for n in load_nodes_cached(cfg) if n.enabled]

    def collect(
        self,
        source: Source,
        run_local: Callable[[str, str], ProbeResult],
        options: dict[str, Any] | None = None,
    ) -> RegionCollection:
        collection = RegionCollection()

        # ---- mechanism A: this runner --------------------------------
        try:
            local_probe = run_local(self.local_region, self.local_node)
        except Exception as exc:  # noqa: BLE001 - one bad source must not kill the run
            LOGGER.error(
                "local probe crashed",
                extra={"source_id": source.id, "region": self.local_region, "check": "local", "error": str(exc)[:200]},
            )
            local_probe = ProbeResult(source_id=source.id, region=self.local_region, node=self.local_node)
            local_probe.error_code = "PROBE_CRASH"
            local_probe.error_stage = "L0_runner"
            local_probe.error_message = str(exc)[:500]
        collection.local_probe = local_probe
        collection.add(local_probe)

        # ---- mechanism B: remote nodes -------------------------------
        if not self.nodes:
            return collection

        source_payload = {
            "id": source.id,
            "url": source.raw_url or source.url,
            "name": source.name,
            "type": source.type,
        }
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(self.nodes))) as pool:
            futures = {
                pool.submit(self.node_client.probe, node, source_payload, options or {}): node
                for node in self.nodes
            }
            for future in as_completed(futures):
                node = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001
                    outcome = NodeOutcome(node=node, error=f"NODE_CRASH: {exc}"[:200])
                collection.node_outcomes.append(outcome)
                if outcome.probe is not None:
                    collection.add(outcome.probe)
                else:
                    LOGGER.warning(
                        "probe node failed",
                        extra={
                            "source_id": source.id,
                            "region": node.region,
                            "node": node.name,
                            "check": "node",
                            "error": outcome.error,
                        },
                    )

        collection.tolerated_failures = max(0, len(collection.failed_nodes) - self.tolerant_failures)
        return collection


def load_nodes_cached(cfg):
    from .node_client import load_nodes

    return load_nodes(cfg)
