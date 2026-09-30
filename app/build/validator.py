"""Output validation + the build safety valve (spec §19, §30).

The single most important guarantee in this project: a bad detection run must
never empty ``tvbox.json``.  Every candidate output is validated *before* it is
allowed to replace the published file, and a failure keeps the previous file
byte-for-byte.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlsplit

from ..utils.urls import normalize_url

REQUIRED_ITEM_FIELDS = ("name", "url")


@dataclass
class ValidationResult:
    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    previous_count: int | None = None
    new_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "checks": self.checks,
            "previous_count": self.previous_count,
            "new_count": self.new_count,
        }


def _items_of(output: Any) -> list[dict[str, Any]]:
    if isinstance(output, list):
        return [item for item in output if isinstance(item, dict)]
    if not isinstance(output, dict):
        return []
    for key in ("urls", "sites", "list", "lives"):
        value = output.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def count_items(output: Any) -> int:
    return len(_items_of(output))


def validate_output(
    output: Any,
    *,
    previous: Any | None = None,
    min_sources: int = 3,
    max_drop_ratio: float = 0.70,
    required_fields: Iterable[str] = REQUIRED_ITEM_FIELDS,
) -> ValidationResult:
    result = ValidationResult()

    def fail(reason: str) -> None:
        result.ok = False
        result.reasons.append(reason)

    # ---- structural ------------------------------------------------------
    items = _items_of(output)
    result.new_count = len(items)
    if not items:
        fail("NO_ITEMS")

    if result.new_count < int(min_sources):
        fail(f"BELOW_MIN_SOURCES:{result.new_count}<{min_sources}")

    seen: set[str] = set()
    for index, item in enumerate(items):
        for field_name in required_fields:
            value = item.get(field_name)
            if field_name not in item or value in (None, ""):
                fail(f"ITEM_{index}_MISSING_{field_name.upper()}")
        url = str(item.get("url") or item.get("api") or "")
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                fail(f"ITEM_{index}_NOT_HTTP")
            key = normalize_url(url)
            if key in seen:
                fail(f"DUPLICATE_URL:{key[:80]}")
            seen.add(key)

    try:
        json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError):
        fail("NOT_JSON_SERIALISABLE")

    # ---- comparison against the published file ---------------------------
    if previous is not None:
        previous_count = count_items(previous)
        result.previous_count = previous_count
        if previous_count > 0:
            drop_ratio = 1.0 - (result.new_count / previous_count)
            result.checks["drop_ratio"] = round(drop_ratio, 4)
            if result.new_count < previous_count and drop_ratio > float(max_drop_ratio):
                fail(
                    f"ACTIVE_DROP_{drop_ratio:.0%}>{float(max_drop_ratio):.0%}"
                    f" ({previous_count} -> {result.new_count})"
                )

    result.checks.update({
        "items": result.new_count,
        "min_sources": int(min_sources),
        "max_drop_ratio": float(max_drop_ratio),
    })
    return result


def is_acceptable(result: ValidationResult) -> bool:
    return bool(result.ok)


def summarize(result: ValidationResult) -> str:
    if result.ok:
        return f"ok ({result.new_count} items)"
    return "blocked: " + "; ".join(result.reasons)
