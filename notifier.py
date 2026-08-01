"""Slack notification via Incoming Webhooks (no extra dependencies)."""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from typing import Optional

log = logging.getLogger(__name__)

_webhook_url: Optional[str] = None


def _get_webhook_url() -> Optional[str]:
    global _webhook_url
    if _webhook_url is None:
        _webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    return _webhook_url or None


def configure(webhook_url: str):
    global _webhook_url
    _webhook_url = webhook_url


def send_slack(text: str, *, blocks: Optional[list] = None) -> bool:
    url = _get_webhook_url()
    if not url:
        return False

    payload: dict = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status == 200
            if not ok:
                log.warning(f"Slack returned status {resp.status}")
            return ok
    except (urllib.error.URLError, OSError) as e:
        log.warning(f"Slack notification failed: {e}")
        return False


def notify_task_done(project: str, task_name: str, status: str,
                     rounds: str = "", extra: str = ""):
    icon = ":white_check_mark:" if status == "completed" else ":x:"
    lines = [f"{icon} *[{project}]* Task `{task_name}` — *{status}*"]
    if rounds:
        lines.append(f"Rounds: {rounds}")
    if extra:
        lines.append(extra)
    send_slack("\n".join(lines))


def notify_all_done(project: str, summary: dict[str, str]):
    lines = [f":tada: *[{project}]* All tasks finished!\n"]
    for name, status in summary.items():
        icon = ":white_check_mark:" if status == "completed" else ":x:"
        lines.append(f"  {icon} `{name}` — {status}")
    send_slack("\n".join(lines))
