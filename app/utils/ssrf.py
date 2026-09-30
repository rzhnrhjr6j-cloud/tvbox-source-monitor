"""SSRF protection (spec §15).

Every outbound request - including each redirect hop - must pass through
:func:`guard_url`.  A probe node or the main runner must never be tricked into
touching loopback, RFC1918, link-local or cloud metadata endpoints.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urlsplit

DEFAULT_BLOCKED_HOSTS = (
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
)

_DNS_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}
_DNS_TTL_SECONDS = 300.0


class UnsafeURLError(ValueError):
    """Raised when a URL targets a host we are not allowed to contact."""


def parse_networks(values) -> tuple[ipaddress._BaseNetwork, ...]:
    networks = []
    for value in values or ():
        try:
            networks.append(ipaddress.ip_network(str(value), strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _resolve(host: str) -> tuple[str, ...]:
    now = time.time()
    cached = _DNS_CACHE.get(host)
    if cached and now - cached[0] < _DNS_TTL_SECONDS:
        return cached[1]
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"DNS_ERROR: cannot resolve {host}: {exc}") from exc
    addresses = tuple(sorted({info[4][0] for info in infos}))
    _DNS_CACHE[host] = (now, addresses)
    return addresses


def clear_dns_cache() -> None:
    _DNS_CACHE.clear()


def _blocked(address: str, networks) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    # is_private covers RFC1918 (10/8, 172.16/12, 192.168/16) and the IPv6
    # unique-local range.  It must be here: node/probe.py calls guard_url()
    # without a CIDR list, so anything missing from this line is reachable.
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    for network in networks:
        if ip.version == network.version and ip in network:
            return True
    return False


def guard_url(
    url: str,
    blocked_cidrs=(),
    blocked_hosts=DEFAULT_BLOCKED_HOSTS,
    allow_private: bool = False,
    resolve: bool = True,
) -> list[str]:
    """Validate ``url``; return the resolved addresses.

    Raises :class:`UnsafeURLError` when the target is not permitted.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsafeURLError(f"BAD_URL: {url!r}") from exc

    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"BAD_SCHEME: {parts.scheme!r}")

    raw_host = parts.hostname or ""
    host = raw_host.lower().rstrip(".")
    if not host:
        raise UnsafeURLError("NO_HOST")

    if not allow_private:
        for blocked in blocked_hosts or ():
            if host == str(blocked).lower() or host.endswith("." + str(blocked).lower()):
                raise UnsafeURLError(f"BLOCKED_HOST: {host}")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    networks = parse_networks(blocked_cidrs)
    if literal is not None:
        if not allow_private and _blocked(str(literal), networks):
            raise UnsafeURLError(f"BLOCKED_IP: {literal}")
        return [str(literal)]

    if not resolve:
        return []

    addresses = _resolve(host)
    if not addresses:
        raise UnsafeURLError(f"DNS_ERROR: no address for {host}")
    if not allow_private:
        for address in addresses:
            if _blocked(address, networks):
                raise UnsafeURLError(f"BLOCKED_IP: {host} -> {address}")
    return list(addresses)


def is_safe(url: str, blocked_cidrs=(), blocked_hosts=DEFAULT_BLOCKED_HOSTS) -> bool:
    try:
        guard_url(url, blocked_cidrs, blocked_hosts)
        return True
    except UnsafeURLError:
        return False
