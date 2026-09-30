"""L1 - HTTP reachability (spec §8).

DNS, TCP, TLS, HTTP status and wall-clock latency for the config endpoint.
"""

from __future__ import annotations

from typing import Any

from ..utils.http_client import ERR_DNS, ERR_SSRF, ERR_TLS, ERR_TOO_LARGE, ERR_TOO_SLOW, HttpResult


def check_http(
    url: str,
    client,
    *,
    timeout: float | None = None,
    max_bytes: int | None = None,
) -> tuple[dict[str, Any], HttpResult]:
    """L1: fetch the config endpoint.  Returns ``(fields, raw_result)``."""
    layer_timeout = timeout or client.read_timeout
    result: HttpResult = client.request(
        "GET",
        url,
        timeout=(client.connect_timeout, layer_timeout),
        max_bytes=max_bytes,
        headers={"Accept": "application/json, text/plain, */*"},
    )
    return http_fields(result), result


def http_fields(result: HttpResult, previous_error: str | None = None) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "http_ok": bool(result.ok),
        "status_code": result.status,
        "dns_ok": bool(result.dns_ok),
        "tls_ok": bool(result.tls_ok),
        "response_ms": result.elapsed_ms or None,
    }
    if result.error_code:
        fields["error_code"] = result.error_code
        fields["error_message"] = (result.error_message or "")[:500]
    elif previous_error:
        fields["error_code"] = previous_error
    if result.error_code == ERR_DNS:
        fields["dns_ok"] = False
    if result.error_code == ERR_TLS:
        fields["tls_ok"] = False
    if result.error_code == ERR_SSRF:
        fields["dns_ok"] = False
    return fields


def is_fatal_transport_error(code: str | None) -> bool:
    """Errors that mean "do not even try deeper layers"."""
    return code in (ERR_DNS, ERR_TLS, ERR_SSRF, ERR_TOO_SLOW, ERR_TOO_LARGE)
