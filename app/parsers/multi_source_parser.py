"""Multi-source (多仓) expansion (spec §5.1.C).

When a candidate turns out to be a wrapper config we do not stop at the
wrapper: every child becomes its own independently scored ``Source`` and the
parent/child relationship is preserved via ``parent_id``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .json_parser import ConfigDoc, ParseError, build_doc, parse_text
from ..utils.urls import parse_url, source_id_for


@dataclass
class ChildSource:
    source_id: str
    url: str          # fetchable URL (first spelling we saw)
    name: str
    parent_id: str
    depth: int = 1
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.source_id,
            "url": self.url,
            "name": self.name,
            "parent_id": self.parent_id,
            "depth": self.depth,
        }


def expand(doc: ConfigDoc, parent_id: str, max_children: int = 200, depth: int = 1) -> list[ChildSource]:
    """Turn a ``multi`` doc into child sources.  Never raises."""
    if doc is None or not doc.children:
        return []
    children: list[ChildSource] = []
    seen: set[str] = set()
    for entry in doc.children[:max_children]:
        raw_url = str(entry.get("url") or "").strip()
        parsed = parse_url(raw_url)
        if parsed is None:
            continue
        child_id = source_id_for(raw_url)
        if not child_id or child_id in seen or child_id == parent_id:
            continue
        seen.add(child_id)
        name = str(entry.get("name") or "").strip() or parsed.host
        children.append(
            ChildSource(
                source_id=child_id,
                url=parsed.fetch,
                name=name,
                parent_id=parent_id,
                depth=depth,
                raw=entry.get("raw") or {},
            )
        )
    return children


def expand_payload(
    text: str,
    parent_id: str,
    max_children: int = 200,
    depth: int = 1,
) -> list[ChildSource]:
    """Convenience wrapper: parse then expand.  Returns ``[]`` on any failure."""
    try:
        doc = parse_text(text)
    except ParseError:
        return []
    return expand(doc, parent_id, max_children, depth)


def build_child_doc_stub(child: ChildSource) -> dict[str, Any]:
    """Metadata recorded alongside a child source so lineage survives rebuilds."""
    return {
        "parent_id": child.parent_id,
        "depth": child.depth,
        "origin": "expansion",
        "child_name": child.name,
    }


def flatten(children: Iterable[ChildSource]) -> list[dict[str, Any]]:
    return [child.to_dict() for child in children]
