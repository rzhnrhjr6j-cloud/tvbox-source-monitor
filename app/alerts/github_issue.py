"""GitHub Issues channel (spec §16).

Creates an issue per distinct failure fingerprint and closes it again when the
source recovers.  Disabled unless ``ALERT_GITHUB_REPO`` and a token are set.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..logging_setup import get_logger

LOGGER = get_logger("alerts.github")

_API = "https://api.github.com"


class GitHubIssueChannel:
    name = "github_issue"

    def __init__(self, cfg, client, store=None):
        section = (cfg.section_default("notifications").get("channels") or {}).get("github_issue") or {}
        self.enabled = bool(section.get("enabled", False))
        self.repo = os.environ.get(str(section.get("repo_env") or "ALERT_GITHUB_REPO"), "")
        self.token = os.environ.get(str(section.get("token_env") or "GITHUB_TOKEN"), "")
        self.labels = [str(item) for item in (section.get("labels") or ["alert", "automated"])]
        self.close_on_recovery = bool(section.get("close_on_recovery", True))
        self.client = client
        self.store = store

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.repo) and bool(self.token)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _meta_key(self, fingerprint: str) -> str:
        return f"alert_issue:{fingerprint}"

    def send(self, payload: dict[str, Any]) -> bool:
        if not self.available:
            return False
        fingerprint = str(payload.get("fingerprint") or "")
        title = str(payload.get("title") or "alert")[:240]
        body = str(payload.get("message") or "")
        existing = self.store.get_meta(self._meta_key(fingerprint)) if (self.store and fingerprint) else None
        if existing and str(existing).isdigit():
            return self._comment(int(existing), body)
        result = self.client.post_json(
            f"{_API}/repos/{self.repo}/issues",
            {"title": title, "body": body, "labels": self.labels},
            headers=self._headers(),
            timeout=(self.client.connect_timeout, 20),
            max_bytes=256 * 1024,
        )
        if not result.ok:
            LOGGER.warning("github issue creation failed",
                           extra={"stage": "alerts", "check": self.name, "error": result.error_code or ""})
            return False
        try:
            number = int(result.json().get("number"))
        except (ValueError, TypeError):
            return True
        if self.store and fingerprint:
            self.store.set_meta(self._meta_key(fingerprint), str(number))
        return True

    def _comment(self, number: int, body: str) -> bool:
        result = self.client.post_json(
            f"{_API}/repos/{self.repo}/issues/{number}/comments",
            {"body": body[:60000]},
            headers=self._headers(),
            timeout=(self.client.connect_timeout, 20),
            max_bytes=128 * 1024,
        )
        return bool(result.ok)

    def resolve(self, payload: dict[str, Any]) -> bool:
        """Close the issue opened for this fingerprint (spec §17 recovery notice)."""
        if not self.available or not self.close_on_recovery:
            return False
        fingerprint = str(payload.get("fingerprint") or "")
        if not fingerprint or not self.store:
            return False
        number = self.store.get_meta(self._meta_key(fingerprint))
        if not number or not str(number).isdigit():
            return False
        result = self.client.request(
            "PATCH",
            f"{_API}/repos/{self.repo}/issues/{int(number)}",
            headers=self._headers(),
            data=json.dumps({"state": "closed"}).encode("utf-8"),
            timeout=(self.client.connect_timeout, 20),
            max_bytes=128 * 1024,
        )
        if result.ok and self.store:
            self.store.set_meta(self._meta_key(fingerprint), "")
        return bool(result.ok)
