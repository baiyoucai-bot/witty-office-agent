"""网页抓取策略：默认允许公网；内网模式才只放行私网和白名单。"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from witty_agent.prompts import get_prompt
from witty_agent.runtime import web_settings

_PUBLIC_DEFAULTS = frozenset(
    {
        "api.openai.com",
        "api.anthropic.com",
        "generativelanguage.googleapis.com",
        "api.github.com",
        "github.com",
    }
)


def host_allowed(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().casefold()
    if not host:
        return False
    settings = web_settings()
    allow = set(settings.get("allow_hosts") or [])
    if host in allow or any(host.endswith(f".{item}") for item in allow if item):
        return True
    if _is_dangerous_host(host):
        return False
    if not settings.get("deny_public", False):
        return True
    if not settings.get("allow_private", True):
        return False
    return _is_private_host(host)


def assert_fetchable(url: str) -> None:
    if not host_allowed(url):
        raise ValueError(get_prompt("web_fetch_denied", host=urlparse(url).hostname or ""))


def is_public_default_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").strip().casefold()
    return host in _PUBLIC_DEFAULTS


def _is_dangerous_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(address.is_unspecified or address.is_multicast or address.is_link_local)


def _is_dangerous_host(host: str) -> bool:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return _is_dangerous_address(literal)
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        raw = info[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if _is_dangerous_address(address):
            return True
    return False


def _address_ok(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        return False
    if address.is_link_local:
        return False
    return bool(address.is_private or address.is_loopback)


def _is_private_host(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return _address_ok(literal)
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    seen = False
    for info in infos:
        raw = info[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        seen = True
        if not _address_ok(address):
            return False
    return seen
