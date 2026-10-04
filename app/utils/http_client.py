"""Bounded HTTP client (spec §25).

Guarantees, by construction:

* layered timeouts - connect / read / total, never an unbounded wait
* a hard response-size ceiling - stops reading past ``max_response_bytes``
* bounded retries with exponential backoff + jitter (never infinite)
* SSRF validation on the initial URL *and on every redirect hop*
* one thread-local session per worker thread (requests.Session is not
  concurrency-safe when shared)
"""

from __future__ import annotations

import json
import random
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urljoin

import requests

from ..logging_setup import get_logger
from .ssrf import DEFAULT_BLOCKED_HOSTS, UnsafeURLError, guard_url, parse_networks

LOGGER = get_logger("http")

ERR_DNS = "DNS_ERROR"
ERR_TLS = "TLS_ERROR"
ERR_TIMEOUT = "TIMEOUT"
ERR_CONNECT = "CONNECT_ERROR"
ERR_TOO_LARGE = "RESPONSE_TOO_LARGE"
ERR_SSRF = "SSRF_BLOCKED"
ERR_REDIRECT = "TOO_MANY_REDIRECTS"
ERR_TOO_SLOW = "TOTAL_TIMEOUT"
ERR_REQUEST = "REQUEST_ERROR"
ERR_DECODE = "DECODE_ERROR"

RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})
_SUCCESS_JSON = ("json",)


@dataclass
class HttpResult:
    url: str
    final_url: str = ""
    ok: bool = False
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    elapsed_ms: int = 0
    first_byte_ms: int | None = None
    attempts: int = 0
    redirected: bool = False
    dns_ok: bool = False
    tls_ok: bool = False
    truncated: bool = False
    error_code: str | None = None
    error_message: str | None = None

    @property
    def text(self) -> str:
        for encoding in (self.headers.get("content-type", "").split("charset=")[-1].strip(), "utf-8"):
            if not encoding:
                continue
            try:
                return self.content.decode(encoding, errors="strict")
            except (LookupError, UnicodeDecodeError):
                continue
        return self.content.decode("utf-8", errors="replace")

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    @property
    def size(self) -> int:
        return len(self.content)

    def json(self) -> Any:
        return json.loads(self.text)

    def error_summary(self) -> str:
        if self.error_code is None:
            return ""
        return f"{self.error_code}: {self.error_message or ''}".strip()


def _classify(exc: BaseException) -> tuple[str, bool]:
    """Map an exception to (error_code, retryable)."""
    if isinstance(exc, requests.exceptions.SSLError):
        return ERR_TLS, False
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return ERR_TIMEOUT, True
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return ERR_TIMEOUT, True
    if isinstance(exc, requests.exceptions.Timeout):
        return ERR_TIMEOUT, True
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return ERR_REDIRECT, False
    if isinstance(exc, requests.exceptions.InvalidURL):
        return ERR_REQUEST, False
    if isinstance(exc, requests.exceptions.ConnectionError):
        text = str(exc).lower()
        if "name or service not known" in text or "nodename nor servname" in text or "getaddrinfo" in text:
            return ERR_DNS, False
        if "certificate" in text or "ssl" in text:
            return ERR_TLS, False
        return ERR_CONNECT, True
    if isinstance(exc, requests.exceptions.RequestException):
        return ERR_REQUEST, False
    if isinstance(exc, socket.gaierror):
        return ERR_DNS, False
    if isinstance(exc, ssl.SSLError):
        return ERR_TLS, False
    return ERR_REQUEST, False


