"""Historical stability (spec §10).

``stability = 50% * 30d + 30% * 7d + 20% * recent`` - but windows with no
samples are *dropped and the weights re-normalised* rather than counted as 0.
Treating "no history yet" as "0% available" would permanently doom every new
source, which is the opposite of what §10 asks for.
"""

from __future__ import annotations

from typing import Callable, Iterable

NEUTRAL = 50.0


def windowed_stability(
    windows: Iterable[dict],
    availability_lookup: Callable[[float], float | None],
) -> tuple[float, dict[str, float | None]]:
    """Return ``(score, per_window_detail)``; missing windows are excluded."""
    detail: dict[str, float | None] = {}
    weighted = 0.0
    total_weight = 0.0

    for entry in windows or ():
        name = str(entry.get("name") or "")
        days = float(entry.get("days", 0) or 0)
        weight = float(entry.get("weight", 0) or 0)
        if weight <= 0 or days <= 0:
            continue
        value = availability_lookup(days)
        detail[name] = value
        if value is None:
            continue
        weighted += value * weight
        total_weight += weight

    if total_weight <= 0:
        return NEUTRAL, detail
    return round(weighted / total_weight, 3), detail


def availability_from_samples(successes: int, checks: int) -> float | None:
    if checks <= 0:
        return None
    return round(100.0 * successes / checks, 3)


def with_min_samples(values: dict[str, float | None], minimum: int, counts: dict[str, int]) -> dict[str, float | None]:
    """Blank out windows that do not meet a minimum sample count."""
    out: dict[str, float | None] = {}
    for name, value in values.items():
        out[name] = value if counts.get(name, 0) >= minimum else None
    return out


def longest_runs(sequence: Iterable[bool]) -> tuple[int, int]:
    """``(longest success run, longest failure run)`` over a bool sequence."""
    best_ok = best_fail = current_ok = current_fail = 0
    for value in sequence:
        if value:
            current_ok += 1
            current_fail = 0
        else:
            current_fail += 1
            current_ok = 0
        best_ok = max(best_ok, current_ok)
        best_fail = max(best_fail, current_fail)
    return best_ok, best_fail
