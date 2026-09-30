"""A real (not mocked) local origin used by the end-to-end tests.

It behaves like a small 苹果CMS-flavoured source: a config endpoint, a search
endpoint, a detail endpoint and a tiny media endpoint.  Handlers can be flipped
to "down" so tests exercise real failures over real sockets.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

SITE_TEMPLATE = {
    "key": "local-demo",
    "name": "Local Demo",
    "type": 3,
    "api": "{base}/api.php/provide/vod/",
    "searchable": 1,
    "quickSearch": 1,
    "filterable": 1,
}

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)", re.IGNORECASE)

MIRRORED_JAR = b"PK\x03\x04 a crawler jar reached through the proxy"

# A sibling reference of the kind real configs carry: always reachable from a
# GitHub runner, never reachable from the client we publish for.
INNER_RAW = "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"


def build_config(base: str) -> dict:
    site = dict(SITE_TEMPLATE)
    site["api"] = SITE_TEMPLATE["api"].format(base=base)
    return {"sites": [site], "lives": [], "parses": []}


class _Handler(BaseHTTPRequestHandler):
    server_version = "LocalSource/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    # -- helpers -----------------------------------------------------------
    @property
    def state(self) -> dict:
        return self.server.state  # type: ignore[attr-defined]

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def _send(self, body: bytes, status=200, content_type="application/json; charset=utf-8",
              extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload, status=200) -> None:
        self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"), status)

    def _send_rangeable(self, body: bytes, content_type: str) -> None:
        match = _RANGE_RE.match(self.headers.get("Range") or "")
        if not match:
            self._send(body, 200, content_type)
            return
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else len(body) - 1
        start = min(start, max(len(body) - 1, 0))
        end = min(end, len(body) - 1)
        if end < start:
            self._send(b"", 416, content_type)
            return
        chunk = body[start:end + 1]
        self._send(chunk, 206, content_type, {"Content-Range": f"bytes {start}-{end}/{len(body)}"})

    # -- routing -----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        return self._route()

    def do_HEAD(self):  # noqa: N802
        return self._route()

    def _route(self):
        state = self.state
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)

        if state.get("request_log") is not None:
            state["request_log"].append(self.path)
        if state.get("delay"):
            time.sleep(state["delay"])

        if path == "/config.json":
            if not state["config_up"]:
                return self._send_json({"error": "service unavailable"}, status=503)
            return self._send_rangeable(
                json.dumps(build_config(self.base), ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        if path == "/api.php/provide/vod/":
            if not state["search_up"]:
                return self._send_json({"code": 0, "list": [], "total": 0, "msg": "search disabled"})
            if query.get("ids"):
                return self._send_json({
                    "code": 1,
                    "list": [{
                        "vod_id": query["ids"][0],
                        "vod_name": "Local Demo Item",
                        "vod_play_from": "line1",
                        "vod_play_url": f"第1集${self.base}/media/1.m3u8#第2集${self.base}/media/2.m3u8",
                    }],
                    "total": 1,
                })
            keyword = (query.get("wd") or [""])[0]
            if not keyword:
                return self._send_json({"code": 0, "list": [], "total": 0})
            return self._send_json({
                "code": 1,
                "list": [
                    {"vod_id": 1001, "vod_name": f"{keyword} 01", "vod_play_url": ""},
                    {"vod_id": 1002, "vod_name": f"{keyword} 02", "vod_play_url": ""},
                    {"vod_id": 1003, "vod_name": f"{keyword} 03", "vod_play_url": ""},
                ],
                "total": 3,
            })

        if path == "/media/1.m3u8":
            if not state["media_up"]:
                return self._send(b"", 404, "text/plain")
            return self._send_rangeable(b"#EXTM3U\n#EXT-X-VERSION:3\n", "application/vnd.apple.mpegurl")

        if path == "/media/2.m3u8":
            if not state["media_up"]:
                return self._send(b"", 404, "text/plain")
            return self._send_rangeable(b"#EXTM3U\n", "application/vnd.apple.mpegurl")

        if path == "/multi.json":
            if not state["config_up"]:
                return self._send_json({"error": "service unavailable"}, status=503)
            return self._send_rangeable(
                json.dumps(
                    {
                        "urls": [
                            {"name": "Local Demo", "url": f"{self.base}/config.json"},
                            {"name": "Broken Child", "url": f"{self.base}/broken.json"},
                            {"name": "Self Reference", "url": f"{self.base}/multi.json"},
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        if path == "/broken.json":
            return self._send(b"<!doctype html><html><body>not a config</body></html>",
                              content_type="text/html; charset=utf-8")

        if path == "/inner.json":
            if not state["config_up"]:
                return self._send_json({"error": "service unavailable"}, status=503)
            return self._send(
                json.dumps(
                    {
                        "sites": [dict(SITE_TEMPLATE, api=f"{self.base}/api.php/provide/vod/")],
                        "urls": [{"name": "Child", "url": INNER_RAW}],
                        "lives": [{"name": "Live", "url": INNER_RAW}],
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                content_type="application/json; charset=utf-8",
            )

        if path == "/notjson.json":
            return self._send(b"this is definitely not json", content_type="application/json")

        if path == "/assets/spider.jar":
            return self._send(b"PK\x03\x04 fake jar payload", content_type="application/octet-stream")

        if path == "/withjar.json":
            if not state["config_up"]:
                return self._send_json({"error": "service unavailable"}, status=503)
            return self._send(
                json.dumps(
                    {
                        "spider": "./assets/spider.jar;md5;deadbeef",
                        "sites": [dict(SITE_TEMPLATE, api=f"{self.base}/api.php/provide/vod/")],
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                content_type="application/json; charset=utf-8",
            )

        # A config whose crawler jars have gone away, the way a deleted author
        # repository looks to the client: four sites, three of which cannot open
        # - one on a 404 jar, one on a dead top-level spider (csp_* sites need
        # it even though they carry no jar of their own), one on a live jar.
        if path == "/deadjars.json":
            if not state["config_up"]:
                return self._send_json({"error": "service unavailable"}, status=503)
            return self._send(
                json.dumps(
                    {
                        "spider": f"{self.base}/assets/missing.jar",
                        "sites": [
                            dict(SITE_TEMPLATE, key="dead-jar", name="Dead Jar",
                                 api="csp_DeadJar",
                                 jar=f"{self.base}/assets/missing.jar"),
                            dict(SITE_TEMPLATE, key="live-jar", name="Live Jar",
                                 api="csp_LiveJar",
                                 jar=f"{self.base}/assets/spider.jar"),
                            dict(SITE_TEMPLATE, key="csp", name="Csp Site", api="csp_Dead"),
                            dict(SITE_TEMPLATE, key="plain", name="Plain Site",
                                 api=f"{self.base}/api.php/provide/vod/"),
                        ],
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                content_type="application/json; charset=utf-8",
            )

        if path == "/alljarsdead.json":
            if not state["config_up"]:
                return self._send_json({"error": "service unavailable"}, status=503)
            return self._send(
                json.dumps(
                    {
                        "sites": [dict(SITE_TEMPLATE, key="only", name="Only Site",
                                       api=f"{self.base}/api.php/provide/vod/",
                                       jar=f"{self.base}/assets/missing.jar")],
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                content_type="application/json; charset=utf-8",
            )

        # Stands in for an acceleration proxy: /proxy/<anything> answers with a
        # jar, the way https://<proxy>/https://raw.github…/x.jar does, so the
        # jar-hosting path can be exercised without reaching GitHub.
        if path.startswith("/proxy/"):
            if not state["config_up"]:
                return self._send(b"", 503, "text/plain")
            return self._send(MIRRORED_JAR, content_type="application/octet-stream")

        if path == "/proxiedjar.json":
            if not state["config_up"]:
                return self._send_json({"error": "service unavailable"}, status=503)
            return self._send(
                json.dumps(
                    {
                        "spider": "https://raw.githubusercontent.com/Some/Repo/main/spider.jar;md5;cafe",
                        "sites": [dict(SITE_TEMPLATE, key="csp", name="Csp Site", api="csp_X")],
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                content_type="application/json; charset=utf-8",
            )

        return self._send(b"", 404, "text/plain")


class LocalSourceServer:
    """Context manager serving the fake origin on an ephemeral port."""

    def __init__(self, *, config_up=True, search_up=True, media_up=True, delay=0.0, log_requests=True):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.state = {  # type: ignore[attr-defined]
            "config_up": config_up,
            "search_up": search_up,
            "media_up": media_up,
            "delay": delay,
            "request_log": [] if log_requests else None,
        }
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "LocalSourceServer":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def config_url(self) -> str:
        return f"{self.base}/config.json"

    @property
    def state(self) -> dict:
        return self._server.state  # type: ignore[attr-defined]

    @property
    def requests(self) -> list[str]:
        return list(self.state.get("request_log") or [])

    def set(self, **kwargs) -> None:
        self.state.update(kwargs)
