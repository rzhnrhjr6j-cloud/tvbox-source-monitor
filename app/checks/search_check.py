"""L3 - search test (spec §8).

Uses only fixed, publicly documented test keywords (spec §8: never user data).
The first keyword finds a working endpoint; the remaining keywords are then run
against that endpoint so all three probes really happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..parsers.normalizer import (
    NormalizedSite,
    count_results,
    extract_video_ids,
    normalize_sites,
    search_url_candidates,
)


@dataclass
class SearchOutcome:
    success: bool = False
    result_count: int = 0
    first_ms: int | None = None
    site: NormalizedSite | None = None
    payload: Any = None
    keyword_hits: dict[str, int] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    def to_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "search_success": self.success,
            "search_result_count": self.result_count,
            "search_first_ms": self.first_ms,
        }
        if not self.success and self.error_code:
            fields["error_code"] = self.error_code
            fields["error_message"] = (self.error_message or "")[:500]
        return fields


def _attempt(client, url: str, timeout: float, max_bytes: int):
    result = client.request(
        "GET",
        url,
        timeout=(client.connect_timeout, timeout),
        max_bytes=max_bytes,
        headers={"Accept": "application/json, text/plain, */*"},
    )
    if not result.ok:
        return None, result
    try:
        return result.json(), result
    except ValueError:
        return None, result


def check_search(
    sites: list[dict[str, Any]] | list[NormalizedSite],
    client,
    *,
    keywords: list[dict[str, str]],
    timeout: float = 10,
    max_sites: int = 5,
    max_bytes: int = 2 * 1024 * 1024,
) -> SearchOutcome:
    if not keywords:
        return SearchOutcome(error_code="NO_KEYWORDS", error_message="checks.search_keywords is empty")

    normalized = sites if all(isinstance(s, NormalizedSite) for s in sites) else normalize_sites(sites)
    if not normalized:
        return SearchOutcome(error_code="NO_SITES", error_message="config exposes no usable api endpoint")

    outcome = SearchOutcome()
    last_error: tuple[str, str] | None = None

    # -- phase 1: locate a working endpoint with the first keyword ----------
    first_keyword = str(keywords[0].get("keyword") or "")
    working_site: NormalizedSite | None = None
    working_payload: Any = None
    for site in normalized[:max_sites]:
        if not site.searchable:
            continue
        for url in search_url_candidates(site.api, first_keyword):
            payload, result = _attempt(client, url, timeout, max_bytes)
            if payload is None:
                last_error = (result.error_code or "SEARCH_NO_JSON", result.error_message or url)
                continue
            if count_results(payload) > 0:
                working_site, working_payload = site, payload
                outcome.first_ms = result.first_byte_ms or result.elapsed_ms
                break
            last_error = ("SEARCH_EMPTY_RESULT", f"{url} returned 0 results")
        if working_site:
            break

    if working_site is None:
        outcome.error_code, outcome.error_message = last_error or (
            "SEARCH_FAILED",
            "no searchable site returned results",
        )
        return outcome

    # -- phase 2: run the remaining keywords against the working endpoint ---
    outcome.site = working_site
    outcome.payload = working_payload
    total = 0
    for index, entry in enumerate(keywords):
        label = str(entry.get("label") or f"kw{index}")
        keyword = str(entry.get("keyword") or "")
        if not keyword:
            continue
        if index == 0:
            hits = count_results(working_payload)
            outcome.keyword_hits[label] = hits
            total += hits
            continue
        hits = 0
        for url in search_url_candidates(working_site.api, keyword):
            payload, _ = _attempt(client, url, timeout, max_bytes)
            if payload is None:
                continue
            hits = count_results(payload)
            if hits:
                if outcome.payload is None:
                    outcome.payload = payload
                break
        outcome.keyword_hits[label] = hits
        total += hits

    outcome.result_count = total
    outcome.success = total > 0
    if not outcome.success:
        outcome.error_code = "SEARCH_NO_RESULTS"
        outcome.error_message = "no keyword returned any result"
    return outcome


def search_video_ids(outcome: SearchOutcome) -> list[str]:
    return extract_video_ids(outcome.payload)
