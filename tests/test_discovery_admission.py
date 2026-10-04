"""Discovery admission: a real manual-pool name replaces the old placeholder."""

from __future__ import annotations

import pytest


class _Scorer:
    def candidate_score(self, signals):  # noqa: ANN001 - test stub
        return 100.0

    def candidate_threshold(self) -> float:
        return 0.0


def test_manual_placeholder_name_is_backfilled_on_reseen(scratch_cfg):
    from app.discovery.aggregator import DiscoveryAggregator
    from app.discovery.types import Candidate
    from app.models import DiscoveryRecord, Source, Status
    from app.policy import PolicySet
    from app.storage.sqlite import Store
    from app.utils.urls import parse_url

    store = Store(scratch_cfg.path("app.db_path", ensure_parent=True))
    try:
        url = "https://raw.githubusercontent.com/o/r/main/config.json"
        normalized = parse_url(url).normalized
        policy = PolicySet(scratch_cfg)
        source_id = policy.canonical_id(url)
        store.upsert_source(
            Source(id=source_id, url=normalized, raw_url=url, name="manual", status=Status.CANDIDATE)
        )
        store.record_discoveries(
            [
                DiscoveryRecord(
                    url=url,
                    normalized_url=normalized,
                    source_id=source_id,
                    adapter="manual",
                    query="candidates.json",
                    admitted=True,
                    reason="",
                )
            ]
        )

        aggregator = DiscoveryAggregator(scratch_cfg, None, store, _Scorer())
        outcome = aggregator.admit(
            Candidate(url=url, adapter="manual", query="candidates.json", name="真名")
        )

        assert outcome.reason == "KNOWN"
        assert store.get_source(source_id).name == "真名"
    finally:
        store.close()


# -- aggregate preference ---------------------------------------------------
def test_candidate_weights_sum_to_one(scratch_cfg):
    weights = scratch_cfg.section("candidate").get("weights") or {}
    assert abs(sum(float(value) for value in weights.values()) - 1.0) < 1e-9


def test_multi_site_config_outscores_a_single_site_config(scratch_cfg):
    from app.scoring.scorer import Scorer

    scorer = Scorer(scratch_cfg, store=None)
    base = {
        "reachable": True,
        "json_valid": True,
        "schema_valid": True,
        "https": True,
        "initial_search": True,
    }
    single = scorer.candidate_score(base)
    aggregate = scorer.candidate_score({**base, "aggregate_config": True})

    assert aggregate > single
    assert aggregate <= 100.0


def test_a_live_single_site_config_still_clears_the_threshold(scratch_cfg):
    from app.scoring.scorer import Scorer

    scorer = Scorer(scratch_cfg, store=None)
    score = scorer.candidate_score(
        {"reachable": True, "json_valid": True, "schema_valid": True, "initial_search": True}
    )

    assert score >= scorer.candidate_threshold()


@pytest.mark.parametrize(
    "schema,sites,children,expected",
    [
        ("single", [{"api": "https://a.example.com/api.php"}], [], False),
        (
            "single",
            [{"api": "https://a.example.com/api.php"}, {"api": "https://b.example.com/api.php"}],
            [],
            True,
        ),
        ("multi", [], [{"url": "https://a.example.com/config.json"}], True),
    ],
)
def test_aggregate_signal_flags_multi_site_and_multi_wrapper(
    scratch_cfg, store, schema, sites, children, expected
):
    from app.discovery.aggregator import DiscoveryAggregator
    from app.parsers.json_parser import ConfigDoc
    from app.scoring.scorer import Scorer

    aggregator = DiscoveryAggregator(scratch_cfg, None, store, Scorer(scratch_cfg, None))
    doc = ConfigDoc(schema=schema, raw={}, sites=sites, children=children)

    signals = aggregator._signals("https://a.example.com/config.json", doc, 1, True)

    assert signals["aggregate_config"] is expected
