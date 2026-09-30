"""L1-L5 orchestration -> one ProbeResult per (source, region) (spec §8, §35).

Design rules:

* every layer is short-circuited the moment it makes the deeper layers
  meaningless (a config that does not parse cannot be searched);
* a failure in any layer is recorded, never raised;
* the layer that produced the error is preserved in ``error_stage`` so a log
  line can be traced to ``source_id + region + check type`` (spec §35-13).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger
from ..models import HealthLevel, ProbeResult, Source
from ..parsers.json_parser import ConfigDoc
from .config_check import check_config
from .detail_check import check_detail
from .http_check import check_http, is_fatal_transport_error
from .playback_check import check_playback
from .search_check import check_search, search_video_ids

LOGGER = get_logger("checks.runner")

L1, L2, L3, L4, L5 = "L1_http", "L2_config", "L3_search", "L4_detail", "L5_playback"


@dataclass
class CheckOptions:
    search_keywords: list[dict[str, str]] = field(default_factory=list)
    max_sites_per_source: int = 5
    search_timeout: float = 10.0
    detail_timeout: float = 10.0
    playback_timeout: float = 8.0
    playback_probe_bytes: int = 1
    playback_max_urls: int = 3
    config_max_bytes: int = 5 * 1024 * 1024
    api_max_bytes: int = 2 * 1024 * 1024
    require_search_for_active: bool = True
    require_playback_for_active: bool = True
    config_failure_is_fatal: bool = True

    @classmethod
    def from_config(cls, cfg) -> "CheckOptions":
        section = cfg.section_default("checks")
        grade = section.get("grade") or {}
        keywords = section.get("search_keywords") or []
        cleaned = [
            {"label": str(item.get("label") or f"kw{index}"), "keyword": str(item.get("keyword") or "")}
            for index, item in enumerate(keywords)
            if isinstance(item, dict) and str(item.get("keyword") or "").strip()
        ]
        return cls(
            search_keywords=cleaned,
            max_sites_per_source=int(section.get("max_sites_per_source", 5)),
            search_timeout=float(section.get("search_timeout", 10)),
            detail_timeout=float(section.get("detail_timeout", 10)),
            playback_timeout=float(section.get("playback_timeout", 8)),
            playback_probe_bytes=int(section.get("playback_probe_bytes", 1)),
            playback_max_urls=int(section.get("playback_max_urls", 3)),
            config_max_bytes=int(cfg.get("http.max_response_bytes", 5 * 1024 * 1024)),
            require_search_for_active=bool(grade.get("require_search_for_active", True)),
            require_playback_for_active=bool(grade.get("require_playback_for_active", True)),
            config_failure_is_fatal=bool(grade.get("config_failure_is_fatal", True)),
        )


def _grade(
    probe: ProbeResult,
    doc: ConfigDoc | None,
    options: CheckOptions,
) -> str:
    if not probe.http_ok:
        return HealthLevel.FAILED
    if not probe.config_parse_success:
        return HealthLevel.FAILED if options.config_failure_is_fatal else HealthLevel.DEGRADED
    # a 多仓 wrapper carries no search surface of its own - reachable + valid is enough
    if doc is not None and doc.is_multi:
        return HealthLevel.ACTIVE
    if options.require_search_for_active and not probe.search_success:
        return HealthLevel.DEGRADED
    if options.require_playback_for_active and not probe.playback_probe_success:
        return HealthLevel.DEGRADED
    return HealthLevel.ACTIVE


def run_checks(
    source: Source,
    client,
    options: CheckOptions,
    *,
    region: str = "",
    node: str = "",
) -> ProbeResult:
    """Run L1-L5 against one source and return a populated ProbeResult."""
    probe = ProbeResult(source_id=source.id, region=region, node=node)
    url = source.raw_url or source.url
    if not url:
        probe.error_code = "NO_URL"
        probe.error_stage = L1
        probe.health_level = HealthLevel.FAILED
        return probe

    # ---- L1 HTTP ---------------------------------------------------------
    fields, result = check_http(url, client, timeout=client.read_timeout, max_bytes=options.config_max_bytes)
    _apply(probe, fields, L1)

    if not result.ok:
        probe.health_level = HealthLevel.FAILED
        return _log(probe, source, region)
    if result.truncated:
        probe.http_ok = False
        probe.error_code = "RESPONSE_TOO_LARGE"
        probe.error_stage = L1
        probe.health_level = HealthLevel.FAILED
        return _log(probe, source, region)
    if is_fatal_transport_error(result.error_code):
        probe.health_level = HealthLevel.FAILED
        return _log(probe, source, region)

    body = result.text

    # ---- L2 config -------------------------------------------------------
    config_fields, doc = check_config(body)
    _apply(probe, config_fields, L2)
    if not probe.config_parse_success:
        probe.health_level = HealthLevel.FAILED if options.config_failure_is_fatal else HealthLevel.DEGRADED
        return _log(probe, source, region)

    if doc is not None and doc.is_multi:
        probe.health_level = HealthLevel.ACTIVE
        return _log(probe, source, region)

    sites = list(doc.sites) if doc is not None else []

    # ---- L3 search -------------------------------------------------------
    outcome = check_search(
        sites,
        client,
        keywords=options.search_keywords,
        timeout=options.search_timeout,
        max_sites=options.max_sites_per_source,
        max_bytes=options.api_max_bytes,
    )
    _apply(probe, outcome.to_fields(), L3)
    if not outcome.success:
        probe.health_level = _grade(probe, doc, options)
        return _log(probe, source, region)

    # ---- L4 detail -------------------------------------------------------
    video_ids = search_video_ids(outcome)
    detail = check_detail(
        outcome.site,
        video_ids[0] if video_ids else "",
        client,
        timeout=options.detail_timeout,
        max_bytes=options.api_max_bytes,
    )
    _apply(probe, detail.to_fields(), L4)

    # ---- L5 playback -----------------------------------------------------
    if detail.success:
        playback = check_playback(
            detail.payload,
            client,
            timeout=options.playback_timeout,
            probe_bytes=options.playback_probe_bytes,
            max_urls=options.playback_max_urls,
        )
        _apply(probe, playback.to_fields(), L5)

    probe.health_level = _grade(probe, doc, options)
    return _log(probe, source, region)


_STAGE_PREFIX = {
    L1: "",
    L2: "",
    L3: "",
    L4: "",
    L5: "",
}


def _apply(probe: ProbeResult, fields: dict[str, Any], stage: str) -> None:
    """Merge a layer's fields; the *first* error wins so the root cause sticks."""
    for key, value in fields.items():
        if key in ("error_code", "error_message"):
            continue
        setattr(probe, key, value)
    code = fields.get("error_code")
    if code and not probe.error_code:
        probe.error_code = code
        probe.error_message = fields.get("error_message") or ""
        probe.error_stage = stage
    elif code:
        # keep the earliest root cause, but remember the layer that also complained
        probe.error_message = f"{probe.error_message} | {stage}:{code}"[:500]


def _log(probe: ProbeResult, source: Source, region: str) -> ProbeResult:
    level = LOGGER.info if probe.health_level == HealthLevel.ACTIVE else LOGGER.warning
    level(
        "check finished",
        extra={
            "source_id": source.id,
            "source_name": source.name,
            "region": region,
            "node": probe.node,
            "check": probe.error_stage or "ok",
            "url": source.raw_url or source.url,
            "health": probe.health_level,
            "error_code": probe.error_code or "",
        },
    )
    return probe
