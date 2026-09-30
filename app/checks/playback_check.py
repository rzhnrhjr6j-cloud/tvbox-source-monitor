"""L5 - playback probe (spec §8, §38).

Probes only truly public, directly addressable media URLs: connect, optionally
range-read a single byte, look at content-type and first-byte latency.

Deliberately NOT done: no DRM interaction, no Referer/Token emulation, no
access-control circumvention, and never a full download.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..parsers.normalizer import extract_play_urls


@dataclass
class PlaybackOutcome:
    url_obtained: bool = False
    success: bool = False
    probed: int = 0
    content_type: str = ""
    first_byte_ms: int | None = None
    probed_url: str = ""
    error_code: str | None = None
    error_message: str | None = None

    def to_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "playback_url_obtained": self.url_obtained,
            "playback_probe_success": self.success,
            "playback_content_type": self.content_type,
            "playback_first_byte_ms": self.first_byte_ms,
        }
        if not self.success and self.error_code:
            fields["error_code"] = self.error_code
            fields["error_message"] = (self.error_message or "")[:500]
        return fields


def check_playback(
    payload: Any,
    client,
    *,
    timeout: float = 8,
    probe_bytes: int = 1,
    max_urls: int = 3,
) -> PlaybackOutcome:
    urls = extract_play_urls(payload, limit=max(max_urls, 1))
    if not urls:
        return PlaybackOutcome(
            url_obtained=False,
            error_code="NO_PLAYBACK_URL",
            error_message="detail response carried no directly probeable http(s) media URL",
        )

    outcome = PlaybackOutcome(url_obtained=True)
    last_error: tuple[str, str] | None = None
    for url in urls:
        result = client.probe_first_bytes(
            url,
            nbytes=probe_bytes,
            timeout=(client.connect_timeout, timeout),
            max_bytes=64 * 1024,
        )
        outcome.probed += 1
        if result.error_code == "SSRF_BLOCKED":
            last_error = ("PLAYBACK_SSRF_BLOCKED", result.error_message or url)
            continue
        if result.status is None:
            last_error = (result.error_code or "PLAYBACK_ERROR", result.error_message or url)
            continue
        if result.status >= 400:
            last_error = (f"HTTP_{result.status}", f"{url} -> {result.status}")
            continue
        outcome.success = True
        outcome.content_type = result.content_type
        outcome.first_byte_ms = result.first_byte_ms
        outcome.probed_url = url
        return outcome

    code, message = last_error or ("PLAYBACK_FAILED", "no candidate playback url responded")
    outcome.error_code = code
    outcome.error_message = message
    return outcome
