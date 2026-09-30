"""Telegram channel (spec §16)."""

from __future__ import annotations

import os
from typing import Any

from ..logging_setup import get_logger

LOGGER = get_logger("alerts.telegram")


class TelegramChannel:
    name = "telegram"

    def __init__(self, cfg, client):
        section = (cfg.section_default("notifications").get("channels") or {}).get("telegram") or {}
        self.enabled = bool(section.get("enabled", False))
        self.token = os.environ.get(str(section.get("bot_token_env") or "TELEGRAM_BOT_TOKEN"), "")
        self.chat_id = os.environ.get(str(section.get("chat_id_env") or "TELEGRAM_CHAT_ID"), "")
        self.client = client

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.token) and bool(self.chat_id)

    def send(self, payload: dict[str, Any]) -> bool:
        if not self.available:
            return False
        text = payload.get("text") or payload.get("title") or "alert"
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        result = self.client.post_json(
            url,
            {"chat_id": self.chat_id, "text": str(text)[:4000], "disable_web_page_preview": True},
            timeout=(self.client.connect_timeout, 10),
            max_bytes=64 * 1024,
        )
        if not result.ok:
            LOGGER.warning("telegram delivery failed",
                           extra={"stage": "alerts", "check": self.name, "error": result.error_code or ""})
        return bool(result.ok)
