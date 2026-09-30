#!/usr/bin/env python3
"""Multi-region probe node (spec §12 - §15).

Two deployment shapes, one file:

  A. self-hosted runner (recommended long term)
       python node/probe.py --once --region HK
     probes one source and prints the ProbeResult JSON on stdout.  Wire it into
     a workflow job that runs on a self-hosted runner in that region.  No
     inbound port, no credentials.

  B. remote probe API
       PROBE_TOKEN=... PROBE_SECRET=... python node/probe.py --serve --region JP
     exposes ``POST /probe`` for the main GitHub Action to call.

Security controls on the HTTP surface (spec §15): token auth, HMAC request
signature, timestamp replay window, per-token rate limit, request body cap,
response body cap, request timeout, and an SSRF guard that refuses loopback /
RFC1918 / link-local / cloud-metadata targets.

The node deliberately re-uses ``app/`` so a probe performed here is bit-for-bit
the same check the runner performs.  Deploy the repository (or the wheel) with
the node; ``--once`` needs nothing else.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from app.checks.runner import CheckOptions, run_checks
    from app.config import load_config
    from app.models import HealthLevel, ProbeResult, Source
    from app.utils.http_client import HttpClient
    from app.utils.ssrf import UnsafeURLError, guard_url
    from app.utils.timeutil import parse_iso, to_iso, utcnow
except ImportError as exc:  # pragma: no cover - deployment guard
    print(
        "cannot import the `app` package - deploy the repository (or install the wheel) "
        f"next to node/probe.py.  Original error: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(3)

REPLAY_WINDOW_SECONDS = 300
MAX_REQUEST_BYTES = 64 * 1024
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "60"))


# ---------------------------------------------------------------------------
# shared probe logic
# ---------------------------------------------------------------------------
def make_source(payload: dict[str, Any]) -> Source:
    return Source(
        id=str(payload.get("id") or ""),
        url=str(payload.get("url") or ""),
        raw_url=str(payload.get("url") or ""),
        name=str(payload.get("name") or ""),
        type=str(payload.get("type") or "single"),
    )


def probe_source(
    payload: dict[str, Any],
    *,
    region: str,
    node: str,
    options: CheckOptions | None = None,
    allow_private: bool = False,
    cfg=None,
) -> ProbeResult:
    cfg = cfg or load_config(explicit_root=str(REPO_ROOT))
    options = options or CheckOptions.from_config(cfg)
    http_settings = dict(cfg.section("http"))
    # a probe node must not be talked into scanning its own network
    client = HttpClient(http_settings, allow_private=allow_private)
    try:
        return run_checks(make_source(payload), client, options, region=region, node=node)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# mode A: one shot
# ---------------------------------------------------------------------------
def run_once(args) -> int:
    if not args.url:
        print("--once requires --url", file=sys.stderr)
        return 2
    region = args.region or os.environ.get("PROBE_REGION") or "CN"
    node = args.node or os.environ.get("PROBE_NODE_NAME") or "node"
    payload = {"id": args.source_id or "", "url": args.url, "name": args.node or "", "type": "single"}
    try:
        result = probe_source(payload, region=region, node=node, allow_private=args.allow_private)
    except Exception as exc:  # noqa: BLE001 - a node must always answer
        result = ProbeResult(source_id=args.source_id, region=region, node=node)
        result.error_code = "NODE_ERROR"
        result.error_stage = "L0_node"
        result.error_message = str(exc)[:500]
        result.health_level = HealthLevel.FAILED
    document = {
        "source_id": result.source_id,
        "region": region,
        "node": node,
        "probe": {key: value for key, value in result.to_row().items() if key != "source_id"},
        "issued_at": to_iso(),
    }
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# mode B: HTTP probe API
# ---------------------------------------------------------------------------
class RateLimiter:
    """Simple per-token sliding window."""

    def __init__(self, limit_per_minute: int):
        self.limit = max(1, limit_per_minute)
        self._events: dict[str, list[float]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            window = [stamp for stamp in self._events.get(key, []) if now - stamp < 60.0]
            if len(window) >= self.limit:
                self._events[key] = window
                return False
            window.append(now)
            self._events[key] = window
            return True


class ProbeHandler(BaseHTTPRequestHandler):
    server_version = "TvboxProbeNode/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if getattr(self.server, "quiet", False):  # type: ignore[attr-defined]
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers -----------------------------------------------------------
    @property
    def settings(self) -> dict[str, Any]:
        return self.server.settings  # type: ignore[attr-defined]

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_key(self) -> str:
        return self.headers.get("X-Probe-Token", "") or self.client_address[0]

    # -- routes ------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/healthz", "/health"):
            return self._json({
                "ok": True,
                "node": self.settings["node"],
                "region": self.settings["region"],
                "time": to_iso(),
                "auth_required": bool(self.settings["token"]),
            })
        return self._json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") != "/probe":
            return self._json({"error": "not found"}, status=404)

        # ---- body size cap --------------------------------------------
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "bad content-length"}, status=400)
        if length <= 0:
            return self._json({"error": "empty body"}, status=400)
        if length > MAX_REQUEST_BYTES:
            return self._json({"error": "request too large"}, status=413)
        body = self.rfile.read(length)

        # ---- auth ------------------------------------------------------
        if not self._authenticate(body):
            return self._json({"error": "unauthorized"}, status=401)

        # ---- rate limit -------------------------------------------------
        if not self.server.limiter.allow(self._client_key()):  # type: ignore[attr-defined]
            return self._json({"error": "rate limited"}, status=429)

        # ---- payload ----------------------------------------------------
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json({"error": "invalid json"}, status=400)
        if not isinstance(document, dict):
            return self._json({"error": "payload must be an object"}, status=400)

        source = document.get("source") or {}
        url = str(source.get("url") or "")
        if not url:
            return self._json({"error": "source.url is required"}, status=400)

        # ---- SSRF guard: the node must not be usable as a scanner --------
        if not self.settings["allow_private"]:
            try:
                guard_url(url)
            except UnsafeURLError as exc:
                return self._json({"error": f"source url rejected: {exc}"}, status=403)
        allowlist = self.settings["allowlist"]
        if allowlist and not any(url.startswith(prefix) for prefix in allowlist):
            return self._json({"error": "source url not in allowlist"}, status=403)

        region = str(document.get("region") or self.settings["region"])
        node = str(document.get("node") or self.settings["node"])

        started = time.monotonic()
        try:
            result = probe_source(
                source,
                region=region,
                node=node,
                allow_private=self.settings["allow_private"],
                cfg=self.settings["cfg"],
            )
        except Exception as exc:  # noqa: BLE001
            result = ProbeResult(source_id=str(source.get("id") or ""), region=region, node=node)
            result.error_code = "NODE_ERROR"
            result.error_stage = "L0_node"
            result.error_message = str(exc)[:500]
            result.health_level = HealthLevel.FAILED

        elapsed = int((time.monotonic() - started) * 1000)
        if elapsed > PROBE_TIMEOUT * 1000:
            # the probe finished but exceeded the node budget - report it
            result.error_message = ((result.error_message or "") + f" | node budget exceeded ({elapsed}ms)")[:500]

        return self._json({
            "ok": True,
            "node": node,
            "region": region,
            "elapsed_ms": elapsed,
            "issued_at": to_iso(),
            "probe": {key: value for key, value in result.to_row().items() if key != "source_id"},
        })

    def _authenticate(self, body: bytes) -> bool:
        token = self.settings["token"]
        secret = self.settings["secret"]
        if not token and not secret:
            return True
        provided = self.headers.get("X-Probe-Token", "")
        if not token or not hmac.compare_digest(provided, token):
            return False
        if not secret:
            return True
        timestamp = self.headers.get("X-Probe-Timestamp", "")
        signature = self.headers.get("X-Probe-Signature", "")
        moment = parse_iso(timestamp)
        if moment is None:
            return False
        skew = abs((utcnow() - moment).total_seconds())
        if skew > REPLAY_WINDOW_SECONDS:
            return False
        expected = hmac.new(
            secret.encode("utf-8"),
            timestamp.encode("utf-8") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(provided_signature := signature, expected) and bool(provided_signature)


def run_serve(args) -> int:
    cfg = load_config(explicit_root=str(REPO_ROOT))
    region = args.region or os.environ.get("PROBE_REGION") or "CN"
    node = args.node or os.environ.get("PROBE_NODE_NAME") or f"node-{region.lower()}"
    token = os.environ.get("PROBE_TOKEN", "")
    secret = os.environ.get("PROBE_SECRET", "")
    if not token or not secret:
        print("refusing to serve without PROBE_TOKEN and PROBE_SECRET (spec §15)", file=sys.stderr)
        return 2

    allowlist = [item.strip() for item in os.environ.get("PROBE_ALLOWLIST", "").split(",") if item.strip()]
    server = ThreadingHTTPServer((args.host, args.port), ProbeHandler)
    server.daemon_threads = True
    server.settings = {  # type: ignore[attr-defined]
        "cfg": cfg,
        "region": region,
        "node": node,
        "token": token,
        "secret": secret,
        "allow_private": bool(args.allow_private),
        "allowlist": allowlist,
    }
    server.limiter = RateLimiter(int(os.environ.get("PROBE_RATE_LIMIT", "60")))  # type: ignore[attr-defined]
    server.quiet = bool(args.quiet)  # type: ignore[attr-defined]

    print(f"probe node '{node}' listening on {args.host}:{args.port} (region {region})", file=sys.stderr)
    if allowlist:
        print(f"source allowlist: {allowlist}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="probe", description="tvbox-source-monitor probe node")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="probe one source, print JSON (mechanism A)")
    mode.add_argument("--serve", action="store_true", help="serve the HTTP probe API (mechanism B)")
    parser.add_argument("--region", default=None)
    parser.add_argument("--node", default=None, help="node name")
    parser.add_argument("--url", default=None, help="source URL for --once")
    parser.add_argument("--source-id", default="", help="source id for --once")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--allow-private", action="store_true", help="testing only: permit private targets")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return run_once(args) if args.once else run_serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
