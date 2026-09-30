"""URL normalisation and dedup keys (spec §7).

``http://example.com/a``, ``https://example.com/a``, ``https://example.com/a/``
and every GitHub raw/blob spelling must collapse onto a single ``source_id``.
``source_id = sha256(normalized_url)``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}
MEDIA_SUFFIXES = (".m3u8", ".mp4", ".flv", ".mkv", ".ts", ".m4v", ".mov", ".mpd")

_URL_RE = re.compile(r"https?://[^\s\"'<>\\,\u3000]+", re.IGNORECASE)
_MULTI_SLASH_RE = re.compile(r"/{2,}")
_GITHUB_RAW_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/(?:raw|blob)/(.+)$")
_GITHUB_REF_RE = re.compile(r"^/([^/]+)/([^/]+)/refs/heads/(.+)$")
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9_-]*[a-z0-9])?)+$")
_SINGLE_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")


@dataclass(frozen=True)
class ParsedURL:
    scheme: str
    host: str
    port: int | None
    path: str
    query: str
    normalized: str   # aggressive, for dedup / source_id
    fetch: str        # first-seen, safe to request


def _normalize_github(host: str, path: str) -> tuple[str, str]:
    """Collapse the many spellings of a GitHub file URL onto raw.githubusercontent.com."""
    if host in ("github.com", "www.github.com"):
        match = _GITHUB_RAW_PATH_RE.match(path)
        if match:
            owner, repo, rest = match.groups()
            return "raw.githubusercontent.com", f"/{owner}/{repo}/{rest}"
    if host in ("raw.githubusercontent.com", "raw.github.com"):
        match = _GITHUB_REF_RE.match(path)
        if match:
            owner, repo, rest = match.groups()
            return "raw.githubusercontent.com", f"/{owner}/{repo}/{rest}"
    return host, path


def _netloc_host(host: str) -> str:
    """IPv6 literals must stay bracketed inside a netloc."""
    return f"[{host}]" if ":" in host else host


def _valid_host(host: str) -> bool:
    """Reject ``not a url`` style garbage that urlsplit happily accepts."""
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return bool(_HOSTNAME_RE.match(host) or _SINGLE_LABEL_RE.match(host))


def _normalize_query(query: str, drop_tracking: bool, tracking: tuple[str, ...]) -> str:
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    if drop_tracking:
        pairs = [(key, value) for key, value in pairs if key.lower() not in tracking]
    pairs.sort()
    return urlencode(pairs, doseq=True)


# ---------------------------------------------------------------------------
# Dedup options (spec §7).  These come from ``config/discovery.yaml`` so the
# dedup rules are configuration, not hard-coded behaviour (spec §35-14).
# ``configure_dedup`` is called once by ``load_config``; explicit keyword
# arguments to :func:`parse_url` always win over these defaults.
# ---------------------------------------------------------------------------
_DEDUP_DEFAULTS: dict[str, Any] = {
    "drop_query": False,
    "drop_tracking": True,
    "tracking": (),
    "strip_trailing_slash": True,
}


def configure_dedup(
    *,
    strip_trailing_slash: bool | None = None,
    drop_tracking: bool | None = None,
    tracking: Iterable[str] | None = None,
) -> None:
    """Install the dedup policy from config (spec §7).

    ``drop_tracking`` only ever removes the parameters named in
    ``tracking_params``.  Bulk query parameters are deliberately preserved:
    ``?token=``/``?id=`` are load-bearing on real feeds, and deleting them
    would both break fetching and merge genuinely different sources.
    """
    if strip_trailing_slash is not None:
        _DEDUP_DEFAULTS["strip_trailing_slash"] = bool(strip_trailing_slash)
    if drop_tracking is not None:
        _DEDUP_DEFAULTS["drop_tracking"] = bool(drop_tracking)
    if tracking is not None:
        _DEDUP_DEFAULTS["tracking"] = tuple(
            sorted({str(item).strip().lower() for item in tracking if str(item).strip()})
        )


def dedup_options() -> dict[str, Any]:
    """A snapshot of the active dedup policy (used by tests and `doctor`)."""
    return dict(_DEDUP_DEFAULTS)


def parse_url(
    raw: str,
    *,
    drop_query: bool | None = None,
    drop_tracking: bool | None = None,
    tracking: tuple[str, ...] | None = None,
    strip_trailing_slash: bool | None = None,
) -> ParsedURL | None:
    # unspecified keyword arguments fall back to the configured policy
    if drop_query is None:
        drop_query = bool(_DEDUP_DEFAULTS["drop_query"])
    if drop_tracking is None:
        drop_tracking = bool(_DEDUP_DEFAULTS["drop_tracking"])
    if tracking is None:
        tracking = tuple(_DEDUP_DEFAULTS["tracking"])
    if strip_trailing_slash is None:
        strip_trailing_slash = bool(_DEDUP_DEFAULTS["strip_trailing_slash"])
    if not raw:
        return None
    value = str(raw).strip().strip("\"'<>")
    if not value:
        return None
    if value.startswith("//"):
        value = "https:" + value
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None

    host = (parts.hostname or "").lower().rstrip(".")
    if not _valid_host(host):
        return None

    path = _MULTI_SLASH_RE.sub("/", parts.path or "")
    if path and not path.startswith("/"):
        path = "/" + path
    try:
        path = quote(path, safe="/%:@!$&'()*+,;=~-._")
    except Exception:  # pragma: no cover - quote is extremely lenient
        return None

    query = "" if drop_query else _normalize_query(parts.query, drop_tracking, tracking)

    fetch_host, fetch_path = _normalize_github(host, path)
    fetch_host = _netloc_host(fetch_host)
    fetch_netloc = fetch_host if port is None else f"{fetch_host}:{port}"
    fetch_url = urlunsplit((scheme, fetch_netloc, fetch_path, query, ""))

    dedup_path = fetch_path
    if strip_trailing_slash and dedup_path.endswith("/") and len(dedup_path) > 1:
        dedup_path = dedup_path.rstrip("/")
    if dedup_path == "/":
        dedup_path = ""
    dedup_netloc = fetch_host
    if port is not None and port not in (80, 443):
        dedup_netloc = f"{fetch_host}:{port}"
    # scheme is deliberately dropped so http://x/a and https://x/a share one
    # source_id (spec §7).  The real scheme survives in ParsedURL.fetch.
    normalized = urlunsplit(("https", dedup_netloc, dedup_path, query, ""))

    return ParsedURL(
        scheme=scheme,
        host=fetch_host,
        port=port,
        path=fetch_path,
        query=query,
        normalized=normalized,
        fetch=fetch_url,
    )


def normalize_url(raw: str, **kwargs) -> str:
    """Return the canonical dedup key, or ``""`` when the URL is unusable."""
    parsed = parse_url(raw, **kwargs)
    return parsed.normalized if parsed else ""


def source_id_for(raw: str) -> str:
    """``sha256(normalized_url)`` - stable across http/https and slash variants."""
    key = normalize_url(raw)
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def host_of(raw: str) -> str:
    """Bare host (IPv6 without brackets)."""
    parsed = parse_url(raw)
    if not parsed:
        return ""
    return parsed.host.strip("[]")


def is_http_url(raw: str) -> bool:
    return parse_url(raw) is not None


def looks_like_media(url: str) -> bool:
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return False
    return path.endswith(MEDIA_SUFFIXES)


def extract_urls(text: str) -> list[str]:
    """Pull http(s) URLs out of arbitrary text (used by the web adapter)."""
    seen: dict[str, None] = {}
    for match in _URL_RE.finditer(text or ""):
        candidate = match.group(0).rstrip(").,;'\"")
        key = normalize_url(candidate)
        if key:
            seen.setdefault(key, candidate)
    return list(seen.values())


def redact(raw: str) -> str:
    """Hide credentials before a URL lands in a log line."""
    parsed = parse_url(raw)
    if not parsed:
        return raw
    if "@" in (urlsplit(parsed.fetch).netloc or ""):
        netloc = urlsplit(parsed.fetch).netloc.split("@")[-1]
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
    return parsed.fetch
