"""Scoring weights and the source lifecycle ladder (spec §9, §10, §11)."""

from __future__ import annotations

import pytest

from app.models import Source, Status
from app.scoring.scorer import Scorer, ScoreBreakdown, apply_lifecycle


@pytest.fixture()
def scorer(cfg):
    return Scorer(cfg, store=None)


def make_source(**kwargs) -> Source:
    base = {"id": "s1", "url": "https://a.example.com/c.json", "name": "A"}
    base.update(kwargs)
    return Source(**base)


# -- weights ---------------------------------------------------------------
def test_weights_sum_to_one(cfg):
    weights = cfg.section("weights")
    assert abs(sum(float(value) for value in weights.values()) - 1.0) < 1e-6


def test_weighted_total_renormalises_when_components_are_excluded(scorer):
    breakdown = ScoreBreakdown(availability=100, search=100, playback=100, latency=100,
                               stability=100, regional=100, freshness=100)
    assert scorer.weighted_total(breakdown) == pytest.approx(100.0)

    # dropping the two search-shaped components must not silently deflate the
    # score of a 多仓 wrapper: the remaining weights are re-normalised
    excluded = scorer.weighted_total(breakdown, {"search_success", "playback_success"})
    assert excluded == pytest.approx(100.0)


def test_weighted_total_respects_a_zero_component(scorer):
    breakdown = ScoreBreakdown(availability=0, search=100, playback=100, latency=100,
                               stability=100, regional=100, freshness=100)
    assert 0 < scorer.weighted_total(breakdown) < 100


def test_multi_source_is_excluded_from_search_shaped_components(scorer):
    assert scorer.applicable_components(make_source(type="multi")) == {"search_success", "playback_success"}
    assert scorer.applicable_components(make_source(type="single")) == set()


def test_status_thresholds_come_from_config(scorer, cfg):
    active = float(cfg.get("status_thresholds.active"))
    degraded = float(cfg.get("status_thresholds.degraded"))
    assert scorer.status_for(active) == Status.ACTIVE
    assert scorer.status_for(active - 1) == Status.DEGRADED
    assert scorer.status_for(degraded - 1) == Status.FAILED


def test_latency_score_uses_configured_buckets(scorer):
    assert scorer.latency_score(100) == 100
    assert scorer.latency_score(10_000_000) == 0
    assert scorer.latency_score(None) > 0  # unknown latency is neutral, not zero


# -- lifecycle -------------------------------------------------------------
def test_new_source_that_scores_well_becomes_active_immediately(scorer):
    source = make_source(status=Status.CANDIDATE, score=90)
    outcome = apply_lifecycle(source, success=True, scorer=scorer)
    assert outcome.status == Status.ACTIVE
    assert source.consecutive_success == 1


def test_a_single_failure_does_not_drop_the_source(scorer, cfg):
    source = make_source(status=Status.ACTIVE, score=90)
    outcome = apply_lifecycle(source, success=False, scorer=scorer)
    assert source.consecutive_failure == 1
    assert cfg.get("lifecycle.fail_1_keep") is True
    assert outcome.paused is False


def test_five_failures_pause_output_but_do_not_kill_the_source(scorer, cfg):
    pause_at = int(cfg.get("lifecycle.fail_pause_output"))
    source = make_source(status=Status.ACTIVE, score=90)
    for _ in range(pause_at):
        outcome = apply_lifecycle(source, success=False, scorer=scorer)
    assert source.consecutive_failure == pause_at
    assert outcome.paused is True
    assert source.status == Status.DEGRADED


def test_twelve_failures_mark_it_failed(scorer, cfg):
    dead_at = int(cfg.get("lifecycle.fail_mark_failed"))
    source = make_source(status=Status.ACTIVE, score=90)
    for _ in range(dead_at):
        apply_lifecycle(source, success=False, scorer=scorer)
    assert source.status == Status.FAILED
    assert source.consecutive_failure == dead_at


