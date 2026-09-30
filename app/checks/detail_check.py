"""L4 - detail test (spec §8).

From a search hit, walk into the detail endpoint and check whether a playlist /
episode list actually comes back.  Only read-only GETs on the source's own
public API - no auth, no tokens, nothing bypassed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..parsers.normalizer import NormalizedSite, detail_url_candidates, has_playlist


@dataclass
class DetailOutcome:
    success: bool = False
    has_playlist: bool = False
    payload: Any = None
    error_code: str | None = None
    error_message: str | None = None

    def to_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "detail_success": self.success,
            "detail_has_playlist": self.has_playlist,
        }
        if not self.success and self.error_code:
            fields["error_code"] = self.error_code
            fields["error_message"] = (self.error_message or "")[:500]
        return fields


def check_detail(
    site: NormalizedSite | None,
    video_id: str,
    client,
    *,
    timeout: float = 10,
    max_bytes: int = 2 * 1024 * 1024,
) -> DetailOutcome:
    if site is None or not video_id:
        return DetailOutcome(error_code="DETAIL_NO_INPUT", error_message="no site or video id available")

    last_error: tuple[str, str] | None = None
    for url in detail_url_candidates(site.api, video_id):
        result = client.request(
            "GET",
            url,
            timeout=(client.connect_timeout, timeout),
            max_bytes=max_bytes,
            headers={"Accept": "application/json, text/plain, */*"},
        )
        if not result.ok:
            last_error = (result.error_code or "DETAIL_HTTP", result.error_message or url)
            continue
        try:
            payload = result.json()
        except ValueError:
            last_error = ("DETAIL_NO_JSON", url)
            continue
        if not isinstance(payload, dict) or not payload.get("list"):
            last_error = ("DETAIL_EMPTY", url)
            continue
        return DetailOutcome(success=True, has_playlist=has_playlist(payload), payload=payload)

    code, message = last_error or ("DETAIL_FAILED", "no detail endpoint responded")
    return DetailOutcome(error_code=code, error_message=message)
