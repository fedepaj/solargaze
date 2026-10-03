"""
A second opinion on DNS, for a network whose resolver drops names for
minutes at a time.

Imported for its side effect: socket.getaddrinfo first asks the system as
usual; when that fails, it asks Google's DNS-over-HTTPS service at the IP
8.8.8.8 (no name to resolve, so it works when names do not), and keeps
every answer for ten minutes. It covers whatever Python opens — requests,
urllib3, pystac — and not GDAL, whose curl resolves on its own; that is why
the rasters are downloaded with requests and read from disk.

Nothing changes on a network that works: the system answers first.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import ssl
import threading
import time
import urllib.request

log = logging.getLogger("netdns")

_system = socket.getaddrinfo
_cache: dict[str, tuple[float, list[str]]] = {}
_lock = threading.Lock()
TTL = 600


def _doh(host: str) -> list[str]:
    url = f"https://8.8.8.8/resolve?name={host}&type=A"
    with urllib.request.urlopen(url, timeout=10, context=ssl.create_default_context()) as r:
        answer = json.load(r).get("Answer", [])
    return [a["data"] for a in answer if a.get("type") == 1]


def _literal(host) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except (ValueError, TypeError):
        return False


def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if isinstance(host, str) and _literal(host):
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        return [(fam, type or socket.SOCK_STREAM, proto, "", (host, port))]
    try:
        res = _system(host, port, family, type, proto, flags)
        if isinstance(host, str) and res:
            ips = [r[4][0] for r in res if r[0] == socket.AF_INET]
            if ips:
                with _lock:
                    _cache[host] = (time.time(), ips)
        return res
    except socket.gaierror:
        if not isinstance(host, str):
            raise
        with _lock:
            hit = _cache.get(host)
        ips = hit[1] if hit and time.time() - hit[0] < TTL else None
        if not ips:
            try:
                ips = _doh(host)
            except Exception:
                ips = None
            if not ips:
                raise
            with _lock:
                _cache[host] = (time.time(), ips)
            log.info("resolved %s over HTTPS: %s", host, ips[0])
        return [(socket.AF_INET, type or socket.SOCK_STREAM, proto, "", (ip, port)) for ip in ips]


socket.getaddrinfo = getaddrinfo
