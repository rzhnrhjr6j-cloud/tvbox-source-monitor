"""Generic JSON webhook channel (spec §16).

Posts a flat, documented payload.  Works directly with Slack/Discord/Feishu/
DingTalk relay endpoints and with any custom collector.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..logging_setup import get_logger

LOGGER = get_logger("alerts.webhook")


class WebhookChannel:
    name = "webhook"

    def __init__(self, cfg, client):
        section = (cfg.section_default("notifications").get("channels") or {}).get("webhook") or {}
        self.enabled = bool(section.get("enabled", False))
        self.url = os.environ.get(str(section.get("url_env") or "ALERT_WEBHOOK_URL"), "")
        self.timeout = float(section.get("timeout", 10))
        self.client = client

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.url)

    def send(self, payload: dict[str, Any]) -> bool:
        if not self.available:
            return False
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        result = self.client.request(
            "POST",
            self.url,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            data=body,
            timeout=(self.client.connect_timeout, self.timeout),
            max_bytes=128 * 1024,
            allow_redirects=False,
        )
        if not result.ok:
            LOGGER.warning("webhook delivery failed",
                           extra={"stage": "alerts", "check": self.name, "error": result.error_code or ""})
        return bool(result.ok)