class HttpClient:
    """Small, predictable wrapper around requests."""

    def __init__(self, settings: Mapping[str, Any] | None = None, allow_private: bool = False):
        settings = dict(settings or {})
        self.connect_timeout = float(settings.get("connect_timeout", 5))
        self.read_timeout = float(settings.get("read_timeout", 10))
        self.total_timeout = float(settings.get("total_timeout", 20))
        self.max_retries = int(settings.get("max_retries", 3))
        self.backoff_base = float(settings.get("backoff_base", 0.5))
        self.backoff_max = float(settings.get("backoff_max", 8))
        self.jitter = float(settings.get("jitter", 0.3))
        self.max_response_bytes = int(settings.get("max_response_bytes", 5 * 1024 * 1024))
        self.max_redirects = int(settings.get("max_redirects", 5))
        self.verify_tls = bool(settings.get("verify_tls", True))
        self.user_agent = str(settings.get("user_agent", "tvbox-source-monitor/1.0"))
        self.blocked_cidrs = parse_networks(settings.get("blocked_cidrs") or ())
        self.blocked_hosts = tuple(settings.get("blocked_hosts") or DEFAULT_BLOCKED_HOSTS)
        self.allow_private = allow_private
        self._local = threading.local()

    # -- plumbing ----------------------------------------------------------
    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"})
            self._local.session = session
        return session

    def close(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()
            self._local.session = None

    def _backoff(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            delay = max(0.0, min(retry_after, self.backoff_max))
        else:
            delay = min(self.backoff_max, self.backoff_base * (2 ** max(0, attempt - 1)))
        if self.jitter:
            delay += random.uniform(0, self.jitter * max(delay, self.backoff_base))
        return delay

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    # -- the one public entry point ----------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
        json_body: Any = None,
        max_bytes: int | None = None,
        stop_after_bytes: int | None = None,
        timeout: tuple[float, float] | None = None,
        allow_redirects: bool = True,
        max_retries: int | None = None,
    ) -> HttpResult:
        ceiling = self.max_response_bytes if max_bytes is None else max_bytes
        read_timeout = timeout[1] if timeout else self.read_timeout
        connect_timeout = timeout[0] if timeout else self.connect_timeout
        deadline = time.monotonic() + self.total_timeout
        retry_limit = self.max_retries if max_retries is None else max(0, int(max_retries))

        result = HttpResult(url=url)
        last_error: str | None = None
        run_started = time.monotonic()

        for attempt in range(1, retry_limit + 2):
            result.attempts = attempt
            if time.monotonic() >= deadline:
                result.error_code = ERR_TOO_SLOW
                result.error_message = f"exceeded total timeout of {self.total_timeout}s"
                return result

            try:
                attempt_result = self._single_attempt(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    data=data,
                    json_body=json_body,
                    ceiling=ceiling,
                    stop_after_bytes=stop_after_bytes,
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    deadline=deadline,
                    allow_redirects=allow_redirects,
                )
            except UnsafeURLError as exc:
                message = str(exc)
                if message.startswith(ERR_DNS):
                    result.error_code = ERR_DNS
                    result.dns_ok = False
                else:
                    result.error_code = ERR_SSRF
                    result.dns_ok = True
                result.error_message = message
                result.attempts = attempt
                return result
            except BaseException as exc:  # noqa: BLE001 - classify() decides
                code, retryable = _classify(exc)
                last_error = code
                if not retryable or attempt > retry_limit:
                    result.error_code = code
                    result.error_message = f"{type(exc).__name__}: {exc}"[:500]
                    result.dns_ok = code != ERR_DNS
                    result.tls_ok = code != ERR_TLS
                    return result
                self._sleep(self._backoff(attempt))
                continue

            attempt_result.attempts = attempt

            if attempt_result.error_code is not None:
                retryable = attempt_result.error_code in (ERR_CONNECT, ERR_TIMEOUT, ERR_REQUEST)
                if attempt_result.status in RETRY_STATUSES:
                    retryable = True
                if retryable and attempt <= retry_limit and time.monotonic() < deadline:
                    retry_after = None
                    raw_retry = attempt_result.headers.get("retry-after")
                    if raw_retry:
                        try:
                            retry_after = float(raw_retry)
                        except ValueError:
                            retry_after = None
                    self._sleep(self._backoff(attempt, retry_after))
                    last_error = attempt_result.error_code
                    continue
                attempt_result.elapsed_ms = max(
                    attempt_result.elapsed_ms, int((time.monotonic() - deadline + self.total_timeout) * 1000)
                )
                return attempt_result

            if attempt_result.status in RETRY_STATUSES and attempt <= retry_limit and time.monotonic() < deadline:
                retry_after = None
                raw_retry = attempt_result.headers.get("retry-after")
                if raw_retry:
                    try:
                        retry_after = float(raw_retry)
                    except ValueError:
                        retry_after = None
                self._sleep(self._backoff(attempt, retry_after))
                continue

            attempt_result.elapsed_ms = max(
                attempt_result.elapsed_ms, int((time.monotonic() - run_started) * 1000)
            )
            return attempt_result

        result.error_code = last_error or ERR_REQUEST
        result.error_message = "retries exhausted"
        return result

    # -- one attempt incl. manual redirects --------------------------------
    def _single_attempt(
        self,
        method: str,
        url: str,
        *,
        headers,
        params,
        data,
        json_body,
        ceiling: int,
        stop_after_bytes,
        connect_timeout: float,
        read_timeout: float,
        deadline: float,
        allow_redirects: bool,
    ) -> HttpResult:
        current = url
        current_params = dict(params or {})
        result = HttpResult(url=url)
        started = time.monotonic()

        for hop in range(self.max_redirects + 1):
            addresses = guard_url(current, self.blocked_cidrs, self.blocked_hosts, self.allow_private)
            result.dns_ok = result.dns_ok or bool(addresses)
            if time.monotonic() >= deadline:
                result.error_code = ERR_TOO_SLOW
                result.error_message = "total timeout budget exhausted"
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
                return result

            try:
                response = self.session.request(
                    method,
                    current,
                    params=current_params or None,
                    data=data,
                    json=json_body,
                    headers=dict(headers or {}),
                    timeout=(connect_timeout, read_timeout),
                    allow_redirects=False,
                    stream=True,
                    verify=self.verify_tls,
                )
            except requests.exceptions.SSLError:
                result.tls_ok = False
                raise
            except requests.exceptions.RequestException:
                raise

            result.tls_ok = result.tls_ok or current.lower().startswith("https://")
            location = response.headers.get("location")

            if allow_redirects and response.status_code in (301, 302, 303, 307, 308) and location:
                next_url = urljoin(current, location)
                response.close()
                if hop >= self.max_redirects:
                    result.error_code = ERR_REDIRECT
                    result.error_message = f"more than {self.max_redirects} redirects"
                    result.elapsed_ms = int((time.monotonic() - started) * 1000)
                    return result
                if response.status_code == 303 or (response.status_code in (301, 302) and method.upper() == "POST"):
                    method = "GET"
                    data = None
                    json_body = None
                current = next_url
                current_params = {}
                result.redirected = True
                continue

            # ---- final response: read with a hard ceiling ----------------
            chunks: list[bytes] = []
            total = 0
            truncated = False
            first_byte_ms = None
            try:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    if first_byte_ms is None:
                        first_byte_ms = int((time.monotonic() - started) * 1000)
                    if stop_after_bytes is not None and total + len(chunk) >= stop_after_bytes:
                        chunks.append(chunk[: max(0, stop_after_bytes - total)])
                        total = stop_after_bytes
                        break
                    total += len(chunk)
                    if total > ceiling:
                        chunks.append(chunk)
                        truncated = True
                        break
                    chunks.append(chunk)
            finally:
                response.close()

            result.status = response.status_code
            result.headers = {key.lower(): value for key, value in response.headers.items()}
            result.content = b"".join(chunks)[:ceiling] if truncated else b"".join(chunks)
            result.truncated = truncated
            result.first_byte_ms = first_byte_ms
            result.final_url = current
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            result.ok = 200 <= response.status_code < 300 and not truncated

            if truncated:
                result.error_code = ERR_TOO_LARGE
                result.error_message = f"response exceeded {ceiling} bytes"
            elif response.status_code >= 400:
                result.error_code = f"HTTP_{response.status_code}"
                result.error_message = response.reason or ""
            return result

        result.error_code = ERR_REDIRECT
        result.error_message = "redirect loop"
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    # -- convenience -------------------------------------------------------
    def get(self, url: str, **kwargs) -> HttpResult:
        return self.request("GET", url, **kwargs)

    def get_json(self, url: str, **kwargs) -> HttpResult:
        kwargs.setdefault("headers", {})
        headers = dict(kwargs["headers"])
        headers.setdefault("Accept", "application/json, text/plain, */*")
        kwargs["headers"] = headers
        return self.request("GET", url, **kwargs)

    def post_json(self, url: str, payload: Any, **kwargs) -> HttpResult:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/json")
        return self.request("POST", url, headers=headers, json_body=payload, **kwargs)

    def probe_first_bytes(self, url: str, nbytes: int = 1, **kwargs) -> HttpResult:
        """Playback probe (spec §8 L5): connect + Range request, read N bytes, stop."""
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Range", f"bytes=0-{max(0, nbytes - 1)}")
        headers.setdefault("Accept", "*/*")
        # a gzip/deflate stream truncated by Range is undecodable, so ask for
        # the raw bytes - without this a 1-byte probe silently yields 0 bytes
        headers.setdefault("Accept-Encoding", "identity")
        return self.request("GET", url, headers=headers, stop_after_bytes=nbytes, **kwargs)
