"""Cheap host reachability precheck for configs that need a crawler jar.

The build mirror already removes sites whose jar is definitively gone, but that
happens after a candidate has consumed an admission slot and an initial search
probe.  Most stale GitHub configs point every ``csp_*`` site at a dead jar
host, so rejecting those candidates during discovery leaves the bounded run
budget for configs that can still work on a phone.

This check is deliberately conservative: only URLs that are definitively
404/410, dead hosts, empty 200 responses, or HTML soft-404s count as dead.
Timeouts, 5xx responses, redirects we cannot classify, and relative/non-HTTP
references stay *unknown* and therefore pass.  A false positive here would
hide a source that the next build might have recovered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_REF_TAIL = re.compile(r"(\$.*|;md5;.*)$")
_DEAD_STATUS = frozenset({404, 410})
_DEAD_HOST_ERRORS = frozenset({"DNS_ERROR", "CONNECT_ERROR", "TLS_ERROR", "SSRF_BLOCKED"})
_JAR_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"dex\n")
_GITHUB_ORIGIN_MARKERS = ("raw.githubusercontent.com/", "github.com/", "gitee.com/")
_JAR_MIRRORS = ("", "https://ghfast.top/", "https://gh-proxy.com/", "https://hk.gh-proxy.org/")


def _clean_reference(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _REF_TAIL.sub("", value.strip()).strip()


def _unwrap_proxy_url(value: str) -> str:
    """Return the innermost known origin URL from a proxied jar URL."""
    text = value.strip()
    for marker in _GITHUB_ORIGIN_MARKERS:
        index = text.find(marker)
        if index < 0:
            continue
        start = text.rfind("http", 0, index)
        if start >= 0:
            return text[start:]
    return text


def _probe_urls(reference: str) -> list[str]:
    """Try the author's URL and the same GitHub mirrors used by the verifier."""
    origin = _unwrap_proxy_url(reference)
    prefixes = _JAR_MIRRORS if "github" in origin else ("",)
    urls: list[str] = []
    for prefix in prefixes:
        candidate = f"{prefix}{origin}" if prefix else origin
        if candidate not in urls:
            urls.append(candidate)
    return urls


def _classify_probe(result: Any) -> str:
    if result.ok and result.content and not _looks_like_html(result.content):
        return "alive" if _looks_like_jar(result.content) else "unknown"
    if result.status in _DEAD_STATUS or result.error_code in _DEAD_HOST_ERRORS:
        return "dead"
    if result.ok and not result.content:
        return "dead"
    if result.ok and _looks_like_html(result.content):
        return "dead"
    return "unknown"


def jar_references(payload: Any) -> list[str]:
    """Return explicit crawler-jar references in a parsed config.

    ``csp_*`` sites that inherit a top-level ``spider`` are covered by that
    spider.  A bare csp key with no URL is not probeable and is intentionally
    omitted so it cannot make an otherwise dead config look reachable.
    """
    if not isinstance(payload, dict):
        return []
    refs: set[str] = set()
    spider = _clean_reference(payload.get("spider"))
    if spider.lower().endswith(".jar"):
        refs.add(spider)
    for site in payload.get("sites") or []:
        if not isinstance(site, dict):
            continue
        for key in ("jar", "api", "ext"):
            ref = _clean_reference(site.get(key))
            if ref.lower().endswith(".jar"):
                refs.add(ref)
    return sorted(refs)


def _looks_like_html(blob: bytes) -> bool:
    sample = blob[:256].lstrip().lower()
    return sample.startswith((b"<!doctype html", b"<html", b"<?xml"))


def _looks_like_jar(blob: bytes) -> bool:
    return any(blob.startswith(magic) for magic in _JAR_MAGIC)


@dataclass(frozen=True)
class JarReachability:
    checked: int = 0
    alive: int = 0
    dead: int = 0
    unknown: int = 0

    @property
    def rejected(self) -> bool:
        """True only when every probeable reference is definitively dead."""
        return self.checked > 0 and self.alive == 0 and self.dead > 0 and self.unknown == 0


def check_jar_reachability(
    payload: Any,
    client: Any,
    *,
    timeout: float,
    max_probes: int,
    cache: dict[str, str] | None = None,
) -> JarReachability:
    """Probe explicit jar URLs, using a per-run URL cache.

    A successful probe stops immediately: one live jar is enough for the
    client to open the source, and the build's own hosting pass will handle
    the rest.
    """
    references = jar_references(payload)
    if not references:
        return JarReachability()

    state = cache if cache is not None else {}
    checked = alive = dead = unknown = 0
    for reference in references:
        url = _clean_reference(reference)
        if not url.startswith(("http://", "https://")):
            unknown += 1
            continue

        cache_key = _unwrap_proxy_url(url)
        verdict = state.get(cache_key)
        if verdict is None:
            if len(state) >= max(1, max_probes):
                unknown += 1
                continue
            checked += 1
            verdicts: list[str] = []
            for probe_url in _probe_urls(url):
                result = client.probe_first_bytes(
                    probe_url,
                    8,
                    timeout=(client.connect_timeout, timeout),
                    max_retries=0,
                )
                candidate_verdict = _classify_probe(result)
                verdicts.append(candidate_verdict)
                if candidate_verdict == "alive":
                    break
            if "alive" in verdicts:
                verdict = "alive"
            elif "unknown" in verdicts:
                verdict = "unknown"
            else:
                verdict = "dead"
            state[cache_key] = verdict

        if verdict == "alive":
            alive += 1
            break
        if verdict == "dead":
            dead += 1
        else:
            unknown += 1

    return JarReachability(checked=checked, alive=alive, dead=dead, unknown=unknown)