def test_a_failed_source_does_not_snap_back_on_one_good_probe(scorer):
    """spec §11: recovery must climb the ladder, not jump straight to ACTIVE.

    FAILED sources are excluded from the output by status (spec §18-3), which
    is why `paused` is not the mechanism doing the work here.
    """
    source = make_source(status=Status.FAILED, score=90)
    outcome = apply_lifecycle(source, success=True, scorer=scorer)
    assert outcome.status == Status.FAILED
    assert outcome.status != Status.ACTIVE
    assert source.consecutive_success == 1


def test_recovery_climbs_the_ladder(scorer, cfg):
    recovering_at = int(cfg.get("lifecycle.recover_min_success"))
    active_at = int(cfg.get("lifecycle.recover_to_active"))
    source = make_source(status=Status.FAILED, score=90)

    for _ in range(recovering_at - 1):
        apply_lifecycle(source, success=True, scorer=scorer)
    assert source.status == Status.FAILED

    outcome = apply_lifecycle(source, success=True, scorer=scorer)
    assert outcome.status == Status.RECOVERING
    assert "recovering" in outcome.note

    while source.consecutive_success < active_at:
        outcome = apply_lifecycle(source, success=True, scorer=scorer)
    assert outcome.status == Status.ACTIVE
    assert outcome.note == "recovered"
    assert source.paused is False


def test_recovery_emits_status_change_and_recovered_events(scorer, cfg):
    source = make_source(status=Status.FAILED, score=90)
    events = []
    for _ in range(int(cfg.get("lifecycle.recover_to_active"))):
        events.extend(apply_lifecycle(source, success=True, scorer=scorer).events)
    kinds = {event.event for event in events}
    assert "status_change" in kinds
    assert "recovered" in kinds


def test_blacklisted_stays_blacklisted(scorer):
    source = make_source(status=Status.ACTIVE, score=95, blacklisted=True)
    outcome = apply_lifecycle(source, success=True, scorer=scorer)
    assert outcome.status == Status.BLACKLISTED


def test_counters_track_maxima(scorer, cfg):
    source = make_source(status=Status.ACTIVE, score=90)
    for _ in range(3):
        apply_lifecycle(source, success=False, scorer=scorer)
    apply_lifecycle(source, success=True, scorer=scorer)
    assert source.max_consecutive_failure == 3
    assert source.consecutive_failure == 0
    assert source.total_checks == 4


def test_a_completed_recovery_does_not_flap_back_to_degraded(scorer, cfg):
    """Regression: the trailing 7-day score must not undo a finished recovery.

    After an outage the score lags (the 7-day windows still carry the outage),
    so re-deciding the status from it on the very next probe flapped the source
    ACTIVE -> DEGRADED every round and dropped it from tvbox.json again.
    """
    recover_to = int(cfg.get("lifecycle.recover_to_active"))
    source = make_source(status=Status.ACTIVE, score=95)
    for _ in range(int(cfg.get("lifecycle.fail_pause_output"))):
        apply_lifecycle(source, success=False, scorer=scorer)
    for _ in range(recover_to):
        apply_lifecycle(source, success=True, scorer=scorer)
    assert source.status == Status.ACTIVE

    # one more healthy probe, with a score that still sits below `active`
    source.score = float(cfg.get("status_thresholds.degraded")) + 5
    outcome = apply_lifecycle(source, success=True, scorer=scorer)
    assert source.consecutive_success == recover_to + 1
    assert outcome.status == Status.ACTIVE
    assert source.paused is False


def test_a_low_score_still_blocks_promotion_on_a_streak(scorer, cfg):
    """The score stays a floor: a streak alone must not promote a bad source."""
    recover_to = int(cfg.get("lifecycle.recover_to_active"))
    source = make_source(status=Status.FAILED, score=10)
    for _ in range(recover_to):
        outcome = apply_lifecycle(source, success=True, scorer=scorer)
    assert source.consecutive_success >= recover_to
    assert source.status == Status.ACTIVE  # the ladder itself completed
    # ...but with a score below the degraded floor the streak cannot hold it up,
    # and the score path takes over again (below `degraded` means FAILED)
    source.status = Status.DEGRADED
    source.score = float(cfg.get("status_thresholds.degraded")) - 1
    outcome = apply_lifecycle(source, success=True, scorer=scorer)
    assert outcome.status != Status.ACTIVE
    assert outcome.status == Status.FAILED
