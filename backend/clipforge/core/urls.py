"""Validation of user-supplied media links.

The platform fetches arbitrary URLs on the server, which is a classic SSRF vector
(cloud metadata endpoints, internal admin panels, the database). Two layers:

* ``parse_media_url`` — cheap syntactic checks used by the API at submit time.
* ``assert_public_host`` — DNS resolution check run by the worker right before
  downloading; every resolved address must be globally routable.

Deployment adds the third layer: workers run on a network with no route to internal
services (see docs/DEPLOYMENT.md).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit


class UnsafeURLError(ValueError):
    pass


_PLATFORMS: list[tuple[str, tuple[str, ...]]] = [
    ("youtube", ("youtube.com", "youtu.be", "youtube-nocookie.com")),
    ("twitch", ("twitch.tv",)),
    ("kick", ("kick.com",)),
    ("tiktok", ("tiktok.com",)),
    ("instagram", ("instagram.com",)),
    ("x", ("x.com", "twitter.com")),
    ("facebook", ("facebook.com", "fb.watch")),
    ("vimeo", ("vimeo.com",)),
    ("rumble", ("rumble.com",)),
    ("reddit", ("reddit.com", "redd.it")),
    ("snapchat", ("snapchat.com",)),
    ("linkedin", ("linkedin.com",)),
]

_URL_SPLIT = re.compile(r"[\s,]+")


def detect_platform(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for name, domains in _PLATFORMS:
        if any(host == d or host.endswith("." + d) for d in domains):
            return name
    return "web"


def parse_media_url(raw: str) -> str:
    url = raw.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError("Only http(s) links are supported.")
    if not parts.hostname:
        raise UnsafeURLError("The link has no host name.")
    if parts.username or parts.password:
        raise UnsafeURLError("Links with embedded credentials are not allowed.")
    if len(url) > 2048:
        raise UnsafeURLError("The link is too long.")
    try:
        _ = parts.port  # urlsplit validates the port lazily
    except ValueError as exc:
        raise UnsafeURLError("The link has an invalid port.") from exc
    host = parts.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:  # "localhost", "javascript:..." and other single-label names
            raise UnsafeURLError("The link must point to a public website.") from None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def split_url_list(text: str) -> list[str]:
    """Split a pasted blob (newlines, spaces, commas) into unique links, order preserved."""
    seen: dict[str, None] = {}
    for token in _URL_SPLIT.split(text):
        if token:
            seen.setdefault(token, None)
    return list(seen)


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def assert_public_host(url: str, *, allow_private: bool = False) -> None:
    host = urlsplit(url).hostname
    if not host:
        raise UnsafeURLError("The link has no host name.")
    if allow_private:
        return
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [literal]
    else:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeURLError(f"Could not resolve host {host!r}.") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or not all(_is_public(a) for a in addresses):
        raise UnsafeURLError("Links to private or internal network addresses are not allowed.")
