"""SSRF guard: URLs come from untrusted pages, emails and READMEs, so every outbound target must be public."""

from __future__ import annotations

import ipaddress
import socket
from functools import lru_cache
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048


class UnsafeUrlError(ValueError):
    pass


@lru_cache(maxsize=1024)
def _resolve(host: str, port: int) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise UnsafeUrlError(f"cannot resolve host {host!r}: {error}") from error
    return tuple(sorted({info[4][0] for info in infos}))


def assertPublicUrl(url: str, allowPrivateNetwork: bool = False) -> str:
    """Returns the url unchanged if it is http(s), has no credentials, and every resolved address is public.

    Residual risk: DNS can change between this check and the connection (rebinding). Callers re-check on
    every redirect hop and sub-request, which narrows but does not close that window.
    """
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise UnsafeUrlError("url missing or too long")
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as error:
        raise UnsafeUrlError(f"malformed url: {error}") from error
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"scheme {parts.scheme!r} not allowed")
    if not parts.hostname:
        raise UnsafeUrlError("url has no host")
    if parts.username or parts.password:
        raise UnsafeUrlError("url must not embed credentials")
    if allowPrivateNetwork:
        return url
    for address in _resolve(parts.hostname, port):
        if not ipaddress.ip_address(address.split("%", 1)[0]).is_global:
            raise UnsafeUrlError(f"host {parts.hostname!r} resolves to non-public address {address}")
    return url


def isPublicUrl(url: str, allowPrivateNetwork: bool = False) -> bool:
    try:
        assertPublicUrl(url, allowPrivateNetwork)
    except UnsafeUrlError:
        return False
    return True
