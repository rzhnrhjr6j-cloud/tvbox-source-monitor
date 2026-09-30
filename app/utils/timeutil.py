"""Time helpers - everything in the project is UTC and ISO-8601."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime | None = None) -> str:
    moment = moment or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(ISO_FORMAT)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_seconds(value: str | None) -> float | None:
    moment = parse_iso(value)
    if moment is None:
        return None
    return max(0.0, (utcnow() - moment).total_seconds())


def age_days(value: str | None) -> float | None:
    seconds = age_seconds(value)
    return None if seconds is None else seconds / 86400.0


def day_key(moment: datetime | None = None) -> str:
    return to_iso(moment)[:10]


def days_ago(days: float) -> str:
    return to_iso(utcnow() - timedelta(days=days))
