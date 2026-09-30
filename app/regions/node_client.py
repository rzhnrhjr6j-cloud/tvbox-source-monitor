"""Remote probe API client - 方案 B (spec §14, §15).

Every call is token-authenticated, HMAC-signed, timestamped, bounded by a
timeout, and size-capped.  A node that is unreachable is reported as such and
never fails the surrounding run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger
from ..models import ProbeResult
from ..utils.http_client import HttpClient
from ..utils.ssrf import UnsafeURLError, guard_url
from ..utils.timeutil import to_iso

LOGGER = get_logger("regions.node")

PROBE_FIELDS = tuple(ProbeResult.__dataclass_fields__)  # type: ignore[attr-defined]


@dataclass
class NodeConfig:
    name: str
    region: str
    url: str = ""
    token: str = ""
    secret: str = ""
    weight: float = 1.0
    timeout: float = 30.0
    enabled: bool = True

    @property
    def is_remote(self) -> bool:
        return bool(self.url)


@dataclass
class NodeOutcome:
    node: NodeConfig
    probe: ProbeResult | None = None
    error: str = ""
    latency_ms: int | None = None
    http_status: int | None = None

    @property
    def ok(self) -> bool:
        return self.probe is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node.name,
            "region": self.node.region,
            "ok": self.ok,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "http_status": self.http_status,
        }


def load_nodes(cfg) -> list[NodeConfig]:
    """Read ``regions.nodes`` from config; secrets come from env vars by name."""
    nodes: list[NodeConfig] = []
    for entry in cfg.get("regions.nodes") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        region = str(entry.get("region") or "").strip()
        if not name or not url or not region:
            continue
        token = os.environ.get(str(entry.get("token_env") or ""), "") if entry.get("token_env") else ""
        secret = os.environ.get(str(entry.get("secret_env") or ""), "") if entry.get("secret_env") else ""
        nodes.append(
            NodeConfig(
                name=name,
                region=region,
                url=url,
                token=token,
                secret=secret,
                weight=float(entry.get("weight", 1.0)),
                timeout=float(entry.get("timeout", 30)),
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return nodes


def sign_body(secret: str, timestamp: str, body: bytes) -> str:
    payload = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


class NodeClient:
    def __init__(self, client: HttpClient, *, max_response_bytes: int = 65536):
        self.client = client
        self.max_response_bytes = max_response_bytes

    def probe(self, node: NodeConfig, source: dict[str, Any], options: dict[str, Any] | None = None) -> NodeOutcome:
        if not node.is_remote:
            return NodeOutcome(node=node, error="NOT_REMOTE")
        if not node.token or not node.secret:
            return NodeOutcome(node=node, error="MISSING_CREDENTIALS")

        try:
            guard_url(node.url, self.client.blocked_cidrs, self.client.blocked_hosts, self.client.allow_private)
        except UnsafeURLError as exc:
            return NodeOutcome(node=node, error=f"UNSAFE_NODE_URL: {exc}")

        payload = {
            "job": "probe",
            "region": node.region,
            "node": node.name,
            "issued_at": to_iso(),
            "source": source,
            "options": options or {},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        timestamp = to_iso()
        headers = {
            "Content-Type": "application/json",
            "X-Probe-Token": node.token,
            "X-Probe-Timestamp": timestamp,
            "X-Probe-Signature": sign_body(node.secret, timestamp, body),
            "Accept": "application/json",
        }

        result = self.client.request(
            "POST",
            node.url,
            headers=headers,
            data=body,
            timeout=(self.client.connect_timeout, node.timeout),
            max_bytes=self.max_response_bytes,
        )
        if not result.ok:
            return NodeOutcome(
                node=node,
                error=result.error_code or f"HTTP_{result.status}",
                latency_ms=result.elapsed_ms,
                http_status=result.status,
            )

        try:
            document = result.json()
        except ValueError:
            return NodeOutcome(node=node, error="NODE_BAD_JSON", http_status=result.status)

        raw_probe = document.get("probe") if isinstance(document, dict) else None
        if not isinstance(raw_probe, dict):
            return NodeOutcome(node=node, error="NODE_NO_PROBE", http_status=result.status)

        probe = _probe_from_document(raw_probe, node, source)
        if probe is None:
            return NodeOutcome(node=node, error="NODE_PROBE_UNUSABLE", http_status=result.status)
        return NodeOutcome(node=node, probe=probe, latency_ms=result.elapsed_ms, http_status=result.status)


def _probe_from_document(raw: dict[str, Any], node: NodeConfig, source: dict[str, Any]) -> ProbeResult | None:
    if str(raw.get("source_id") or "") not in ("", str(source.get("id") or "")):
        # a node must not be able to answer for a different source
        return None
    data = {key: raw[key] for key in PROBE_FIELDS if key in raw}
    data["source_id"] = str(source.get("id") or "")
    data["region"] = node.region
    data["node"] = node.name
    data.pop("score", None)
    try:
        return ProbeResult(**{**{k: None for k in ()}, **data})
    except TypeError:
        LOGGER.warning("node returned an unusable probe", extra={"node": node.name, "region": node.region})
        return None
