"""Config parsing + schema detection (spec §6, §8 L2).

Recognised shapes:

``multi``   多仓 wrapper, e.g. ``{"urls": [{"name": ..., "url": ...}]}``
``single``  单仓 config, e.g. ``{"sites": [...], "lives": [...], "parses": [...]}``
``sites``   a bare site list (``[{...}, {...}]`` or ``{"sites": [...]}``)
``unknown`` valid JSON that matches none of the above

Anything that is not valid JSON, or that is obviously HTML/binary, is rejected
before it can ever reach the health-check pool.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_MULTI = "multi"
SCHEMA_SINGLE = "single"
SCHEMA_SITES = "sites"
SCHEMA_UNKNOWN = "unknown"

SITE_COLLECTIONS = ("sites", "lives", "parses", "flags", "rules", "ads", "ijk", "hosts", "wallpaper", "logo")
SINGLE_ONLY_KEYS = ("lives", "parses", "rules", "flags", "wallpaper", "spider", "ads", "ijk", "logo")

_HTML_RE = re.compile(r"^\s*(<!doctype\s+html|<html|<\?xml|\{[\s]*\"?<)", re.IGNORECASE)
_BINARY_RE = re.compile(rb"[\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0e\x0f]")


class ParseError(ValueError):
    """Raised when a payload cannot be treated as a config."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}".strip(": "))
        self.code = code
        self.message = message


@dataclass
class ConfigDoc:
    schema: str
    raw: Any
    sites: list[dict[str, Any]] = field(default_factory=list)
    lives: list[dict[str, Any]] = field(default_factory=list)
    parses: list[dict[str, Any]] = field(default_factory=list)
    children: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_multi(self) -> bool:
        return self.schema == SCHEMA_MULTI

    def summary(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "sites": len(self.sites),
            "lives": len(self.lives),
            "parses": len(self.parses),
            "children": len(self.children),
        }


def looks_like_html(text: str) -> bool:
    return bool(_HTML_RE.match(text or ""))


def looks_like_binary(payload: bytes) -> bool:
    if not payload:
        return False
    sample = payload[:2048]
    if b"\x00" in sample:
        return True
    return bool(_BINARY_RE.search(sample))


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _child_entries(value: Any) -> list[dict[str, Any]]:
    """Normalise a 多仓 ``urls`` array, which may hold strings or objects."""
    entries: list[dict[str, Any]] = []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return entries
    for item in value:
        if isinstance(item, str):
            if item.strip():
                entries.append({"url": item.strip(), "name": ""})
        elif isinstance(item, dict):
            url = item.get("url") or item.get("api") or item.get("path") or ""
            if isinstance(url, str) and url.strip():
                entries.append({
                    "url": url.strip(),
                    "name": str(item.get("name") or item.get("title") or "").strip(),
                    "raw": item,
                })
    return entries


def detect_schema(data: Any) -> str:
    if isinstance(data, list):
        if not data:
            return SCHEMA_UNKNOWN
        # ["https://a/c.json", ...] is a first-class multi-repo spelling and
        # has to be checked before the dict-shaped branches
        if all(isinstance(item, str) for item in data):
            return SCHEMA_MULTI
        dicts = [item for item in data if isinstance(item, dict)]
        if not dicts:
            return SCHEMA_UNKNOWN
        if any(("api" in item) for item in dicts):
            return SCHEMA_SITES
        if any(("url" in item) for item in dicts):
            return SCHEMA_MULTI
        return SCHEMA_UNKNOWN

    if not isinstance(data, dict):
        return SCHEMA_UNKNOWN

    if isinstance(data.get("urls"), (list, dict)):
        return SCHEMA_MULTI
    if isinstance(data.get("sites"), list):
        if any(key in data for key in SINGLE_ONLY_KEYS):
            return SCHEMA_SINGLE
        return SCHEMA_SITES
    if any(key in data for key in SINGLE_ONLY_KEYS):
        return SCHEMA_SINGLE
    # a lone site object
    if "api" in data and ("name" in data or "key" in data):
        return SCHEMA_SITES
    return SCHEMA_UNKNOWN


def parse_text(text: str) -> ConfigDoc:
    """Parse a fetched config body.  Raises :class:`ParseError`."""
    if text is None:
        raise ParseError("EMPTY_BODY")
    stripped = text.strip()
    if not stripped:
        raise ParseError("EMPTY_BODY")
    if looks_like_html(stripped):
        raise ParseError("HTML_NOT_JSON", "body looks like an HTML page")
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ParseError("INVALID_JSON", f"{exc.msg} at line {exc.lineno} column {exc.colno}") from exc
    return build_doc(data)


def build_doc(data: Any) -> ConfigDoc:
    schema = detect_schema(data)
    doc = ConfigDoc(schema=schema, raw=data)

    if schema == SCHEMA_MULTI:
        if isinstance(data, list):
            doc.children = _child_entries(data)
        else:
            doc.children = _child_entries(data.get("urls"))
            doc.meta = {k: v for k, v in data.items() if k != "urls" and not isinstance(v, (list, dict))}
        if not doc.children:
            raise ParseError("MULTI_WITHOUT_URLS", "multi-repo config has no usable urls[]")
        return doc

    if schema == SCHEMA_SITES:
        if isinstance(data, list):
            doc.sites = [item for item in data if isinstance(item, dict)]
        elif isinstance(data.get("sites"), list):
            doc.sites = _dict_list(data.get("sites"))
        else:
            # a lone site object: {"api": ..., "name": ...}
            doc.sites = [data]
        if not doc.sites:
            raise ParseError("SITES_EMPTY", "site list is empty")
        return doc

    if schema == SCHEMA_SINGLE:
        doc.sites = _dict_list(data.get("sites"))
        doc.lives = _dict_list(data.get("lives"))
        doc.parses = _dict_list(data.get("parses"))
        doc.meta = {
            key: value
            for key, value in data.items()
            if key not in SITE_COLLECTIONS and not isinstance(value, (list, dict))
        }
        if not (doc.sites or doc.lives or doc.parses):
            raise ParseError("SINGLE_WITHOUT_CONTENT", "single config has no sites/lives/parses")
        return doc

    raise ParseError("UNKNOWN_SCHEMA", "JSON is valid but not a recognised TVBox config shape")


def required_fields_present(doc: ConfigDoc) -> tuple[bool, str]:
    """L2 completeness check (spec §8)."""
    if doc.schema == SCHEMA_UNKNOWN:
        return False, "unknown schema"
    if doc.schema == SCHEMA_MULTI:
        if not doc.children:
            return False, "no children"
        return True, ""
    if doc.schema in (SCHEMA_SINGLE, SCHEMA_SITES):
        usable = [site for site in doc.sites if str(site.get("api") or "").strip()]
        if not usable:
            return False, "no site carries an api field"
        return True, ""
    return False, "no content"
