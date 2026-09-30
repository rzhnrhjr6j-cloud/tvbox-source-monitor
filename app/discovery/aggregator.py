"""Discovery orchestration + the admission pipeline (spec §6, §7, §26, §27).

Spec §6 is explicit that "we found a URL" is not "it belongs in the output":

    discovered -> URL normalize -> reachability -> content-type -> JSON parse
    -> schema detect -> candidate score -> admitted | rejected_candidates

Multi-repo configs are expanded into independently scored children (§5.1.C),
recursively but with hard depth and breadth caps.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..checks.search_check import check_search
from ..logging_setup import get_logger
from ..models import DiscoveryRecord, Event, Source, SourceEvent, Status
from ..parsers.json_parser import ConfigDoc, ParseError, parse_text, required_fields_present
from ..parsers.multi_source_parser import expand_payload
from ..policy import PolicySet
from ..utils.urls import normalize_url, parse_url
from .github import GitHubAdapter
from .types import Candidate
from .web import WebAdapter

LOGGER = get_logger("discovery")

REJECTED_FILE = "rejected_candidates.json"


@dataclass
class AdmissionOutcome:
    url: str
    normalized: str = ""
    source_id: str = ""
    admitted: bool = False
    reason: str = ""
    score: float = 0.0
    schema: str = ""
    type: str = "single"
    name: str = ""
    adapter: str = ""
    depth: int = 0
    parent_id: str | None = None
    references: int = 0
    children: list[str] = field(default_factory=list)
    signals: dict[str, bool] = field(default_factory=dict)
    # parsed child specs, kept in-memory only so ``run`` does not re-fetch the
    # parent config just to expand it a second time
    child_specs: list[Any] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "normalized": self.normalized,
            "source_id": self.source_id,
            "admitted": self.admitted,
            "reason": self.reason,
            "candidate_score": self.score,
            "schema": self.schema,
            "type": self.type,
            "name": self.name,
            "adapter": self.adapter,
            "depth": self.depth,
            "parent_id": self.parent_id,
            "references": self.references,
            "signals": self.signals,
        }


class DiscoveryAggregator:
    def __init__(self, cfg, client, store, scorer, policy: PolicySet | None = None):
        self.cfg = cfg
        self.client = client
        self.store = store
        self.scorer = scorer
        self.policy = policy or PolicySet(cfg)
        self.admission = cfg.section_default("admission")
        self.expansion = cfg.section_default("discovery").get("expansion") or {}
        self.max_candidates = int(self.admission.get("max_candidates_per_run", 300))
        self.max_config_bytes = int(self.admission.get("max_config_bytes", 5 * 1024 * 1024))
        self.reject_cooldown = float(self.admission.get("reject_cooldown_hours", 24))
        self.initial_search = bool(self.admission.get("initial_search", True))
        self.initial_search_timeout = float(self.admission.get("initial_search_timeout", 10))
        self.rejected_path: Path = cfg.path("app.data_dir") / REJECTED_FILE
        self.adapters = self._build_adapters()

    def _build_adapters(self) -> list[Any]:
        enabled = [str(item) for item in (self.cfg.get("discovery.enabled_adapters") or [])]
        adapters: list[Any] = []
        if "manual" in enabled:
            adapters.append("manual")
        if "github" in enabled:
            adapters.append(GitHubAdapter(self.cfg, self.client))
        if "web" in enabled:
            adapters.append(WebAdapter(self.cfg, self.client))
        if "expansion" in enabled:
            adapters.append("expansion")
        return adapters

    # -- collection --------------------------------------------------------
    def collect(self) -> tuple[list[Candidate], Counter]:
        candidates: list[Candidate] = []
        references: Counter = Counter()

        for adapter in self.adapters:
            if adapter == "manual":
                candidates.extend(self._collect_manual())
                continue
            if adapter == "expansion":
                continue
            try:
                produced = list(adapter.discover())
            except Exception as exc:  # noqa: BLE001 - one adapter must never kill discovery
                LOGGER.error(
                    "discovery adapter crashed",
                    extra={"stage": "discovery", "check": getattr(adapter, "name", "?"), "error": str(exc)[:300]},
                )
                continue
            LOGGER.info(
                "discovery adapter finished",
                extra={"stage": "discovery", "check": getattr(adapter, "name", "?"), "found": len(produced)},
            )
            candidates.extend(produced)

        deduped: dict[str, Candidate] = {}
        for candidate in candidates:
            key = self.policy.canonical_id(candidate.url)
            if not key:
                continue
            references[key] += 1
            deduped.setdefault(key, candidate)
        return list(deduped.values()), references

    def _collect_manual(self) -> list[Candidate]:
        section = self.cfg.section_default("discovery").get("manual") or {}
        path = self.cfg.path("app.data_dir") / str(section.get("file", "candidates.json")).split("/")[-1]
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("manual candidate pool unreadable",
                           extra={"stage": "discovery", "check": "manual", "error": str(exc)[:200]})
            return []

        items: Iterable[Any] = data.get("urls") if isinstance(data, dict) else data
        out: list[Candidate] = []
        for item in items or []:
            if isinstance(item, str):
                out.append(Candidate(url=item.strip(), adapter="manual", query="candidates.json", name="manual"))
            elif isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                out.append(
                    Candidate(
                        url=url,
                        adapter="manual",
                        query="candidates.json",
                        name=str(item.get("name") or ""),
                        note=str(item.get("note") or ""),
                        tags=[str(tag) for tag in (item.get("tags") or [])],
                    )
                )
        return out

    # -- admission ---------------------------------------------------------
    def _fetch(self, url: str) -> tuple[ConfigDoc | None, str, str]:
        """Return ``(doc, reason, body)``; ``doc is None`` means rejected."""
        result = self.client.get_json(
            url,
            timeout=(self.client.connect_timeout, self.client.read_timeout),
            max_bytes=self.max_config_bytes,
        )
        if not result.ok:
            return None, f"UNREACHABLE:{result.error_code or result.status}", ""
        if result.truncated:
            return None, "RESPONSE_TOO_LARGE", ""
        content_type = result.content_type
        if content_type and content_type not in ("application/json", "text/plain", "text/json", ""):
            if bool(self.admission.get("reject_html", True)) and content_type in ("text/html", "application/xhtml+xml"):
                return None, f"CONTENT_TYPE:{content_type}", ""
        body = result.text
        try:
            doc = parse_text(body)
        except ParseError as exc:
            return None, exc.code, ""
        complete, reason = required_fields_present(doc)
        if not complete:
            return None, f"INCOMPLETE:{reason}"[:120], ""
        return doc, "", body

    def _signals(self, url: str, doc: ConfigDoc, references: int, initial_search_ok: bool | None) -> dict[str, bool]:
        parsed = parse_url(url)
        signals = {
            "reachable": True,
            "json_valid": doc is not None,
            "schema_valid": True,
            "https": bool(parsed and parsed.scheme == "https"),
            "multi_reference": references >= 2,
            "initial_search": True,
        }
        if initial_search_ok is not None:
            signals["initial_search"] = initial_search_ok
        return signals

    def _initial_search(self, doc: ConfigDoc) -> bool | None:
        if not self.initial_search:
            return None
        if doc.schema not in ("single", "sites"):
            return None
        keywords = (self.cfg.section_default("checks").get("search_keywords") or [])[:1]
        if not keywords:
            return None
        outcome = check_search(
            doc.sites,
            self.client,
            keywords=[{"label": str(keywords[0].get("label") or "kw0"), "keyword": str(keywords[0].get("keyword") or "")}],
            timeout=self.initial_search_timeout,
            max_sites=3,
            max_bytes=2 * 1024 * 1024,
        )
        return outcome.success

    def admit(
        self,
        candidate: Candidate,
        references: int = 1,
        *,
        parent_id: str | None = None,
        depth: int = 0,
    ) -> AdmissionOutcome:
        outcome = AdmissionOutcome(
            url=candidate.url,
            adapter=candidate.adapter,
            depth=depth,
            parent_id=parent_id,
            references=references,
        )
        parsed = parse_url(candidate.url)
        if parsed is None:
            outcome.reason = "BAD_URL"
            return outcome
        outcome.normalized = parsed.normalized
        outcome.source_id = self.policy.canonical_id(candidate.url)

        if self.policy.is_blacklisted(candidate.url):
            outcome.reason = f"BLACKLISTED:{self.policy.blacklist_reason(candidate.url)}"
            self.store.record_discoveries([DiscoveryRecord(
                url=candidate.url, normalized_url=outcome.normalized, source_id=outcome.source_id,
                adapter=candidate.adapter, query=candidate.query, admitted=False, reason="BLACKLISTED",
            )])
            return outcome

        if self.store.seen_recently(outcome.normalized, self.reject_cooldown):
            existing = self.store.get_source(outcome.source_id)
            if existing is not None:
                outcome.admitted = existing.status != Status.REJECTED
                outcome.reason = "KNOWN"
                outcome.name = existing.name
                outcome.schema = existing.schema
                outcome.type = existing.type
                return outcome
            if parent_id is None:
                outcome.reason = "RECENTLY_REJECTED"
                return outcome

        doc, reason, _body = self._fetch(parsed.fetch)
        outcome.signals = {"reachable": doc is not None, "json_valid": doc is not None, "schema_valid": doc is not None}
        if doc is None:
            outcome.reason = reason
            self._record(candidate, outcome, admitted=False)
            return outcome

        outcome.schema = doc.schema
        outcome.type = "multi" if doc.is_multi else "single"
        outcome.name = candidate.name or parsed.host

        search_ok = self._initial_search(doc)
        signals = self._signals(parsed.fetch, doc, references, search_ok)
        outcome.signals = signals
        outcome.score = self.scorer.candidate_score(signals)
        outcome.signals["candidate_score"] = round(outcome.score, 3)

        threshold = self.scorer.candidate_threshold()
        if outcome.score < threshold:
            outcome.reason = f"BELOW_THRESHOLD:{outcome.score:.1f}<{threshold:.0f}"
            self._record(candidate, outcome, admitted=False)
            return outcome

        outcome.admitted = True

        if doc.is_multi and self.expansion.get("enabled", True):
            kids = expand_payload(
                json.dumps(doc.raw, ensure_ascii=False),
                outcome.source_id,
                max_children=int(self.expansion.get("max_children", 200)),
                depth=depth + 1,
            )
            outcome.child_specs = kid_sources[:] if (kid_sources := list(kids)) else []
            outcome.children = [child.source_id for child in kid_sources]

        self._upsert_source(doc, outcome, candidate)
        self._record(candidate, outcome, admitted=True)
        return outcome

    def _upsert_source(self, doc: ConfigDoc, outcome: AdmissionOutcome, candidate: Candidate) -> None:
        existing = self.store.get_source(outcome.source_id)
        if existing is not None:
            existing.last_seen_at = existing.last_seen_at
            existing.schema = doc.schema or existing.schema
            existing.type = outcome.type
            existing.candidate_score = outcome.score
            if not existing.name:
                existing.name = outcome.name
            existing.touch()
            if self.policy.is_whitelisted(candidate.url):
                existing.whitelisted = True
            self.store.upsert_source(existing)
            self.store.record_event(SourceEvent(
                source_id=existing.id, event=Event.DISCOVERED, detail="re-seen by discovery",
                to_status=existing.status,
            ))
            return

        source = Source(
            id=outcome.source_id,
            url=outcome.normalized,
            raw_url=parse_url(candidate.url).fetch if parse_url(candidate.url) else candidate.url,
            name=outcome.name,
            type=outcome.type,
            parent_id=outcome.parent_id,
            schema=doc.schema,
            status=Status.CANDIDATE,
            candidate_score=outcome.score,
            tags=list(candidate.tags),
            note=candidate.note,
            whitelisted=self.policy.is_whitelisted(candidate.url),
        )
        self.store.upsert_source(source)
        self.store.record_event(SourceEvent(
            source_id=source.id,
            event=Event.ADMITTED if outcome.admitted else Event.DISCOVERED,
            detail=f"adapter={candidate.adapter} score={outcome.score:.1f}",
            from_status=Status.DISCOVERED,
            to_status=Status.CANDIDATE,
        ))

    def _record(self, candidate: Candidate, outcome: AdmissionOutcome, *, admitted: bool) -> None:
        self.store.record_discoveries([DiscoveryRecord(
            url=candidate.url,
            normalized_url=outcome.normalized,
            source_id=outcome.source_id,
            adapter=candidate.adapter,
            query=candidate.query,
            admitted=admitted,
            reason=outcome.reason,
        )])

    # -- top level ---------------------------------------------------------
    def run(self) -> dict[str, Any]:
        candidates, references = self.collect()
        LOGGER.info("discovery collected candidates",
                    extra={"stage": "discovery", "found": len(candidates)})

        results: list[AdmissionOutcome] = []
        queue: list[tuple[Candidate, int, str | None]] = [
            (candidate, 0, None) for candidate in candidates[: self.max_candidates]
        ]
        seen_ids: set[str] = set()
        processed_urls: set[str] = set()
        processed = 0

        while queue and processed < self.max_candidates:
            candidate, depth, parent_id = queue.pop(0)
            parsed_candidate = parse_url(candidate.url)
            if parsed_candidate is None:
                continue
            if parsed_candidate.normalized in processed_urls:
                # the same URL can arrive from several adapters and from several
                # parents - one admission decision per run is enough
                continue
            processed_urls.add(parsed_candidate.normalized)
            processed += 1
            refs = references.get(self.policy.canonical_id(candidate.url), 1)
            outcome = self.admit(candidate, refs, parent_id=parent_id, depth=depth)
            results.append(outcome)
            if outcome.source_id:
                seen_ids.add(outcome.source_id)

            if not outcome.admitted or depth >= int(self.expansion.get("max_depth", 2) or 2):
                continue
            if outcome.type != "multi" or not outcome.child_specs:
                continue
            for child in outcome.child_specs:
                if child.source_id in seen_ids:
                    continue
                queue.append((
                    Candidate(url=child.url, adapter="expansion", query=f"parent:{outcome.source_id}", name=child.name),
                    child.depth,
                    outcome.source_id,
                ))

        admitted = [item for item in results if item.admitted]
        rejected = [item for item in results if not item.admitted]
        self._write_rejected(rejected)

        report = {
            "found": len(candidates),
            "processed": processed,
            "admitted": len(admitted),
            "rejected": len(rejected),
            "expanded_children": sum(1 for item in results if item.depth > 0),
            "by_adapter": dict(Counter(item.adapter for item in results)),
            "candidates": [item.to_dict() for item in admitted],
            "rejections": [{"url": item.url, "reason": item.reason} for item in rejected][:200],
        }
        LOGGER.info(
            "discovery finished",
            extra={"stage": "discovery", "admitted": report["admitted"], "rejected": report["rejected"]},
        )
        return report

    def _write_rejected(self, rejected: list[AdmissionOutcome]) -> None:
        payload = {
            "generated_at": __import__("app.utils.timeutil", fromlist=["to_iso"]).to_iso(),
            "count": len(rejected),
            "items": [item.to_dict() for item in rejected],
        }
        try:
            self.rejected_path.parent.mkdir(parents=True, exist_ok=True)
            self.rejected_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            LOGGER.warning("could not write rejected candidates",
                           extra={"stage": "discovery", "error": str(exc)[:200]})
