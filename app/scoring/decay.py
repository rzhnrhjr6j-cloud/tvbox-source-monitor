"""Freshness decay (spec §9 - the ``freshness`` term).

A source we last saw working a month ago is worth less than one confirmed five
minutes ago.  Exponential half-life decay keeps that continuous rather than
step-wise.
"""

from __future__ import annotations

from ..utils.timeutil import age_days

DEFAULT_HALF_LIFE_DAYS = 14.0


def freshness(last_success_at: str | None, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """1.0 at the moment of success, 0.5 after one half-life, never negative."""
    if half_life_days <= 0:
        return 0.0
    age = age_days(last_success_at)
    if age is None:
        return 0.0
    if age <= 0:
        return 100.0
    return round(100.0 * (0.5 ** (age / half_life_days)), 3)


def decay_curve(half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> list[dict[str, float]]:
    """Small helper for docs / dashboards: value at 0, 1, 7, 30, 90 days."""
    out = []
    for days in (0, 1, 7, 30, 90):
        out.append({
            "days": days,
            "value": round(100.0 * (0.5 ** (days / half_life_days)), 3) if half_life_days > 0 else 0.0,
        })
    return out
