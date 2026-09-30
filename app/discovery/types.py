"""Shared types for the discovery adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Candidate:
    """One URL that some adapter thinks might be a config."""

    url: str
    adapter: str = ""
    query: str = ""
    name: str = ""
    note: str = ""
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "adapter": self.adapter,
            "query": self.query,
            "name": self.name,
            "note": self.note,
            "tags": list(self.tags),
        }
