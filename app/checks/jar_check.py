"""L6 - Android jar verification (spec §8, T-005 follow-up).

L1-L5 run on a Linux runner and only ever see HTTP.  A TVBox client
additionally has to DexClassLoader every ``csp_*`` / jar site and run it on
Android; when that fails the client says "jar解析失败" and the source is dead
before it ever lists a video - exactly the failure L1-L5 cannot see.

That step needs a real device, so it is recorded locally by
``tools/android_verify.py --record`` into ``data/android_verified.json`` and
read back here.  The gate is off by default because a CI runner has no
emulator; a maintainer turns it on for the machine that publishes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils.timeutil import age_days

JAR_STAGE = "L6_android"
EVIDENCE_FILE = "android_verified.json"

_JAR_SUFFIX = ".jar"


def _is_jar_reference(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().endswith(_JAR_SUFFIX)


def has_jar_sites(payload: Any) -> bool:
    """True when judging this config needs Android code execution.

    Two shapes qualify: a top-level ``spider`` that is a jar, and any site
    whose ``api`` is a ``csp_`` key (a DexClassLoader spider) or that names a
    jar through ``api`` / ``ext`` / ``jar``.
    """
    if not isinstance(payload, dict):
        return False
    if _is_jar_reference(payload.get("spider")):
        return True
    for site in payload.get("sites") or []:
        if not isinstance(site, dict):
            continue
        api = site.get("api")
        if isinstance(api, str) and api.strip().lower().startswith("csp_"):
            return True
        for key in ("api", "ext", "jar"):
            if _is_jar_reference(site.get(key)):
                return True
    return False


@dataclass
class AndroidRecord:
    source_id: str
    config_url: str = ""
    verified_at: str = ""
    site_count: int = 0
    loadable_count: int = 0
    playable_count: int = 0
    playable_keys: list[str] = field(default_factory=list)

    @property
    def loaded(self) -> bool:
        return self.loadable_count > 0

    @property
    def playable(self) -> bool:
        return self.playable_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "config_url": self.config_url,
            "verified_at": self.verified_at,
            "site_count": self.site_count,
            "loadable_count": self.loadable_count,
            "playable_count": self.playable_count,
            "playable_keys": list(self.playable_keys),
        }


def load_evidence(path: Path | str) -> dict[str, AndroidRecord]:
    """Read the committed verdict file; a missing or broken file means none."""
    file = Path(path)
    if not file.is_file():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    records = data.get("verified") if isinstance(data, dict) else data
    out: dict[str, AndroidRecord] = {}
    for item in records or []:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id") or "").strip()
        if not source_id:
            continue
        out[source_id] = AndroidRecord(
            source_id=source_id,
            config_url=str(item.get("config_url") or ""),
            verified_at=str(item.get("verified_at") or ""),
            site_count=int(item.get("site_count") or 0),
            loadable_count=int(item.get("loadable_count") or 0),
            playable_count=int(item.get("playable_count") or 0),
            playable_keys=[str(key) for key in (item.get("playable_keys") or [])],
        )
    return out


def is_fresh(record: AndroidRecord, max_age_days: float) -> bool:
    if max_age_days <= 0:
        return True
    age = age_days(record.verified_at)
    return age is not None and age <= max_age_days


def passes(
    source_id: str,
    evidence: dict[str, AndroidRecord],
    *,
    max_age_days: float,
    require_playable: bool,
) -> bool:
    record = evidence.get(source_id)
    if record is None or not is_fresh(record, max_age_days):
        return False
    if not record.loaded:
        return False
    if require_playable and not record.playable:
        return False
    return True


def _at_least_as_strong(record: AndroidRecord, previous: AndroidRecord) -> bool:
    """Whether a fresh verdict may replace the stored one.

    A verification run may only sample the first N sites, so a narrow re-run can
    report zero playable sites while the stored verdict already proved that a
    site further down the list streams media.  Letting that narrow run overwrite
    the stronger verdict would silently unpublish a working source, so the file
    keeps the best verdict seen within the freshness window.
    """
    if record.playable_count != previous.playable_count:
        return record.playable_count > previous.playable_count
    if record.loadable_count != previous.loadable_count:
        return record.loadable_count > previous.loadable_count
    return True


def write_evidence(path: Path | str, records: list[AndroidRecord]) -> None:
    """Merge new verdicts into the file, keeping the strongest per source."""
    existing = load_evidence(path)
    for record in records:
        previous = existing.get(record.source_id)
        if previous is not None and not _at_least_as_strong(record, previous):
            continue
        existing[record.source_id] = record
    ordered = sorted(existing.values(), key=lambda item: item.source_id)
    payload = {
        "_readme": [
            "由 tools/android_verify.py --record 写入：真机/模拟器上 DexClassLoader "
            "每个 csp_*/jar 站点的结果。",
            "source_id 是归一化配置 URL 的 sha256；loadable_count>0 表示客户端不会再报 "
            "「jar解析失败」，playable_count>0 表示至少一个站点真的吐出了媒体 URL。",
        ],
        "verified": [record.to_dict() for record in ordered],
    }
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
