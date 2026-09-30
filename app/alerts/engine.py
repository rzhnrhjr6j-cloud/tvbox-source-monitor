"""Alert engine with suppression (spec §16, §17).

Suppression rule (spec §17): a given source + failure kind alerts on first
occurrence, stays quiet for ``suppress_window_hours``, alerts again when it
recovers, and alerts again if it starts failing again afterwards.  A run can
never emit more than ``max_alerts_per_run`` alerts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..logging_setup import get_logger
from ..models import Alert, AlertKind, ProbeResult, Source
from ..utils.timeutil import to_iso
from .github_issue import GitHubIssueChannel
from .telegram import TelegramChannel
from .webhook import WebhookChannel

LOGGER = get_logger("alerts")


@dataclass
class AlertReport:
    created: int = 0
    suppressed: int = 0
    delivered: int = 0
    failed: int = 0
    resolved: int = 0
    by_channel: dict[str, int] = field(default_factory=dict)
    alerts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "suppressed": self.suppressed,
            "delivered": self.delivered,
            "failed": self.failed,
            "resolved": self.resolved,
            "by_channel": self.by_channel,
            "alerts": self.alerts[:50],
        }


def fingerprint(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class AlertEngine:
    def __init__(self, cfg, store, client):
        section = cfg.section_default("notifications")
        self.cfg = cfg
        self.store = store
        self.section = section
        self.enabled = bool(section.get("enabled", True))
        self.suppress_hours = float(section.get("suppress_window_hours", 24))
        self.alert_after = int(section.get("alert_after_consecutive_failures", 5))
        self.notify_on_recovery = bool(section.get("notify_on_recovery", True))
        self.notify_on_build_blocked = bool(section.get("notify_on_build_blocked", True))
        self.score_drop_delta = float(section.get("score_drop_alert_delta", 25))
        self.max_per_run = int(section.get("max_alerts_per_run", 20))
        self.channels = [
            WebhookChannel(cfg, client),
            TelegramChannel(cfg, client),
            GitHubIssueChannel(cfg, client, store),
        ]
        self.report = AlertReport()
        self._emitted = 0

    # -- transport ---------------------------------------------------------
    def _deliver(self, alert: Alert) -> None:
        payload = {
            "event": alert.kind,
            "source_id": alert.source_id,
            "source_name": alert.title.split(" / ")[-1] if alert.title else "",
            "region": alert.region,
            "severity": alert.severity,
            "title": alert.title,
            "message": alert.message,
            "fingerprint": alert.fingerprint,
            "created_at": alert.created_at,
            "text": f"[{alert.kind.upper()}] {alert.title}\n{alert.message}",
        }
        for channel in self.channels:
            if not getattr(channel, "available", False):
                continue
            try:
                if channel.name == "github_issue" and alert.kind == AlertKind.SOURCE_RECOVERED:
                    ok = channel.resolve(payload)
                else:
                    ok = channel.send(payload)
            except Exception as exc:  # noqa: BLE001 - alerting must never break a run
                LOGGER.error("alert channel crashed",
                             extra={"stage": "alerts", "check": channel.name, "error": str(exc)[:200]})
                ok = False
            if ok:
                alert.channels.append(channel.name)
                self.report.delivered += 1
                self.report.by_channel[channel.name] = self.report.by_channel.get(channel.name, 0) + 1
            else:
                self.report.failed += 1

    def _emit(self, alert: Alert) -> Alert | None:
        if not self.enabled:
            return None
        if self._emitted >= self.max_per_run:
            return None
        existing = self.store.recent_alert(alert.fingerprint, self.suppress_hours)
        if existing is not None:
            alert.suppressed = True
            self.report.suppressed += 1
            self.store.record_alert(alert)
            return None
        self._emitted += 1
        self._deliver(alert)
        self.store.record_alert(alert)
        self.report.created += 1
        self.report.alerts.append({
            "kind": alert.kind,
            "source_id": alert.source_id,
            "region": alert.region,
            "title": alert.title,
            "channels": list(alert.channels),
        })
        LOGGER.warning("alert raised", extra={
            "stage": "alerts", "source_id": alert.source_id or "", "region": alert.region,
            "check": alert.kind, "error": alert.message[:200],
        })
        return alert

    # -- rules -------------------------------------------------------------
    def source_failed(self, source: Source, probe: ProbeResult | None) -> Alert | None:
        if source.consecutive_failure < self.alert_after:
            return None
        error = (probe.error_code if probe else "") or source.health_level or "unknown"
        fp = fingerprint(source.id, AlertKind.SOURCE_FAILED, error)
        alert = Alert(
            source_id=source.id,
            region=(probe.region if probe else "") or "",
            kind=AlertKind.SOURCE_FAILED,
            severity="critical" if source.consecutive_failure >= 12 else "warning",
            title=f"[ALERT] Source failed / {source.name or source.id[:8]}",
            message=(
                f"Source: {source.name or '(unnamed)'}\n"
                f"URL: {source.raw_url or source.url}\n"
                f"Source ID: {source.id}\n"
                f"Region: {(probe.region if probe else '') or '-'}\n"
                f"Failure count: {source.consecutive_failure}\n"
                f"Last success: {source.last_success_at or 'never'}\n"
                f"Score: {source.score}\n"
                f"Error: {error}\n"
                f"Stage: {(probe.error_stage if probe else '') or '-'}\n"
                f"Detail: {(probe.error_message if probe else '') or ''}"
            ),
            fingerprint=fp,
        )
        return self._emit(alert)

    def source_recovered(self, source: Source) -> Alert | None:
        if not self.notify_on_recovery:
            return None
        if source.status != "active":
            return None
        if source.consecutive_success != int(self.cfg.get("lifecycle.recover_to_active", 5)):
            # only announce the moment it crosses the threshold back to ACTIVE
            return None
        fp = fingerprint(source.id, AlertKind.SOURCE_RECOVERED, "recovered")
        alert = Alert(
            source_id=source.id,
            kind=AlertKind.SOURCE_RECOVERED,
            severity="info",
            title=f"[RECOVERED] Source recovered / {source.name or source.id[:8]}",
            message=(
                f"Source: {source.name or '(unnamed)'}\n"
                f"URL: {source.raw_url or source.url}\n"
                f"Consecutive successes: {source.consecutive_success}\n"
                f"Score: {source.score}\n"
                f"Recovered at: {to_iso()}"
            ),
            fingerprint=fp,
        )
        # recovery notices are not suppressed by the failure window
        if self._emitted >= self.max_per_run:
            return None
        self._emitted += 1
        self._deliver(alert)
        self.store.record_alert(alert)
        self.report.created += 1
        # close every open failure alert for this source (spec §17 recovery notice)
        self.report.resolved += self.store.resolve_alerts_for_source(source.id, AlertKind.SOURCE_FAILED)
        return alert

    def score_drop(self, source: Source) -> Alert | None:
        if source.previous_score is None:
            return None
        drop = source.previous_score - source.score
        if drop < self.score_drop_delta:
            return None
        fp = fingerprint(source.id, AlertKind.SCORE_DROP, f"{int(source.previous_score)}->{int(source.score)}")
        alert = Alert(
            source_id=source.id,
            kind=AlertKind.SCORE_DROP,
            severity="info",
            title=f"[SCORE DROP] {source.name or source.id[:8]} -{drop:.0f}",
            message=(
                f"Source: {source.name or '(unnamed)'}\n"
                f"Previous score: {source.previous_score}\n"
                f"Current score: {source.score}\n"
                f"Drop: {drop:.1f}\n"
                f"Status: {source.status}"
            ),
            fingerprint=fp,
        )
        return self._emit(alert)

    def build_blocked(self, reason: str, detail: str = "") -> Alert | None:
        if not self.notify_on_build_blocked:
            return None
        fp = fingerprint(AlertKind.BUILD_BLOCKED, reason)
        alert = Alert(
            source_id=None,
            kind=AlertKind.BUILD_BLOCKED,
            severity="critical",
            title=f"[BUILD BLOCKED] {reason}",
            message=f"Reason: {reason}\n{detail}\nThe previously published tvbox.json has been kept (spec §19/§30/§32).",
            fingerprint=fp,
        )
        return self._emit(alert)

    def node_down(self, node_name: str, region: str, error: str) -> Alert | None:
        fp = fingerprint(node_name, AlertKind.NODE_DOWN, region)
        alert = Alert(
            source_id=None,
            region=region,
            kind=AlertKind.NODE_DOWN,
            severity="warning",
            title=f"[NODE DOWN] {node_name} ({region})",
            message=f"Probe node {node_name} in region {region} failed: {error}",
            fingerprint=fp,
        )
        return self._emit(alert)

    def output_rollback(self, reason: str) -> Alert | None:
        fp = fingerprint(AlertKind.OUTPUT_ROLLBACK, reason)
        alert = Alert(
            source_id=None,
            kind=AlertKind.OUTPUT_ROLLBACK,
            severity="critical",
            title="[ROLLBACK] kept last-known-good tvbox.json",
            message=f"Reason: {reason}",
            fingerprint=fp,
        )
        return self._emit(alert)

    def run_rules(self, sources: Iterable[Source], probes: dict[str, ProbeResult]) -> AlertReport:
        for source in sources:
            probe = probes.get(source.id)
            self.source_failed(source, probe)
            self.source_recovered(source)
            self.score_drop(source)
        return self.report
