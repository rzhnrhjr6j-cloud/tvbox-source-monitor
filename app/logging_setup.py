"""Structured logging.

Spec §35-13 requires that every log line can be traced back to
``source_id`` + ``region`` + ``check type``.  Both the JSON and the text
formatter therefore always append those fields when they are present.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

TRACE_FIELDS = ("source_id", "source_name", "region", "node", "check", "url", "stage", "event")

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "message", "asctime",
}


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {key: value for key, value in record.__dict__.items() if key not in _RESERVED}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for field in TRACE_FIELDS:
            value = record.__dict__.get(field)
            if value not in (None, ""):
                payload[field] = value
        extras = _extras(record)
        if extras:
            payload["extra"] = extras
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = self.formatTime(record, "%H:%M:%S")
        parts = [f"{stamp} {record.levelname:<7} {record.name:<28} {record.getMessage()}"]
        for field in TRACE_FIELDS:
            value = record.__dict__.get(field)
            if value not in (None, ""):
                parts.append(f"{field}={value}")
        extras = _extras(record)
        if extras:
            parts.append(" ".join(f"{key}={value}" for key, value in extras.items()))
        line = "  ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(level: str = "INFO", fmt: str = "json", stream=None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    # urllib3 is chatty at DEBUG and leaks nothing useful
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class LogContext(logging.LoggerAdapter):
    """``with_context(source_id=..., region=...)`` for per-source log lines."""

    def process(self, msg, kwargs):  # type: ignore[override]
        extras = dict(self.extra)
        extras.update(kwargs.get("extra") or {})
        kwargs["extra"] = extras
        return msg, kwargs


def with_context(logger: logging.Logger, **context: Any) -> LogContext:
    return LogContext(logger, context)
