"""Machine-local configuration shared by the CLI and dashboard entrypoints."""

from __future__ import annotations

import ipaddress
import os
import plistlib
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DASHBOARD_TOKEN_FILE = (
    Path.home() / ".config" / "agent-orchestrator" / "dashboard-token"
)
LEGACY_DASHBOARD_TOKEN_FILE = PROJECT_DIR / "launchd" / "_token"
INSTALLED_DASHBOARD_PLIST = (
    Path.home() / "Library" / "LaunchAgents" / "com.user.orch-dashboard.plist"
)


def dashboard_token_file() -> Path:
    configured = os.environ.get("ORCH_DASHBOARD_TOKEN_FILE", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else DEFAULT_DASHBOARD_TOKEN_FILE
    )


def dashboard_token() -> str:
    """Return an explicit token or a locally cached LaunchAgent token."""
    if "ORCH_DASHBOARD_TOKEN" in os.environ:
        return os.environ["ORCH_DASHBOARD_TOKEN"].strip()

    for path in (dashboard_token_file(), LEGACY_DASHBOARD_TOKEN_FILE):
        try:
            token = path.read_text().strip()
        except OSError:
            continue
        if token:
            return token

    try:
        data = plistlib.loads(INSTALLED_DASHBOARD_PLIST.read_bytes())
        token = data.get("EnvironmentVariables", {}).get(
            "ORCH_DASHBOARD_TOKEN", ""
        )
    except (OSError, plistlib.InvalidFileException, AttributeError):
        return ""
    if isinstance(token, str):
        return token.strip()
    return ""


def is_loopback_host(host: str) -> bool:
    value = str(host or "").strip().lower().strip("[]")
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def require_dashboard_auth(host: str, token: str) -> None:
    if not is_loopback_host(host) and not str(token or "").strip():
        raise ValueError(
            "a dashboard exposed beyond localhost requires --token or "
            "$ORCH_DASHBOARD_TOKEN"
        )
