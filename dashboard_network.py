"""Lightweight network and access-URL helpers for the dashboard CLI."""

from __future__ import annotations

import json
import re
import ssl
import subprocess
import sys
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_HOME_LAN_PREFIXES = ("10.0.", "192.168.")


def _is_private(ip: str) -> bool:
    try:
        a, b, *_ = (int(x) for x in ip.split("."))
    except ValueError:
        return False
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    return False


def _routes_to_local_host(ip: str, expected_iface: str = "") -> bool:
    """Return True when connecting to this local IP routes back here."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["route", "-n", "get", ip], capture_output=True, text=True,
                timeout=2,
            ).stdout
        except (subprocess.SubprocessError, FileNotFoundError):
            return True
        iface_match = re.search(r"interface:\s+(\S+)", out)
        route_iface = iface_match.group(1) if iface_match else ""
        if (expected_iface and route_iface == expected_iface
                and re.search(r"flags:\s*<[^>]*\bHOST\b", out)):
            return True
        return (
            re.search(r"interface:\s+lo0\b", out) is not None
            or re.search(r"gateway:\s+127\.0\.0\.1\b", out) is not None
            or re.search(r"flags:.*\bLOCAL\b", out) is not None
        )
    if sys.platform.startswith("linux"):
        try:
            out = subprocess.run(
                ["ip", "route", "get", ip], capture_output=True, text=True,
                timeout=2,
            ).stdout
        except (subprocess.SubprocessError, FileNotFoundError):
            return True
        return bool(
            re.search(r"\blocal\b.*\bdev\s+lo\b", out)
            or re.search(r"\bdev\s+lo\b", out)
        )
    return True


def list_local_ipv4() -> list[tuple[str, str]]:
    """Return prioritized, reachable non-loopback IPv4 interfaces."""
    try:
        out = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=3,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    results: list[tuple[str, str]] = []
    current_iface = ""
    current_iface_up = False
    for line in out.splitlines():
        if line and not line[0].isspace():
            current_iface = line.split(":", 1)[0]
            flags = line.split("<", 1)[1].split(">", 1)[0].split(",") if "<" in line else []
            current_iface_up = "UP" in flags
            continue
        line = line.strip()
        if line.startswith("inet ") and not line.startswith("inet 127."):
            if not current_iface_up:
                continue
            ip = line.split()[1].split("%", 1)[0]
            if _routes_to_local_host(ip, current_iface):
                results.append((current_iface, ip))

    def score(row: tuple[str, str]) -> int:
        iface, ip = row
        if iface.startswith(("utun", "tun", "tap", "ppp", "cscotun", "gpd")):
            return 15 if any(ip.startswith(p) for p in _HOME_LAN_PREFIXES) else 10
        if iface.startswith(("en", "bridge")):
            return 30
        return 50

    results.sort(key=score)
    return results


def pick_best_ip(bind_host: str = "") -> Optional[str]:
    """Pick the best local IPv4 address to advertise."""
    if bind_host and bind_host not in ("0.0.0.0", "::", ""):
        return bind_host
    interfaces = list_local_ipv4()
    for _, ip in interfaces:
        if _is_private(ip):
            return ip
    return interfaces[0][1] if interfaces else None


def build_access_url(ip: Optional[str], port: int, scheme: str,
                     token: Optional[str]) -> str:
    if not ip:
        return ""
    base = f"{scheme}://{ip}:{port}/"
    if token:
        base += "?" + urlencode({"token": token})
    return base


def detect_dashboard(port: int, timeout: float = 2.0) -> dict[str, Any]:
    """Return browser-safe metadata from a live loopback dashboard."""
    for scheme in ("http", "https"):
        url = f"{scheme}://127.0.0.1:{port}/api/health"
        context = ssl._create_unverified_context() if scheme == "https" else None
        try:
            request = Request(url, headers={"Accept": "application/json"})
            with urlopen(request, timeout=timeout, context=context) as response:
                if response.status != 200:
                    continue
                payload = json.loads(response.read(4096))
                if isinstance(payload, dict) and payload.get("ok") is True:
                    return {**payload, "scheme": scheme}
        except (HTTPError, URLError, OSError, TimeoutError,
                json.JSONDecodeError):
            continue
    return {}


def detect_dashboard_scheme(port: int, timeout: float = 2.0) -> str:
    """Detect HTTP or HTTPS from a live dashboard on the loopback port."""
    return str(detect_dashboard(port, timeout).get("scheme") or "")
