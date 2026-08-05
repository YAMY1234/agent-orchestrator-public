"""Web dashboard for agent-orchestrator.

Mobile-friendly read/write control panel over the existing tmux-based runs.
Works with both:
- Lightweight `orch run` sessions (session.json in output dir)
- Full `orch start` YAML tasks (state.json in output dir)

Controls sessions via `tmux capture-pane` / `tmux send-keys`, so nothing about
the orchestrator proper needs to change.

Run:
    orch dashboard --port 7860 [--host 0.0.0.0 --token SECRET]

Endpoints:
    GET  /                            -> static HTML app
    GET  /api/config                  -> browser-safe local dashboard config
    GET  /api/sessions                -> list all runs (merged view)
    GET  /api/sessions/{run_id}       -> details for one run
    GET  /api/sessions/{run_id}/pane  -> live tmux capture-pane text
    GET  /api/sessions/{run_id}/log   -> raw log file tail
    POST /api/sessions/{run_id}/send  -> tmux send-keys {text, enter, literal}
    POST /api/sessions/{run_id}/state -> update state.json fields
    POST /api/sessions/{run_id}/stop  -> graceful stop + save resume metadata
    POST /api/sessions/{run_id}/kill  -> force tmux kill-session
    DELETE /api/sessions/{run_id}     -> move an ended run folder to Trash
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import hmac
import html as html_lib
import json
import mimetypes
import os
import plistlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import textwrap
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from urllib.parse import urlparse

from .dashboard_network import build_access_url, list_local_ipv4, pick_best_ip
from .local_settings import dashboard_token, require_dashboard_auth
from .terminal_theme import (
    normalize_terminal_theme as _normalize_terminal_theme,
    patch_ttyd_index_theme as _patch_ttyd_index_theme,
    ttyd_theme_client_option as _ttyd_theme_client_option,
)

try:
    from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as e:
    raise SystemExit(
        "fastapi/uvicorn not installed. Run: pip install -r requirements.txt\n"
        f"Original error: {e}"
    )

# Optional deps for ttyd reverse proxy (iframe + ws stay same-origin)
try:
    import httpx
except ImportError:
    httpx = None  # type: ignore
try:
    import websockets
    from websockets.exceptions import ConnectionClosed as WsClosed
except ImportError:
    websockets = None  # type: ignore
    WsClosed = Exception  # type: ignore


PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
DEFAULT_OUTPUTS_DIR = Path(
    os.environ.get("ORCH_OUTPUTS_DIR") or PROJECT_DIR / "outputs"
).expanduser().resolve()
STATIC_DIR = PROJECT_DIR / "static"
DEFAULT_DASHBOARD_CONFIG = PROJECT_DIR / "dashboard.local.json"


def _configured_projects_dir(outputs_dir: Path,
                             explicit: Optional[Path] = None) -> Path:
    if explicit is not None:
        return explicit
    configured = os.environ.get("ORCH_PROJECTS_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return outputs_dir.parent / "projects"


# False-ish macOS malloc debug env values still trigger noisy runtime messages
# in every subprocess. Drop them before dashboard-spawned shells/watchers
# inherit the environment.
for _key in (
    "MallocStackLogging",
    "MallocStackLoggingNoCompact",
    "MallocScribble",
    "MallocGuardEdges",
    "MallocNanoZone",
):
    os.environ.pop(_key, None)

from .agent_titles import TitleCache
_TITLE_CACHE = TitleCache(ttl_seconds=600.0)
_QUICKLOOK_PROCS: dict[str, subprocess.Popen] = {}


def _should_auto_title(label: str, cwd: str, alive: bool) -> bool:
    """Keep transcript scanning off the hot path unless it can affect display.

    Manual labels already win over auto titles, and ended sessions can use
    their stable task/run names. Avoiding auto-title work for those rows keeps
    /api/sessions from repeatedly scanning large Codex/Claude/Cursor
    transcript trees on dashboard boot.
    """
    truthy = {"1", "true", "yes", "on"}
    if os.environ.get("ORCH_AUTO_TITLE_SESSIONS", "").lower() not in truthy:
        return False
    if label or not cwd:
        return False
    if alive:
        return True
    return os.environ.get("ORCH_AUTO_TITLE_ENDED", "").lower() in truthy


def _auto_title_for(agent: str, cwd: str, started_at: str,
                    label: str, alive: bool) -> Optional[str]:
    if not _should_auto_title(label, cwd, alive):
        return None
    return _TITLE_CACHE.get(agent, cwd, started_at)

# Lightweight "is the agent active?" probe, driven by the regular
# `/api/sessions` polling loop. For each alive tmux session we capture a stable
# tail of pane text, md5-hash it, and compare to the previous tick. When the
# hash changes, we bump `last_change_ts`. A session is considered "busy" while
# `now - last_change_ts < _BUSY_IDLE_SECONDS`, or while the latest visible
# status says shell/background-terminal work is still running.
# For panel sorting we keep a separate sustained-activity streak: one-off
# changes are ignored until content keeps changing for
# _PANEL_SORT_ACTIVE_SECONDS.
#
# Why here:
#   - Backend polling covers every alive session. Mobile (1-pane layout) and
#     collapsed/hidden sessions still need a busy signal even when no pane is
#     currently rendered.
#   - Piggy-backing on the polling call means zero new endpoints and the
#     cost stays bounded (one `tmux capture-pane` per alive session per list
#     call — a few ms each).
# Trade-off: screen-change granularity is ~1.5s rather than the old WS's 0.4s.
# Plenty for a sidebar-level "this one's working" hint.
_BUSY_IDLE_SECONDS = 2.0
_PANEL_SORT_ACTIVE_SECONDS = 30.0
_ACTIVITY_CONTINUITY_GAP_SECONDS = 15.0
_SESSION_BUSY_HASH: dict[str, bytes] = {}      # tmux_session -> md5 of last capture
_SESSION_LAST_CHANGE: dict[str, float] = {}    # tmux_session -> unix ts of last change
_SESSION_ACTIVITY_STREAK_START: dict[str, float] = {}  # tmux_session -> unix ts
_SESSION_LAST_SUSTAINED_ACTIVE: dict[str, float] = {}  # tmux_session -> unix ts
_SESSION_BACKGROUND_ACTIVE_START: dict[str, float] = {}  # tmux_session -> unix ts
_BACKGROUND_STATUS_DURATION_RE = (
    r"\d+(?:\.\d+)?\s*(?:ms|s|m|h|d)"
    r"(?:\s+\d+(?:\.\d+)?\s*(?:ms|s|m|h|d))*"
)
_BACKGROUND_TIMED_STATUS_RE = (
    r"^\s*(?:[-–—•*✻]\s*)?"
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ]*(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ]*){0,2}"
    r"\s+for\s+" + _BACKGROUND_STATUS_DURATION_RE
)
_BACKGROUND_WAITING_STATUS_RE = r"^\s*(?:[•*✻]\s*)?Waiting\s+for\b.{1,180}\bto\s+finish\b"
_BACKGROUND_STATUS_LINE_PATTERNS = (
    re.compile(_BACKGROUND_TIMED_STATUS_RE),
    re.compile(_BACKGROUND_WAITING_STATUS_RE),
)
_BACKGROUND_ACTIVE_PATTERNS = (
    re.compile(_BACKGROUND_TIMED_STATUS_RE + r".{0,180}\b(?i:(?:\d+\s+)?shells?\s+still\s+running)\b"),
    re.compile(_BACKGROUND_TIMED_STATUS_RE + r".{0,180}\b(?i:(?:\d+\s+)?background\s+terminals?\s+(?:still\s+)?running)\b"),
    re.compile(_BACKGROUND_WAITING_STATUS_RE),
)
_BACKGROUND_STATUS_DURATION_EXTRACT_RE = re.compile(
    r"\bfor\s+((?:\d+(?:\.\d+)?\s*(?:ms|s|m|h|d)(?:\s+|$)){1,4})",
    re.I,
)
_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)", re.I)


def _parse_compact_duration_seconds(text: str) -> float | None:
    total = 0.0
    saw = False
    unit_seconds = {
        "ms": 0.001,
        "s": 1.0,
        "m": 60.0,
        "h": 3600.0,
        "d": 86400.0,
    }
    for value, unit in _DURATION_PART_RE.findall(text or ""):
        total += float(value) * unit_seconds[unit.lower()]
        saw = True
    return total if saw else None


def _background_reason_elapsed_seconds(reason: str) -> float | None:
    m = _BACKGROUND_STATUS_DURATION_EXTRACT_RE.search(reason or "")
    if not m:
        return None
    return _parse_compact_duration_seconds(m.group(1))


def _terminal_theme_for_tmux_session(outputs_dir: Path, session: str) -> str:
    if not session:
        return ""
    for row in _discover_runs(outputs_dir):
        if row.get("tmux_session") == session:
            return _normalize_terminal_theme(str(row.get("terminal_theme") or ""))
    return ""


# ---------------------------------------------------------------------------
# tmux helpers (local subprocess; dashboard runs on the same host as tmux)
# ---------------------------------------------------------------------------

def tmux_capture(session: str, history: bool = True) -> str:
    """Return text snapshot of a tmux pane, or empty string if session is gone."""
    cmd = ["tmux", "capture-pane", "-t", session, "-p"]
    if history:
        cmd += ["-S", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def tmux_capture_activity(session: str) -> str:
    """Return a stable pane-text tail for activity hashing.

    `-J` joins wrapped lines, and `-S -200` uses a fixed scrollback tail instead
    of the current viewport height. That makes the hash much less sensitive to
    panel/window reshapes while still changing when new agent output arrives.
    """
    if not session:
        return ""
    cmd = ["tmux", "capture-pane", "-t", session, "-p", "-J", "-S", "-200"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return tmux_capture(session, history=False)


def _tmux_send_keys(argv: list, *, retries: int = 2,
                    timeout: float = 10.0) -> tuple[bool, str]:
    """Run one `tmux send-keys ...` invocation with small-retry-on-stutter.

    Returns (ok, stderr). tmux occasionally transiently fails with a
    non-zero exit and a message like "no such session" during a resize
    or reattach burst, then succeeds on an immediate retry. We retry a
    couple of times with a short backoff to paper over that, while
    still surfacing the real stderr if the failure persists.
    """
    last_err = ""
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(argv, timeout=timeout, check=False,
                               capture_output=True, text=True)
            if r.returncode == 0:
                return True, ""
            last_err = (r.stderr or "").strip() or f"exit={r.returncode}"
        except subprocess.TimeoutExpired:
            last_err = f"timeout after {timeout}s"
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(0.05 * (attempt + 1))
    return False, last_err


# Delay between literal paste and the trailing Enter.
#
# Why this exists: cursor-agent's TUI uses bracketed paste mode. When we
# fire `tmux send-keys -l <text>` immediately followed by `tmux send-keys
# Enter`, the two events arrive at the agent's stdin within ~10ms — fast
# enough that cursor's input state machine races between (a) consuming
# the Enter to *submit* the draft and (b) processing the trailing tail of
# the bracketed paste. The user-visible symptom: the agent successfully
# receives the prompt and starts working, but a copy of the same prompt
# stays stuck in the agent's draft box (the "→ <text>" line) and stays
# there indefinitely — even after the agent's reply finishes — because
# from cursor's perspective there's a draft waiting and no Enter to
# flush it.
#
# 200ms is enough on a modern Mac to let cursor finish ingesting the
# paste and clear the draft buffer before Enter arrives. The two-step
# launchers in `task_runner.py` / `monitor.py` already do `time.sleep(2)`
# between paste and Enter for the same reason; here we use a tighter
# 0.2s because the dashboard's `/api/sessions/{id}/send` is a synchronous
# HTTP request and we don't want to block UI for 2s on every send. Empirical
# testing on cursor-agent v2026.04.30 with text up to 4 KB confirms 0.2s
# kills the residual-draft bug while keeping send latency near-imperceptible.
PASTE_ENTER_DELAY_S = 0.2


def tmux_send(session: str, text: str, literal: bool = True, enter: bool = False) -> tuple[bool, str]:
    """Send `text` to the tmux pane. Returns (ok, error_msg).

    For large pastes we split into chunks to stay well below OS argv
    limits and per-call timeout budgets. `tmux send-keys -l <blob>`
    passes the whole paste as a single argv entry; on macOS that's
    bounded by ARG_MAX. Chunking at 8 KB also keeps each call
    sub-100ms and lets arbitrarily large pastes through.

    When both `text` and `enter` are set we insert a short settle delay
    (PASTE_ENTER_DELAY_S) between the paste and the Enter to avoid the
    "draft text stuck in cursor-agent's input box" race — see the
    PASTE_ENTER_DELAY_S comment for the gory details.
    """
    base = ["tmux", "send-keys", "-t", session]
    CHUNK = 8 * 1024
    if text:
        if literal and len(text) > CHUNK:
            for i in range(0, len(text), CHUNK):
                chunk = text[i:i + CHUNK]
                ok, err = _tmux_send_keys(base + ["-l", chunk])
                if not ok:
                    return False, f"chunk @{i}: {err}"
        else:
            cmd = list(base)
            if literal:
                cmd.append("-l")
            cmd.append(text)
            ok, err = _tmux_send_keys(cmd)
            if not ok:
                return False, err
    if enter:
        # Only delay when we actually pasted text — sending a bare Enter
        # (text="" + enter=True) is fine to forward instantly.
        if text and literal:
            time.sleep(PASTE_ENTER_DELAY_S)
        ok, err = _tmux_send_keys(base + ["Enter"], timeout=5)
        if not ok:
            return False, f"Enter: {err}"
    return True, ""


def tmux_alive(session: str) -> bool:
    if not session:
        return False
    try:
        r = subprocess.run(["tmux", "has-session", "-t", session],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def tmux_kill(session: str) -> bool:
    try:
        r = subprocess.run(["tmux", "kill-session", "-t", session],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def tmux_send_key(session: str, key: str) -> tuple[bool, str]:
    """Send one tmux key name (for example C-c)."""
    return _tmux_send_keys(["tmux", "send-keys", "-t", session, key],
                           timeout=5)


async def _deliver_first_prompt(session: str, prompt: str,
                                ready_timeout: float = 30.0,
                                grace: float = 2.0) -> bool:
    """Poll for `session` to become addressable, then paste `prompt` + Enter.

    Designed to run as a background asyncio task so the HTTP endpoint that
    kicked off a clone can return immediately. Silently swallows errors —
    the caller already replied to the user, and there's no useful recovery
    path here (the session is live; at worst the user pastes manually).
    """
    if not session or not prompt:
        return False
    import asyncio as _a  # local alias; top-level import exists elsewhere
    loop_deadline = time.time() + ready_timeout
    while time.time() < loop_deadline:
        if tmux_alive(session):
            break
        await _a.sleep(0.3)
    else:
        return False
    await _a.sleep(grace)
    try:
        tmux_send(session, prompt, literal=True, enter=False)
        await _a.sleep(0.2)
        tmux_send(session, "", literal=True, enter=True)
        return True
    except Exception:
        return False


def tmux_list_sessions() -> list[str]:
    """Return all live tmux session names."""
    try:
        r = subprocess.run(["tmux", "ls", "-F", "#{session_name}"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    except (subprocess.SubprocessError, FileNotFoundError):
        return []


def tmux_get_cwd(session: str) -> str:
    """Return the current pane's working directory for a tmux session."""
    if not session:
        return ""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session, "#{pane_current_path}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return ""


def tmux_session_started_at(session: str) -> str:
    """Return the ISO started_at string for a tmux session, derived from
    `#{session_created}` (a unix timestamp). Used to disambiguate which
    agent transcript belongs to which session when multiple sessions
    share a cwd — without this, every orphan/legacy entry in the
    sidebar gets the same auto_title.
    """
    if not session:
        return ""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session, "#{session_created}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return ""
        ts = r.stdout.strip()
        if not ts.isdigit():
            return ""
        from datetime import datetime
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%dT%H:%M:%S")
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        return ""


# Matches run dir names that end in our timestamp suffix, e.g.
#   interactive-20260417-104938
#   test-autoapprove-20260419-014421
_RUN_NAME_TS_RE = re.compile(r"-(\d{8})-(\d{6})$")


def run_name_started_at(run_name: str) -> str:
    """Extract the ISO started_at baked into a run dir name like
    `interactive-20260417-104938` -> `2026-04-17T10:49:38`.

    Returns "" for names that don't carry the timestamp suffix
    (pre-0.2 runs, manual dirs, etc.). Used only as a fallback when
    session.json is missing and there's no live tmux session to ask.
    """
    m = _RUN_NAME_TS_RE.search(run_name or "")
    if not m:
        return ""
    date, time_ = m.group(1), m.group(2)
    return f"{date[0:4]}-{date[4:6]}-{date[6:8]}T{time_[0:2]}:{time_[2:4]}:{time_[4:6]}"


def _token_matches(candidate: Optional[str], expected: Optional[str]) -> bool:
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)


def _dashboard_auth_matches(expected: Optional[str], *,
                            query_token: Optional[str] = None,
                            authorization: Optional[str] = None,
                            cookie_token: Optional[str] = None) -> bool:
    return bool(expected) and (
        _token_matches(query_token, expected)
        or _token_matches(authorization, f"Bearer {expected}")
        or _token_matches(cookie_token, expected)
    )


def icloud_drive_dir() -> Optional[Path]:
    """Return the user's iCloud Drive root if it's mounted, else None."""
    p = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    return p if p.is_dir() else None


# ---------------------------------------------------------------------------
# Orphan-session labels (for tmux sessions without a run dir to persist to).
# Kept in <outputs>/_orphan_labels.json as {session_name: label}.
# ---------------------------------------------------------------------------

def _orphan_labels_path(outputs_dir: Path) -> Path:
    return outputs_dir / "_orphan_labels.json"


def _load_orphan_labels(outputs_dir: Path) -> dict[str, str]:
    p = _orphan_labels_path(outputs_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_orphan_labels(outputs_dir: Path, data: dict[str, str]):
    p = _orphan_labels_path(outputs_dir)
    # Drop labels for sessions that are gone AND entries with empty strings.
    live = set(tmux_list_sessions())
    clean = {k: v for k, v in data.items() if v and k in live}
    p.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Spawning a command in a brand-new terminal window (macOS).
# ---------------------------------------------------------------------------

def _spawn_in_new_terminal(cmd: str, cwd: str = "") -> tuple[bool, str, str]:
    """Open a new terminal window and run `cmd` inside it.

    Tries iTerm2 first (via AppleScript), falls back to Terminal.app. Returns
    (ok, method_used, error_message).
    """
    if sys.platform != "darwin":
        return False, "none", "new-terminal spawn is only implemented on macOS"

    cd_prefix = f"cd {shlex.quote(cwd)} && " if cwd else ""
    full_cmd = cd_prefix + cmd
    errors: list[str] = []

    # --- iTerm2 via AppleScript --------------------------------------------
    if _app_installed("iTerm") or _app_installed("iTerm2"):
        as_cmd = full_cmd.replace("\\", "\\\\").replace('"', '\\"')
        applescript = (
            'tell application "iTerm"\n'
            '  activate\n'
            '  set newWindow to (create window with default profile)\n'
            '  tell current session of newWindow\n'
            f'    write text "{as_cmd}"\n'
            '  end tell\n'
            'end tell\n'
        )
        ok, err = _run_osascript(applescript)
        if ok:
            return True, "iterm", ""
        errors.append(f"iterm: {err}")

    # --- Terminal.app fallback --------------------------------------------
    # Note: avoid `set frontmost to true` — requires Accessibility permission
    # and fails with error -10006 on recent macOS. `activate` already brings
    # Terminal to the foreground.
    as_cmd2 = full_cmd.replace("\\", "\\\\").replace('"', '\\"')
    applescript2 = (
        'tell application "Terminal"\n'
        '  activate\n'
        f'  do script "{as_cmd2}"\n'
        'end tell\n'
    )
    ok, err = _run_osascript(applescript2)
    if ok:
        return True, "terminal", ""
    errors.append(f"terminal: {err}")
    return False, "terminal", " | ".join(errors)


def _app_installed(name: str) -> bool:
    for prefix in ("/Applications", "/System/Applications", os.path.expanduser("~/Applications")):
        if Path(prefix, f"{name}.app").exists():
            return True
    return False


def _run_osascript(script: str) -> tuple[bool, str]:
    osa = shutil.which("osascript")
    if not osa:
        return False, "osascript not found"
    try:
        r = subprocess.run([osa, "-"], input=script, text=True,
                           capture_output=True, timeout=10)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "osascript failed").strip()
        return True, ""
    except subprocess.SubprocessError as e:
        return False, str(e)


def _match_orphan_session(run_name: str, live_sessions: list[str]) -> Optional[str]:
    """For legacy runs (no session.json), try to match an `orch-<task>-<pid>` session.

    run_name is like `interactive-20260417-010004`; we strip the timestamp
    and look for `orch-<task>-*` in live tmux sessions.
    """
    # run_name -> task prefix (strip trailing "-YYYYMMDD-HHMMSS")
    parts = run_name.rsplit("-", 2)
    if len(parts) == 3 and len(parts[1]) == 8 and len(parts[2]) == 6:
        task = parts[0]
    else:
        task = run_name
    prefix = f"orch-{task}-"
    candidates = [s for s in live_sessions if s.startswith(prefix)]
    if not candidates:
        return None
    # When multiple match, pick the one with largest pid suffix (likely newest).
    def _pid(s: str) -> int:
        try:
            return int(s.rsplit("-", 1)[-1])
        except ValueError:
            return 0
    return max(candidates, key=_pid)


# ---------------------------------------------------------------------------
# Run discovery: walk outputs/ and merge state.json (full mode) + session.json
# (light run mode) into a uniform list.
# ---------------------------------------------------------------------------

_JSON_READ_CACHE: dict[str, tuple[int, int, Optional[dict[str, Any]]]] = {}
_JSON_READ_CACHE_LOCK = threading.Lock()


def _safe_read_json(p: Path) -> Optional[dict]:
    try:
        st = p.stat()
    except OSError:
        return None
    key = str(p)
    sig = (int(st.st_mtime_ns), int(st.st_size))
    with _JSON_READ_CACHE_LOCK:
        cached = _JSON_READ_CACHE.get(key)
        if cached and cached[0:2] == sig:
            return copy.deepcopy(cached[2])
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    with _JSON_READ_CACHE_LOCK:
        _JSON_READ_CACHE[key] = (sig[0], sig[1], data)
        # Bound the cache; outputs has a few hundred metadata files, while
        # linked-folder task metadata can add churn in long-lived dashboards.
        if len(_JSON_READ_CACHE) > 2048:
            _JSON_READ_CACHE.clear()
            _JSON_READ_CACHE[key] = (sig[0], sig[1], data)
    return copy.deepcopy(data)


def _dashboard_client_config() -> dict[str, str]:
    config_path = Path(
        os.environ.get("ORCH_DASHBOARD_CONFIG", str(DEFAULT_DASHBOARD_CONFIG))
    ).expanduser()
    data = _safe_read_json(config_path) or {}
    fields = {
        "notes_url": ("ORCH_NOTES_URL", ""),
        "projects_browser_url": (
            "ORCH_PROJECTS_BROWSER_URL", "http://127.0.0.1:8080/"
        ),
        "git_status_url": ("ORCH_GIT_STATUS_URL", "http://127.0.0.1:8501/"),
        "projects_root": (
            "ORCH_PROJECTS_ROOT", str(Path.home() / "Documents" / "Projects")
        ),
    }
    result = {}
    for key, (env_name, default) in fields.items():
        value = (
            os.environ[env_name]
            if env_name in os.environ
            else data.get(key, default)
        )
        result[key] = str(value or "").strip()
    projects_root = Path(
        os.path.expandvars(result["projects_root"])
    ).expanduser()
    if not projects_root.is_dir():
        result["projects_root"] = str(Path.home())
    return result


def _prime_json_cache(paths: list[Path]) -> None:
    """Preload small metadata JSON files concurrently for /api/sessions.

    macOS can take ~40-100ms to open each tiny metadata file when the output
    tree is cold. Sequentially reading 300+ session.json files then dominates
    dashboard boot. This keeps the same mtime/size invalidation semantics as
    _safe_read_json but overlaps the file opens.
    """
    pending: list[Path] = []
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        key = str(p)
        sig = (int(st.st_mtime_ns), int(st.st_size))
        with _JSON_READ_CACHE_LOCK:
            cached = _JSON_READ_CACHE.get(key)
        if not cached or cached[0:2] != sig:
            pending.append(p)
    if len(pending) <= 1:
        for p in pending:
            _safe_read_json(p)
        return
    workers = min(16, max(2, len(pending)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_safe_read_json, pending))


def _safe_write_json(p: Path, data: dict) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(p)


def _projects_root() -> Path:
    configured = _dashboard_client_config()["projects_root"]
    return Path(configured).expanduser().resolve()


def _extra_linked_folder_roots() -> list[Path]:
    roots: list[Path] = []
    extra = os.environ.get("ORCH_LINKED_FOLDER_ROOTS", "")
    for raw in extra.split(":"):
        raw = raw.strip()
        if raw:
            roots.append(Path(raw).expanduser())
    return [p.resolve() for p in roots if p.exists()]


def _linked_folder_roots() -> list[Path]:
    roots = [_projects_root()]
    roots.extend(_extra_linked_folder_roots())
    return [p for p in roots if p.exists()]


def _link_path_scope(path: Path) -> str:
    projects_root = _projects_root()
    if projects_root.exists():
        try:
            rel = path.relative_to(projects_root)
        except ValueError:
            pass
        else:
            if not rel.parts:
                return ""
            first = rel.parts[0]
            if first == "_to_delete":
                return ""
            if len(rel.parts) == 1 and path.is_file():
                return ""
            if first in {"current", "archive"}:
                return "task"
            return "project"

    for root in _extra_linked_folder_roots():
        if path == root or root in path.parents:
            return "external"
    return ""


def _is_allowed_linked_folder(path: Path) -> bool:
    return bool(_link_path_scope(path))


def _should_write_folder_task_metadata(folder: Path) -> bool:
    return _link_path_scope(folder) == "task"


def _normalize_linked_url(raw: str) -> str:
    url = str(raw or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("linked URL must start with http:// or https://")
    if parsed.username or parsed.password:
        raise ValueError("linked URL must not include credentials")
    if any(ord(ch) < 32 for ch in url):
        raise ValueError("linked URL contains control characters")
    return url


def _recover_malformed_linked_url(raw: str) -> str:
    value = str(raw or "").strip()
    lower = value.lower()
    for scheme in ("https", "http"):
        marker = f"{scheme}:/"
        idx = lower.rfind(marker)
        if idx < 0:
            continue
        tail = value[idx + len(marker):]
        if not tail or tail.startswith("/"):
            continue
        return f"{scheme}://{tail}"
    return ""


def _coerce_linked_url(raw: str) -> str:
    try:
        return _normalize_linked_url(raw)
    except ValueError:
        recovered = _recover_malformed_linked_url(raw)
        if recovered:
            return _normalize_linked_url(recovered)
        raise


def _is_linked_url(raw: str) -> bool:
    try:
        _coerce_linked_url(raw)
        return True
    except ValueError:
        return False


def _default_linked_url_label(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path and path != "/":
        return f"{parsed.netloc}{path}"
    return parsed.netloc or url


def _normalize_linked_folders(raw: Any) -> list[dict[str, str]]:
    items = raw if isinstance(raw, list) else []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            path = item
            label = ""
            created_at = ""
            item_type = ""
        elif isinstance(item, dict):
            path = str(item.get("path") or "")
            label = str(item.get("label") or "")
            created_at = str(item.get("created_at") or "")
            item_type = str(item.get("type") or item.get("kind") or "")
        else:
            continue
        path = path.strip()
        if not path:
            continue
        if item_type == "url" or _is_linked_url(path):
            try:
                path = _coerce_linked_url(path)
            except ValueError:
                continue
            item_type = "url"
            label = label or _default_linked_url_label(path)
        if path in seen:
            continue
        seen.add(path)
        if item_type not in {"file", "folder", "url"}:
            try:
                p = Path(path).expanduser()
                item_type = "file" if p.is_file() else "folder"
            except OSError:
                item_type = "folder"
        rec = {
            "path": path,
            "label": label or Path(path).name,
            "type": item_type,
        }
        if created_at:
            rec["created_at"] = created_at
        out.append(rec)
    return out


def _add_linked_path(container: dict[str, Any], path_obj: Path,
                     label: str, item_type: str) -> bool:
    path = str(path_obj)
    if item_type == "url":
        path = _coerce_linked_url(path)
        label = label or _default_linked_url_label(path)
    else:
        label = label or path_obj.name
    item_type = item_type if item_type in {"file", "folder", "url"} else "folder"
    folders = _normalize_linked_folders(container.get("linked_folders"))
    for rec in folders:
        if rec.get("path") == path:
            changed = rec.get("label") != label or rec.get("type") != item_type
            rec["label"] = label
            rec["type"] = item_type
            container["linked_folders"] = folders
            return changed
    folders.append({
        "path": path,
        "label": label,
        "type": item_type,
        "created_at": _iso_now(),
    })
    container["linked_folders"] = folders
    return True


def _add_linked_folder(container: dict[str, Any], folder: Path,
                       label: str) -> bool:
    return _add_linked_path(container, folder, label, "folder")


def _add_linked_url(container: dict[str, Any], url: str, label: str) -> bool:
    return _add_linked_path(container, url, label, "url")


def _folder_task_metadata_context(r: dict[str, Any]) -> dict[str, str]:
    run_dir = Path(str(r.get("run_dir") or ""))
    metadata_name = "state.json" if r.get("kind") == "task" else "session.json"
    return {
        "run_id": str(r.get("run_id") or ""),
        "run_name": str(r.get("run_name") or run_dir.name),
        "task": str(r.get("task") or ""),
        "agent": str(r.get("agent") or ""),
        "tmux_session": str(r.get("tmux_session") or ""),
        "cwd": str(r.get("cwd") or ""),
        "metadata_path": str(run_dir / metadata_name) if run_dir else "",
        "kind": str(r.get("kind") or ""),
    }


def _run_metadata_path(r: dict[str, Any]) -> str:
    run_dir = str(r.get("run_dir") or "")
    if not run_dir:
        return ""
    metadata_name = "state.json" if r.get("kind") == "task" else "session.json"
    return str(Path(run_dir) / metadata_name)


def _active_snapshot_path(outputs_dir: Path) -> Path:
    return outputs_dir / ".active_sessions_snapshot.json"


_ACTIVE_SNAPSHOT_LOCK = threading.Lock()
_ACTIVE_SNAPSHOT_AUTOSAVE_DEFAULT_SECONDS = 3600.0


def _active_snapshot_autosave_enabled() -> bool:
    raw = os.environ.get("ORCH_ACTIVE_SNAPSHOT_AUTOSAVE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _active_snapshot_autosave_interval() -> float:
    raw = os.environ.get(
        "ORCH_ACTIVE_SNAPSHOT_AUTOSAVE_INTERVAL_SEC",
        str(int(_ACTIVE_SNAPSHOT_AUTOSAVE_DEFAULT_SECONDS)),
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = _ACTIVE_SNAPSHOT_AUTOSAVE_DEFAULT_SECONDS
    return max(60.0, value)


def _update_folder_task_metadata(folder: Path, context: dict[str, str],
                                 label: str) -> dict[str, str]:
    meta_dir = folder / ".orch"
    meta_path = meta_dir / "task.json"
    existing = _safe_read_json(meta_path) if meta_path.exists() else {}
    data = existing if isinstance(existing, dict) else {}
    now = _iso_now()
    run_id = str(context.get("run_id") or "")
    previous_owner = str(data.get("created_by_run_id") or "")

    if not previous_owner:
        data.update({
            "schema_version": 1,
            "path": str(folder),
            "created_at": now,
            "created_by_run_id": run_id,
            "created_by_run_name": context.get("run_name", ""),
            "created_by_task": context.get("task", ""),
            "created_by_agent": context.get("agent", ""),
            "created_by_tmux_session": context.get("tmux_session", ""),
            "created_by_metadata_path": context.get("metadata_path", ""),
        })

    data.update({
        "schema_version": 1,
        "path": str(folder),
        "label": label or folder.name,
        "last_linked_at": now,
        "last_linked_run_id": run_id,
        "last_linked_run_name": context.get("run_name", ""),
        "last_linked_task": context.get("task", ""),
        "last_linked_agent": context.get("agent", ""),
        "last_linked_tmux_session": context.get("tmux_session", ""),
        "last_linked_metadata_path": context.get("metadata_path", ""),
    })

    history = data.get("link_history")
    if not isinstance(history, list):
        history = []
    event = {
        "linked_at": now,
        "run_id": run_id,
        "run_name": context.get("run_name", ""),
        "task": context.get("task", ""),
        "agent": context.get("agent", ""),
        "tmux_session": context.get("tmux_session", ""),
        "metadata_path": context.get("metadata_path", ""),
        "label": label or folder.name,
    }
    if not history or any(history[-1].get(k) != event.get(k) for k in ("run_id", "label")):
        history.append(event)
    data["link_history"] = history[-25:]

    try:
        meta_dir.mkdir(parents=True, exist_ok=True)
        _safe_write_json(meta_path, data)
    except OSError as e:
        raise HTTPException(500, f"failed to write task metadata: {e}")

    warning = ""
    owner = str(data.get("created_by_run_id") or "")
    if owner and run_id and owner != run_id:
        warning = (
            f"folder was created by run_id {owner}; "
            f"current link is from run_id {run_id}. "
            "Create a sibling subtask folder unless you are intentionally continuing this one."
        )
    return {"path": str(meta_path), "warning": warning}


def _remove_linked_folder(container: dict[str, Any], raw_path: str) -> bool:
    try:
        target = str(Path(raw_path).expanduser().resolve())
    except OSError:
        target = str(raw_path)
    folders = _normalize_linked_folders(container.get("linked_folders"))
    kept: list[dict[str, str]] = []
    changed = False
    for rec in folders:
        rec_path = rec.get("path", "")
        try:
            rec_resolved = str(Path(rec_path).expanduser().resolve())
        except OSError:
            rec_resolved = rec_path
        if rec_path == raw_path or rec_resolved == target:
            changed = True
            continue
        kept.append(rec)
    container["linked_folders"] = kept
    return changed


def _resolve_linked_path(raw: str) -> Path:
    try:
        path = Path(raw).expanduser().resolve()
    except OSError:
        raise HTTPException(400, "invalid linked path")
    if not (path.is_file() or path.is_dir()):
        raise HTTPException(404, "linked path not found")
    if not _is_allowed_linked_folder(path):
        roots = ", ".join(str(p) for p in _linked_folder_roots())
        raise HTTPException(400, f"linked path outside allowed roots: {roots}")
    return path


def _resolve_linked_folder(raw: str) -> Path:
    folder = _resolve_linked_path(raw)
    if not folder.is_dir():
        raise HTTPException(400, "linked path is not a folder")
    return folder


def _read_text_preview(path: Path, max_chars: int = 12000) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n... (truncated)"
    return text


_PREVIEW_TEXT_EXTS = {
    ".bash", ".cfg", ".conf", ".css", ".csv", ".env", ".gitignore", ".html",
    ".ini", ".js", ".json", ".jsonl", ".log", ".md", ".markdown", ".py",
    ".rs", ".sh", ".toml", ".ts", ".txt", ".xml", ".yaml", ".yml",
}

_SKIP_FOLDER_DIRS = {".git", ".orch", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}

_FINDER_TAG_ATTR = "com.apple.metadata:_kMDItemUserTags"
_FINDER_TAG_COLORS = {
    0: ("none", "#8b949e"),
    1: ("gray", "#8e8e93"),
    2: ("green", "#34c759"),
    3: ("purple", "#af52de"),
    4: ("blue", "#007aff"),
    5: ("yellow", "#ffcc00"),
    6: ("red", "#ff3b30"),
    7: ("orange", "#ff9500"),
}
_FINDER_TAG_NAMES = {name: idx for idx, (name, _hex) in _FINDER_TAG_COLORS.items()}
_ORCH_TAG_PREFIX = "Agent Orchestrator "


def _read_xattr(path: Path, attr: str) -> Optional[bytes]:
    getxattr = getattr(os, "getxattr", None)
    if getxattr is not None:
        try:
            return getxattr(str(path), attr)
        except OSError:
            pass
    if sys.platform == "darwin" and shutil.which("xattr"):
        try:
            proc = subprocess.run(
                ["xattr", "-px", attr, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        text = "".join(proc.stdout.split())
        try:
            return bytes.fromhex(text)
        except ValueError:
            return None
    return None


def _write_xattr(path: Path, attr: str, data: bytes) -> None:
    setxattr = getattr(os, "setxattr", None)
    if setxattr is not None:
        try:
            setxattr(str(path), attr, data)
            return
        except OSError as exc:
            raise HTTPException(500, f"failed to write xattr: {exc}")
    if sys.platform == "darwin" and shutil.which("xattr"):
        proc = subprocess.run(
            ["xattr", "-wx", attr, data.hex(), str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode != 0:
            raise HTTPException(500, proc.stderr.strip() or "failed to write xattr")
        return
    raise HTTPException(500, "xattr writing is not available on this host")


def _remove_xattr(path: Path, attr: str) -> None:
    removexattr = getattr(os, "removexattr", None)
    if removexattr is not None:
        try:
            removexattr(str(path), attr)
            return
        except OSError:
            return
    if sys.platform == "darwin" and shutil.which("xattr"):
        subprocess.run(
            ["xattr", "-d", attr, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=False,
        )


def _finder_tag_list(path: Path) -> list[str]:
    raw = _read_xattr(path, _FINDER_TAG_ATTR)
    if not raw:
        return []
    try:
        tags = plistlib.loads(raw)
    except Exception:
        return []
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    for item in tags:
        out.append(item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item))
    return out


def _finder_tag_meta(path: Path) -> dict[str, Any]:
    tags = _finder_tag_list(path)
    if not tags:
        return {}

    color_indexes: list[int] = []
    for item in tags:
        text = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
        marker = text.rsplit("\n", 1)[-1]
        try:
            color_indexes.append(int(marker))
        except ValueError:
            color_indexes.append(0)
    color_index = next((idx for idx in color_indexes if idx > 0), color_indexes[0] if color_indexes else 0)
    color_name, color_hex = _FINDER_TAG_COLORS.get(color_index, _FINDER_TAG_COLORS[0])
    return {
        "tagged": True,
        "tag_count": len(tags),
        "tag_color": color_name,
        "tag_color_hex": color_hex,
    }


def _set_finder_tag_color(path: Path, color: str) -> dict[str, Any]:
    color = color.strip().lower()
    if color in {"clear", "none", ""}:
        existing = [t for t in _finder_tag_list(path) if not t.startswith(_ORCH_TAG_PREFIX)]
    else:
        if color not in _FINDER_TAG_NAMES or color == "none":
            raise HTTPException(400, "unsupported tag color")
        idx = _FINDER_TAG_NAMES[color]
        existing = [t for t in _finder_tag_list(path) if not t.startswith(_ORCH_TAG_PREFIX)]
        existing.insert(0, f"{_ORCH_TAG_PREFIX}{color}\n{idx}")

    if existing:
        _write_xattr(path, _FINDER_TAG_ATTR, plistlib.dumps(existing))
    else:
        _remove_xattr(path, _FINDER_TAG_ATTR)
    return _finder_tag_meta(path)


def _file_preview_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    return "text"


def _is_previewable_file(path: Path) -> bool:
    if path.name in {"README", "Makefile"}:
        return True
    suffix = path.suffix.lower()
    if suffix in _PREVIEW_TEXT_EXTS:
        return True
    try:
        if path.stat().st_size > 512 * 1024:
            return suffix in {".md", ".markdown", ".log", ".txt"}
    except OSError:
        return False
    return False


def _tail_text(path: Path, max_lines: int = 160,
               max_chars: int = 16000) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


def _read_file_preview(path: Path, max_bytes: int = 256 * 1024) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if path.name == "AGENT_LOG.md" and size > max_bytes:
                f.seek(max(0, size - max_bytes))
            data = f.read(max_bytes + 1)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    if b"\0" in data[:4096]:
        return {
            "ok": False,
            "binary": True,
            "error": "binary files are not previewed",
            "size": size,
        }

    truncated = size > max_bytes
    if len(data) > max_bytes:
        data = data[:max_bytes]
        truncated = True
    content = data.decode("utf-8", errors="replace")
    if path.name == "AGENT_LOG.md" and truncated:
        first_newline = content.find("\n")
        if first_newline >= 0:
            content = content[first_newline + 1:]
        content = "... (showing AGENT_LOG.md tail)\n\n" + content
    return {
        "ok": True,
        "content": content,
        "truncated": truncated,
        "size": size,
        "kind": _file_preview_kind(path),
        "mtime": path.stat().st_mtime,
    }


def _folder_tree(path: Path, max_depth: int = 2,
                 max_entries: int = 120) -> list[str]:
    lines: list[str] = []
    count = 0
    root_depth = len(path.parts)
    for cur, dirs, files in os.walk(path):
        cur_path = Path(cur)
        depth = len(cur_path.parts) - root_depth
        if depth > max_depth:
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__"})
        files = sorted(f for f in files if f != ".DS_Store")
        indent = "  " * depth
        if depth == 0:
            lines.append(f"{path.name}/")
        else:
            lines.append(f"{indent}{cur_path.name}/")
        count += 1
        for name in files:
            if count >= max_entries:
                lines.append("  ...")
                return lines
            lines.append(f"{indent}  {name}")
            count += 1
    return lines


def _clean_folder_rel(rel: str) -> str:
    rel = rel.strip().lstrip("/")
    if not rel:
        return ""
    parts = PurePosixPath(rel).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(400, "invalid path")
    return "/".join(parts)


def _is_safe_linked_target(target: Path, _folder: Path) -> bool:
    return _is_allowed_linked_folder(target)


def _resolve_folder_dir(folder: Path, rel: str) -> Path:
    rel = _clean_folder_rel(rel)
    try:
        target = (folder / rel).resolve() if rel else folder.resolve()
    except OSError:
        raise HTTPException(400, "invalid directory path")
    if not _is_safe_linked_target(target, folder):
        raise HTTPException(400, "directory path escapes allowed linked roots")
    if not target.is_dir():
        raise HTTPException(404, "directory not found")
    return target


def _entry_depth(rel: str) -> int:
    return 0 if not rel else len(rel.split("/"))


def _has_visible_folder_child(path: Path) -> bool:
    try:
        for child in path.iterdir():
            if child.name == ".DS_Store":
                continue
            if child.is_dir():
                if child.name not in _SKIP_FOLDER_DIRS:
                    return True
            elif child.is_file():
                return True
    except OSError:
        return False
    return False


def _folder_dir_entry(root: Path, child: Path,
                      rel: Optional[str] = None) -> dict[str, Any]:
    if rel is None:
        rel = "" if child == root else child.relative_to(root).as_posix()
    try:
        stat = child.stat()
        mtime = stat.st_mtime
    except OSError:
        mtime = 0.0
    entry = {
        "type": "dir",
        "name": child.name,
        "rel": rel,
        "depth": _entry_depth(rel),
        "mtime": mtime,
        "has_children": _has_visible_folder_child(child),
        "is_symlink": child.is_symlink(),
    }
    entry.update(_finder_tag_meta(child))
    return entry


def _folder_file_entry(root: Path, child: Path,
                       rel: Optional[str] = None) -> dict[str, Any]:
    if rel is None:
        rel = child.relative_to(root).as_posix()
    try:
        stat = child.stat()
        size = stat.st_size
        mtime = stat.st_mtime
    except OSError:
        size = 0
        mtime = 0.0
    entry = {
        "type": "file",
        "name": child.name,
        "rel": rel,
        "depth": _entry_depth(rel),
        "size": size,
        "mtime": mtime,
        "kind": _file_preview_kind(child),
        "previewable": _is_previewable_file(child),
        "is_symlink": child.is_symlink(),
    }
    entry.update(_finder_tag_meta(child))
    return entry


def _folder_direct_entries(root: Path, rel: str = "", *,
                           include_self: bool = False,
                           max_entries: int = 5000) -> tuple[list[dict[str, Any]], bool, Path]:
    rel = _clean_folder_rel(rel)
    target = _resolve_folder_dir(root, rel)
    entries: list[dict[str, Any]] = []
    omitted = False
    if include_self:
        entries.append(_folder_dir_entry(root, target, rel))

    try:
        dirs: list[Path] = []
        files: list[Path] = []
        for child in target.iterdir():
            if child.name == ".DS_Store":
                continue
            if child.is_dir():
                if child.name not in _SKIP_FOLDER_DIRS:
                    dirs.append(child)
            elif child.is_file():
                files.append(child)
    except OSError:
        return entries, omitted, target

    dirs = sorted(dirs, key=lambda p: p.name.lower())
    files = sorted(files, key=lambda p: (
        0 if p.name == "README.md" else
        1 if p.name == "AGENT_LOG.md" else
        2,
        p.name.lower(),
    ))
    priority_names = {"README.md", "AGENT_LOG.md"}
    priority_files = [p for p in files if p.name in priority_names]
    other_files = [p for p in files if p.name not in priority_names]

    for child in [*priority_files, *dirs, *other_files]:
        if len(entries) >= max_entries:
            omitted = True
            break
        child_rel = f"{rel}/{child.name}" if rel else child.name
        if child.is_dir():
            entries.append(_folder_dir_entry(root, child, child_rel))
        else:
            entries.append(_folder_file_entry(root, child, child_rel))
    return entries, omitted, target


def _linked_search_tokens(query: str) -> list[str]:
    return [
        token.lower()
        for token in re.split(r"\s+", str(query or "").strip().replace("\\", "/"))
        if token
    ]


def _linked_search_score(tokens: list[str], *, name: str, rel: str,
                         full_path: str, label: str) -> Optional[float]:
    if not tokens:
        return None
    lower_name = name.lower()
    lower_rel = rel.lower()
    lower_full = full_path.lower()
    lower_label = label.lower()
    score = 0.0
    for token in tokens:
        if token in lower_name:
            score += 140.0 + min(len(token), 80)
            if lower_name.startswith(token):
                score += 35.0
            continue
        if token in lower_rel:
            score += 95.0 + min(len(token) * 0.5, 45)
            continue
        if token in lower_full:
            score += 75.0 + min(len(token) * 0.35, 35)
            continue
        if token in lower_label:
            score += 45.0
            continue
        return None
    return score - len(rel) * 0.002


def _search_linked_folder_files(root: Path, *, label: str, tokens: list[str],
                                max_results: int,
                                max_scanned: int) -> tuple[list[dict[str, Any]], bool, int]:
    results: list[dict[str, Any]] = []
    scanned = 0
    omitted = False
    root_depth = len(root.parts)
    try:
        walker = os.walk(root)
        for cur, dirs, files in walker:
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_FOLDER_DIRS)
            files = sorted(f for f in files if f != ".DS_Store")
            cur_path = Path(cur)
            if len(cur_path.parts) - root_depth > 40:
                dirs[:] = []
            for name in files:
                scanned += 1
                if scanned > max_scanned:
                    omitted = True
                    dirs[:] = []
                    break
                child = cur_path / name
                try:
                    resolved = child.resolve()
                except OSError:
                    continue
                if not _is_safe_linked_target(resolved, root) or not resolved.is_file():
                    continue
                try:
                    rel = resolved.relative_to(root).as_posix()
                except ValueError:
                    continue
                score = _linked_search_score(
                    tokens,
                    name=name,
                    rel=rel,
                    full_path=str(resolved),
                    label=label,
                )
                if score is None:
                    continue
                results.append({
                    "score": score,
                    "entry": _folder_file_entry(root, resolved, rel),
                })
            if omitted:
                break
    except OSError:
        omitted = True
    results.sort(key=lambda item: (
        -float(item.get("score") or 0),
        str((item.get("entry") or {}).get("rel") or ""),
    ))
    return results[:max_results], omitted, scanned


def _folder_entries(path: Path, max_depth: int = 5,
                    max_entries: int = 500) -> tuple[list[dict[str, Any]], bool]:
    entries: list[dict[str, Any]] = [{
        "type": "dir",
        "name": path.name,
        "rel": "",
        "depth": 0,
    }]
    count = 1
    omitted = False
    skip_dirs = _SKIP_FOLDER_DIRS

    def add_file(child: Path, depth: int) -> None:
        nonlocal count, omitted
        if count >= max_entries:
            omitted = True
            return
        rel = child.relative_to(path).as_posix()
        try:
            stat = child.stat()
            size = stat.st_size
            mtime = stat.st_mtime
        except OSError:
            size = 0
            mtime = 0.0
        entries.append({
            "type": "file",
            "name": child.name,
            "rel": rel,
            "depth": depth,
            "size": size,
            "mtime": mtime,
            "kind": _file_preview_kind(child),
            "previewable": _is_previewable_file(child),
        })
        count += 1

    def walk_dir(cur_path: Path, depth: int) -> None:
        nonlocal count, omitted
        if omitted:
            return
        try:
            dirs: list[Path] = []
            files: list[Path] = []
            for child in cur_path.iterdir():
                if child.name == ".DS_Store":
                    continue
                if child.is_dir():
                    if child.name not in skip_dirs:
                        dirs.append(child)
                elif child.is_file():
                    files.append(child)
        except OSError:
            return

        files = sorted(files, key=lambda p: (
            0 if p.name == "README.md" else
            1 if p.name == "AGENT_LOG.md" else
            2,
            p.name.lower(),
        ))
        dirs = sorted(dirs, key=lambda p: p.name.lower())

        priority_names = {"README.md", "AGENT_LOG.md"}
        priority_files = [p for p in files if p.name in priority_names]
        other_files = [p for p in files if p.name not in priority_names]

        for child in priority_files:
            add_file(child, depth + 1)
            if omitted:
                return
        if depth >= max_depth:
            for child in other_files:
                add_file(child, depth + 1)
                if omitted:
                    return
            return
        for child in dirs:
            if count >= max_entries:
                omitted = True
                return
            rel = child.relative_to(path).as_posix()
            entries.append({
                "type": "dir",
                "name": child.name,
                "rel": rel,
                "depth": depth + 1,
            })
            count += 1
        for child in other_files:
            add_file(child, depth + 1)
            if omitted:
                return
        for child in dirs:
            walk_dir(child, depth + 1)

    walk_dir(path, 0)
    return entries, omitted


def _default_folder_file(entries: list[dict[str, Any]]) -> str:
    files = [e for e in entries if e.get("type") == "file"]
    rels = {str(e.get("rel")): e for e in files}
    for rel in ("README.md", "readme.md", "AGENT_LOG.md"):
        if rel in rels:
            return rel
    for e in files:
        rel = str(e.get("rel") or "")
        if rel.lower().endswith((".md", ".markdown")) and e.get("previewable"):
            return rel
    for e in files:
        if e.get("previewable"):
            return str(e.get("rel") or "")
    return ""


def _linked_folder_summary(rec: dict[str, str]) -> dict[str, Any]:
    raw_path = rec.get("path", "")
    raw_type = rec.get("type", "")
    if raw_type == "url" or _is_linked_url(raw_path):
        try:
            url = _coerce_linked_url(raw_path)
        except ValueError:
            label = rec.get("label") or raw_path
            return {**rec, "label": label, "type": "url", "exists": False, "allowed": False}
        label = rec.get("label") or _default_linked_url_label(url)
        return {
            **rec,
            "path": url,
            "url": url,
            "label": label,
            "type": "url",
            "exists": True,
            "allowed": True,
            "tree": "",
            "entries": [],
            "tree_omitted": False,
            "loaded_dirs": [],
            "default_file": "",
            "readme": "",
            "agent_log_tail": "",
            "has_readme": False,
            "has_agent_log": False,
            "mtime": 0,
        }
    label = rec.get("label") or Path(raw_path).name
    try:
        linked_path = Path(raw_path).expanduser().resolve()
    except OSError:
        return {**rec, "label": label, "exists": False, "allowed": False}
    exists = linked_path.exists()
    item_type = rec.get("type") if rec.get("type") in {"file", "folder"} else (
        "file" if linked_path.is_file() else "folder"
    )
    allowed = exists and _is_allowed_linked_folder(linked_path)
    summary: dict[str, Any] = {
        **rec,
        "path": str(linked_path),
        "label": label,
        "type": item_type,
        "exists": exists,
        "allowed": allowed,
    }
    summary.update(_finder_tag_meta(linked_path))
    if not allowed:
        return summary
    if linked_path.is_file():
        entry = _folder_file_entry(linked_path.parent, linked_path, "")
        summary.update({
            "tree": "",
            "entries": [entry],
            "tree_omitted": False,
            "loaded_dirs": [""],
            "default_file": "",
            "readme": "",
            "agent_log_tail": "",
            "has_readme": False,
            "has_agent_log": False,
            "mtime": linked_path.stat().st_mtime,
        })
        return summary
    folder = linked_path
    readme = folder / "README.md"
    agent_log = folder / "AGENT_LOG.md"
    try:
        folder_mtime = folder.stat().st_mtime
    except OSError:
        folder_mtime = 0.0
    root_entry: dict[str, Any] = {
        "type": "dir",
        "name": folder.name,
        "rel": "",
        "depth": 0,
        "mtime": folder_mtime,
        "has_children": True,
        "is_symlink": folder.is_symlink(),
        "lazy": True,
    }
    root_entry.update(_finder_tag_meta(folder))
    summary.update({
        "tree": "",
        "entries": [root_entry],
        "tree_omitted": False,
        "loaded_dirs": [],
        "default_file": "",
        "readme": "",
        "agent_log_tail": "",
        "has_readme": readme.is_file(),
        "has_agent_log": agent_log.is_file(),
        "mtime": folder_mtime,
        "lazy": True,
    })
    return summary


def _persist_linked_folder(r: dict[str, Any], folder: Path,
                           label: str) -> bool:
    return _persist_linked_path(r, folder, label, "folder")


def _persist_linked_path(r: dict[str, Any], linked_path: Path,
                         label: str, item_type: str) -> bool:
    run_dir = r.get("run_dir")
    if not run_dir:
        raise HTTPException(400, "session has no run directory")
    if r.get("kind") == "task":
        path = Path(run_dir) / "state.json"
        data = _safe_read_json(path) or {}
        task = r.get("task", "")
        if task not in data or not isinstance(data[task], dict):
            raise HTTPException(404, "task not found in state.json")
        changed = _add_linked_path(data[task], linked_path, label, item_type)
        _safe_write_json(path, data)
        return changed
    path = Path(run_dir) / "session.json"
    if not path.exists():
        raise HTTPException(400, "session.json not found")
    data = _safe_read_json(path) or {}
    changed = _add_linked_path(data, linked_path, label, item_type)
    _safe_write_json(path, data)
    return changed


def _persist_linked_url(r: dict[str, Any], url: str, label: str) -> bool:
    url = _coerce_linked_url(url)
    run_dir = r.get("run_dir")
    if not run_dir:
        raise HTTPException(400, "session has no run directory")
    if r.get("kind") == "task":
        path = Path(run_dir) / "state.json"
        data = _safe_read_json(path) or {}
        task = r.get("task", "")
        if task not in data or not isinstance(data[task], dict):
            raise HTTPException(404, "task not found in state.json")
        changed = _add_linked_url(data[task], url, label)
        _safe_write_json(path, data)
        return changed
    path = Path(run_dir) / "session.json"
    if not path.exists():
        raise HTTPException(400, "session.json not found")
    data = _safe_read_json(path) or {}
    changed = _add_linked_url(data, url, label)
    _safe_write_json(path, data)
    return changed


def _persist_unlink_folder(r: dict[str, Any], raw_path: str) -> bool:
    run_dir = r.get("run_dir")
    if not run_dir:
        raise HTTPException(400, "session has no run directory")
    if r.get("kind") == "task":
        path = Path(run_dir) / "state.json"
        data = _safe_read_json(path) or {}
        task = r.get("task", "")
        if task not in data or not isinstance(data[task], dict):
            raise HTTPException(404, "task not found in state.json")
        changed = _remove_linked_folder(data[task], raw_path)
        _safe_write_json(path, data)
        return changed
    path = Path(run_dir) / "session.json"
    if not path.exists():
        raise HTTPException(400, "session.json not found")
    data = _safe_read_json(path) or {}
    changed = _remove_linked_folder(data, raw_path)
    _safe_write_json(path, data)
    return changed


def _write_linked_folders_to_run_dir(run_dir: Path,
                                     folders: list[dict[str, str]]) -> int:
    folders = _normalize_linked_folders(folders)
    if not folders:
        return 0
    session_json = run_dir / "session.json"
    for _ in range(25):
        if session_json.exists():
            break
        time.sleep(0.1)
    if not session_json.exists():
        raise FileNotFoundError(f"session.json not found at {session_json}")
    data = _safe_read_json(session_json) or {}
    data["linked_folders"] = folders
    _safe_write_json(session_json, data)
    return len(folders)


def _copy_linked_folders_to_spawned_run(
    outputs_dir: Path,
    src: dict[str, Any],
    spawn_result: dict[str, Any],
    *,
    exclude_run_id: str = "",
    resume_id: str = "",
    label: str = "",
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    folders = _normalize_linked_folders(src.get("linked_folders"))
    if not folders:
        return {"copied": 0, "run_dir": "", "warning": ""}

    run_dir_raw = (spawn_result.get("run_dir") or "").strip()
    if run_dir_raw:
        try:
            run_dir = Path(run_dir_raw).expanduser().resolve()
            copied = _write_linked_folders_to_run_dir(run_dir, folders)
            return {"copied": copied, "run_dir": str(run_dir), "warning": ""}
        except (OSError, FileNotFoundError) as e:
            return {"copied": 0, "run_dir": run_dir_raw, "warning": str(e)}

    tmux_session = (spawn_result.get("tmux_session") or "").strip()
    deadline = time.time() + max(0.5, timeout_s)
    last_warning = "spawned run not discovered yet"
    while time.time() < deadline:
        for row in _discover_runs(outputs_dir):
            if exclude_run_id and row.get("run_id") == exclude_run_id:
                continue
            matches_tmux = bool(tmux_session and row.get("tmux_session") == tmux_session)
            matches_resume = bool(
                resume_id
                and row.get("resume_id") == resume_id
                and row.get("alive")
                and (not label or row.get("label") == label or row.get("display_name") == label)
            )
            if not (matches_tmux or matches_resume):
                continue
            run_dir = row.get("run_dir") or ""
            if not run_dir:
                last_warning = "spawned run has no run directory"
                continue
            try:
                copied = _write_linked_folders_to_run_dir(Path(run_dir), folders)
                return {"copied": copied, "run_dir": run_dir, "warning": ""}
            except (OSError, FileNotFoundError) as e:
                last_warning = str(e)
        time.sleep(0.2)
    return {"copied": 0, "run_dir": "", "warning": last_warning}


def _linked_folder_paths(r: dict[str, Any]) -> set[str]:
    linked: set[str] = set()
    for rec in _normalize_linked_folders(r.get("linked_folders")):
        raw = rec.get("path")
        if not raw:
            continue
        if rec.get("type") == "url" or _is_linked_url(raw):
            continue
        try:
            linked.add(str(Path(raw).expanduser().resolve()))
        except OSError:
            continue
    return linked


def _resolve_linked_folder_for_run(r: dict[str, Any], raw: str) -> Path:
    folder = _resolve_linked_path_for_run(r, raw)
    if not folder.is_dir():
        raise HTTPException(400, "linked path is not a folder")
    return folder


def _resolve_linked_path_for_run(r: dict[str, Any], raw: str) -> Path:
    linked_path = _resolve_linked_path(raw)
    if str(linked_path) not in _linked_folder_paths(r):
        raise HTTPException(400, "path is not linked to this session")
    return linked_path


def _resolve_folder_file(folder: Path, rel: str) -> Path:
    rel = _clean_folder_rel(rel)
    if folder.is_file():
        if rel:
            raise HTTPException(400, "linked file does not have child paths")
        target = folder.resolve()
        if not _is_safe_linked_target(target, folder):
            raise HTTPException(400, "file path escapes allowed linked roots")
        return target
    if not rel:
        raise HTTPException(400, "file path is required")
    try:
        target = (folder / rel).resolve()
    except OSError:
        raise HTTPException(400, "invalid file path")
    if not _is_safe_linked_target(target, folder):
        raise HTTPException(400, "file path escapes allowed linked roots")
    if not target.is_file():
        raise HTTPException(404, "file not found")
    return target


def _resolve_folder_target(folder: Path, rel: str) -> Path:
    rel = _clean_folder_rel(rel)
    if folder.is_file():
        if rel:
            raise HTTPException(400, "linked file does not have child paths")
        target = folder.resolve()
        if not _is_safe_linked_target(target, folder):
            raise HTTPException(400, "path escapes allowed linked roots")
        return target
    try:
        target = (folder / rel).resolve() if rel else folder.resolve()
    except OSError:
        raise HTTPException(400, "invalid path")
    if not _is_safe_linked_target(target, folder):
        raise HTTPException(400, "path escapes allowed linked roots")
    if not (target.is_file() or target.is_dir()):
        raise HTTPException(404, "path not found")
    return target


def _open_path_on_host(path: Path) -> tuple[bool, str]:
    if sys.platform == "darwin":
        cmd = ["open", str(path)]
    elif sys.platform.startswith("linux"):
        if not shutil.which("xdg-open"):
            return False, "xdg-open not found"
        cmd = ["xdg-open", str(path)]
    else:
        return False, f"unsupported platform: {sys.platform}"
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _quicklook_file_on_host(path: Path) -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "Quick Look is only available on macOS"
    qlmanage = shutil.which("qlmanage") or "/usr/bin/qlmanage"
    if not Path(qlmanage).exists():
        return False, "qlmanage not found"
    key = str(path)
    old = _QUICKLOOK_PROCS.get(key)
    if old and old.poll() is None:
        try:
            old.terminate()
            old.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                old.kill()
            except OSError:
                pass
    try:
        # `qlmanage -p` is macOS-native Quick Look and cannot be embedded in
        # the browser. Revealing first makes the action visible even when the
        # Quick Look panel opens behind the current Chrome window/Space.
        subprocess.Popen(["/usr/bin/open", "-R", str(path)],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        proc = subprocess.Popen([qlmanage, "-p", str(path)],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
        _QUICKLOOK_PROCS[key] = proc
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _open_folder_on_host(folder: Path) -> tuple[bool, str]:
    return _open_path_on_host(folder)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_iso_epoch(value: str | None) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return time.mktime(dt.timetuple())
        return dt.timestamp()
    except (TypeError, ValueError, OSError):
        return None


def _path_birth_or_mtime(p: Path) -> float:
    try:
        st = p.stat()
    except OSError:
        return 0.0
    return float(getattr(st, "st_birthtime", 0.0) or st.st_mtime)


def _norm_agent(agent: str | None) -> str:
    a = (agent or "").strip().lower()
    if "codex" in a:
        return "codex"
    if "claude" in a:
        return "claude"
    if a in {"cursor", "agent", "cursor-agent"} or "cursor" in a:
        return "cursor"
    return a


_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _resume_cmd_for(agent: str, resume_id: str) -> str:
    qid = shlex.quote(resume_id)
    if agent == "codex":
        return f"codex resume --dangerously-bypass-approvals-and-sandbox {qid}"
    if agent == "claude":
        return f"claude --resume {qid}"
    if agent == "cursor":
        return f"agent --resume {qid}"
    return f"{shlex.quote(agent or 'agent')} --resume {qid}"


def _build_resume_meta(agent: str, resume_id: str, source: str,
                       source_path: str = "",
                       confidence: str = "high") -> dict[str, str]:
    agent = _norm_agent(agent)
    meta = {
        "resume_agent": agent,
        "resume_id": resume_id,
        "resume_cmd": _resume_cmd_for(agent, resume_id),
        "resume_source": source,
        "resume_recorded_at": _iso_now(),
        "resume_confidence": confidence,
    }
    if source_path:
        meta["resume_source_path"] = source_path
    return meta


def _build_inherited_resume_meta(
    src: dict[str, Any],
    agent: str,
    resume_id: str,
) -> dict[str, str]:
    meta = _build_resume_meta(
        agent,
        resume_id,
        "orchestrator-resume",
        _run_metadata_path(src),
        confidence="exact",
    )
    if src.get("run_id"):
        meta["resumed_from_run_id"] = str(src.get("run_id") or "")
    if src.get("run_dir"):
        meta["resumed_from_run_dir"] = str(src.get("run_dir") or "")
    source_resume = src.get("resume") if isinstance(src.get("resume"), dict) else {}
    source = (
        source_resume.get("source")
        or src.get("resume_source")
        or ""
    )
    if source:
        meta["resumed_from_resume_source"] = str(source)
    return meta


def _preallocate_native_resume_meta(agent: str) -> tuple[dict[str, str], str]:
    kind = _norm_agent(agent)
    if kind == "claude":
        resume_id = str(uuid.uuid4())
        return (
            _build_resume_meta(
                "claude", resume_id, "claude-session-id-preallocated",
                confidence="exact"),
            "",
        )
    if kind == "cursor":
        try:
            proc = subprocess.run(
                ["agent", "create-chat"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {}, f"agent create-chat failed: {exc}"
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return {}, f"agent create-chat failed: {err or proc.returncode}"
        for token in re.split(r"\s+", proc.stdout.strip()):
            if token:
                return (
                    _build_resume_meta(
                        "cursor", token, "cursor-create-chat",
                        confidence="exact"),
                    "",
                )
        return {}, "agent create-chat returned no chat id"
    return {}, ""


def _saved_resume_meta(data: dict[str, Any]) -> dict[str, str]:
    nested = data.get("resume") if isinstance(data.get("resume"), dict) else {}
    native = data.get("native_resume") if isinstance(data.get("native_resume"), dict) else {}
    resume_id = data.get("resume_id") or nested.get("id") or native.get("current_id")
    resume_agent = _norm_agent(
        data.get("resume_agent") or nested.get("agent", "")
        or native.get("provider", "")
    )
    resume_cmd = data.get("resume_cmd") or nested.get("cmd") or native.get("cmd")
    if resume_id and resume_agent:
        resume_cmd = _resume_cmd_for(resume_agent, str(resume_id))
    if not resume_id and not resume_cmd:
        return {}
    return {
        "resume_agent": resume_agent,
        "resume_id": resume_id or "",
        "resume_cmd": resume_cmd or "",
        "resume_source": (
            data.get("resume_source") or nested.get("source", "")
            or native.get("source", "")
        ),
        "resume_recorded_at": (
            data.get("resume_recorded_at") or nested.get("recorded_at", "")
            or native.get("captured_at", "")
        ),
        "resume_source_path": (
            data.get("resume_source_path") or nested.get("source_path", "")
            or native.get("source_path", "")
        ),
        "resume_confidence": (
            data.get("resume_confidence") or nested.get("confidence", "")
            or native.get("confidence", "")
        ),
    }


def _add_resume_fields(row: dict[str, Any], data: dict[str, Any]) -> None:
    meta = _saved_resume_meta(data)
    row["resume"] = {
        "agent": meta.get("resume_agent", ""),
        "id": meta.get("resume_id", ""),
        "cmd": meta.get("resume_cmd", ""),
        "source": meta.get("resume_source", ""),
        "recorded_at": meta.get("resume_recorded_at", ""),
        "source_path": meta.get("resume_source_path", ""),
        "confidence": meta.get("resume_confidence", ""),
    } if meta else {}
    row["resume_id"] = meta.get("resume_id", "")
    row["resume_cmd"] = meta.get("resume_cmd", "")


def _extract_resume_from_text(agent: str, text: str) -> dict[str, str]:
    if not text:
        return {}
    kind = _norm_agent(agent)
    patterns = []
    if kind == "codex":
        patterns.append(re.compile(r"\bcodex\s+resume\s+(" + _UUID_RE.pattern + r")"))
    elif kind == "claude":
        patterns.append(re.compile(r"\bclaude\s+(?:--resume|-r)\s+(" + _UUID_RE.pattern + r")"))
    elif kind == "cursor":
        patterns.append(re.compile(r"\bagent\s+--resume\s+(" + _UUID_RE.pattern + r")"))
    patterns.append(_UUID_RE)
    for pat in patterns:
        m = pat.search(text)
        if not m:
            continue
        resume_id = m.group(1) if pat.groups else m.group(0)
        return _build_resume_meta(kind, resume_id.lower(), "terminal-text")
    return {}


def _claude_project_dir(cwd: str) -> Path:
    slug = (cwd or "").replace("/", "-") or "unknown"
    return Path.home() / ".claude" / "projects" / slug


def _cursor_project_slugs(cwd: str) -> list[str]:
    if not cwd:
        return []
    out: list[str] = []
    try:
        cur = Path(cwd).expanduser().resolve()
    except OSError:
        cur = Path(cwd).expanduser()
    for p in [cur, cur.parent]:
        slug = str(p).strip("/").replace("/", "-")
        if slug and slug not in out:
            out.append(slug)
    return out


def _candidate_ok(candidate_start: float, started_epoch: Optional[float]) -> bool:
    if not started_epoch or not candidate_start:
        return True
    return candidate_start >= started_epoch - 600


def _candidate_score(candidate_start: float, started_epoch: Optional[float],
                     mtime: float, cwd_match: bool = True) -> tuple:
    if started_epoch and candidate_start:
        distance = abs(candidate_start - started_epoch)
        return (1 if cwd_match else 0, -distance, mtime)
    return (1 if cwd_match else 0, mtime, candidate_start)


def _find_codex_resume(cwd: str, started_at: str) -> dict[str, str]:
    root = Path.home() / ".codex" / "sessions"
    if not root.exists():
        return {}
    started_epoch = _parse_iso_epoch(started_at)
    best: tuple | None = None
    best_meta: dict[str, str] = {}
    try:
        paths = sorted(root.glob("*/*/*/rollout-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:500]
    except OSError:
        return {}
    for path in paths:
        try:
            with path.open() as f:
                first = f.readline()
            obj = json.loads(first)
            payload = obj.get("payload") or {}
            if obj.get("type") != "session_meta" or not isinstance(payload, dict):
                continue
            resume_id = payload.get("id")
            pcwd = payload.get("cwd", "")
            candidate_start = (
                _parse_iso_epoch(payload.get("timestamp"))
                or _path_birth_or_mtime(path)
            )
            if not resume_id or (cwd and pcwd != cwd):
                continue
            if not _candidate_ok(candidate_start, started_epoch):
                continue
            score = _candidate_score(candidate_start, started_epoch,
                                     path.stat().st_mtime, pcwd == cwd)
            if best is None or score > best:
                best = score
                best_meta = _build_resume_meta("codex", str(resume_id),
                                               "codex-session-file", str(path))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return best_meta


def _find_codex_resume_near_start(
    cwd: str,
    started_at: str,
    *,
    before_s: float = 10.0,
    after_s: float = 180.0,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    root = Path.home() / ".codex" / "sessions"
    started_epoch = _parse_iso_epoch(started_at)
    if not root.exists() or not started_epoch:
        return {}, []
    candidates: list[tuple[float, dict[str, str]]] = []
    try:
        paths = sorted(root.glob("*/*/*/rollout-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:500]
    except OSError:
        return {}, []
    for path in paths:
        try:
            with path.open() as f:
                first = f.readline()
            obj = json.loads(first)
            payload = obj.get("payload") or {}
            if obj.get("type") != "session_meta" or not isinstance(payload, dict):
                continue
            resume_id = str(payload.get("id") or "")
            pcwd = str(payload.get("cwd") or "")
            candidate_start = (
                _parse_iso_epoch(payload.get("timestamp"))
                or _path_birth_or_mtime(path)
            )
            if not resume_id or (cwd and pcwd != cwd) or not candidate_start:
                continue
            if candidate_start < started_epoch - before_s:
                continue
            if candidate_start > started_epoch + after_s:
                continue
            distance = abs(candidate_start - started_epoch)
            meta = _build_resume_meta(
                "codex", resume_id, "codex-session-file", str(path),
                confidence="high",
            )
            meta["resume_candidate_distance_s"] = f"{distance:.3f}"
            candidates.append((distance, meta))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    candidates.sort(key=lambda item: (item[0], item[1].get("resume_source_path", "")))
    flat_candidates = [meta for _distance, meta in candidates]
    if not candidates:
        return {}, []
    if len(candidates) == 1:
        return candidates[0][1], flat_candidates
    first_dist = candidates[0][0]
    second_dist = candidates[1][0]
    if first_dist <= 10.0 and second_dist - first_dist >= 5.0:
        return candidates[0][1], flat_candidates
    return {}, flat_candidates


def _find_claude_resume(cwd: str, started_at: str) -> dict[str, str]:
    root = _claude_project_dir(cwd)
    if not root.exists():
        return {}
    started_epoch = _parse_iso_epoch(started_at)
    best: tuple | None = None
    best_meta: dict[str, str] = {}
    try:
        paths = sorted(root.glob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:200]
    except OSError:
        return {}
    for path in paths:
        resume_id = path.stem
        candidate_start = _path_birth_or_mtime(path)
        timestamp_seen = False
        cwd_match = False
        try:
            with path.open() as f:
                for idx, line in enumerate(f):
                    if idx >= 200:
                        break
                    obj = json.loads(line)
                    if obj.get("sessionId"):
                        resume_id = str(obj["sessionId"])
                    if obj.get("cwd") == cwd:
                        cwd_match = True
                    if obj.get("timestamp") and not timestamp_seen:
                        candidate_start = (
                            _parse_iso_epoch(obj.get("timestamp"))
                            or candidate_start
                        )
                        timestamp_seen = True
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if cwd and not cwd_match:
            continue
        if not _candidate_ok(candidate_start, started_epoch):
            continue
        score = _candidate_score(candidate_start, started_epoch,
                                 path.stat().st_mtime, cwd_match)
        if best is None or score > best:
            best = score
            best_meta = _build_resume_meta("claude", resume_id.lower(),
                                           "claude-jsonl", str(path))
    return best_meta


def _find_cursor_resume(cwd: str, started_at: str) -> dict[str, str]:
    started_epoch = _parse_iso_epoch(started_at)
    best: tuple | None = None
    best_meta: dict[str, str] = {}
    base = Path.home() / ".cursor" / "projects"
    for slug in _cursor_project_slugs(cwd):
        root = base / slug / "agent-transcripts"
        if not root.exists():
            continue
        try:
            paths = sorted(root.glob("*/*.jsonl"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:250]
        except OSError:
            continue
        for path in paths:
            if path.parent.name == "subagents":
                continue
            resume_id = path.parent.name
            if not _UUID_RE.fullmatch(resume_id):
                continue
            candidate_start = _path_birth_or_mtime(path.parent)
            if not _candidate_ok(candidate_start, started_epoch):
                continue
            score = _candidate_score(candidate_start, started_epoch,
                                     path.stat().st_mtime, True)
            if best is None or score > best:
                best = score
                best_meta = _build_resume_meta("cursor", resume_id.lower(),
                                               "cursor-transcript", str(path))
    return best_meta


def _discover_resume_metadata(r: dict[str, Any],
                              terminal_text: str = "") -> dict[str, str]:
    agent = _norm_agent(r.get("agent", ""))
    if not agent:
        return {}
    text_meta = _extract_resume_from_text(agent, terminal_text)
    if text_meta:
        return text_meta
    cwd = r.get("cwd", "") or ""
    started_at = r.get("started_at", "") or ""
    if agent == "codex":
        return _find_codex_resume(cwd, started_at)
    if agent == "claude":
        return _find_claude_resume(cwd, started_at)
    if agent == "cursor":
        return _find_cursor_resume(cwd, started_at)
    return {}


def _apply_resume_meta_to_row(r: dict[str, Any], meta: dict[str, str]) -> None:
    if not meta:
        return
    r["resume_id"] = meta.get("resume_id", "")
    r["resume_cmd"] = meta.get("resume_cmd", "")
    r["resume"] = {
        "agent": meta.get("resume_agent", ""),
        "id": meta.get("resume_id", ""),
        "cmd": meta.get("resume_cmd", ""),
        "source": meta.get("resume_source", ""),
        "recorded_at": meta.get("resume_recorded_at", ""),
        "source_path": meta.get("resume_source_path", ""),
        "confidence": meta.get("resume_confidence", ""),
    }


def _ensure_resume_metadata_for_run(r: dict[str, Any]) -> tuple[bool, str]:
    if r.get("resume_id"):
        return True, ""
    agent = _norm_agent(r.get("agent", ""))
    if not agent:
        return False, "missing agent"
    meta: dict[str, str] = {}
    if agent == "codex":
        meta, _candidates = _find_codex_resume_near_start(
            r.get("cwd", ""), r.get("started_at", ""))
    if not meta:
        meta = _discover_resume_metadata(r)
    if not meta or not meta.get("resume_id"):
        return False, "native resume id not found"
    if r.get("run_dir"):
        _persist_resume_metadata(r, meta)
    _apply_resume_meta_to_row(r, meta)
    return True, ""


def _snapshot_entry_from_run(r: dict[str, Any],
                             slot_idx: int | None,
                             order_idx: int) -> dict[str, Any]:
    r = _run_with_native_model_effort(r)
    resume = r.get("resume") if isinstance(r.get("resume"), dict) else {}
    agent = _norm_agent(resume.get("agent") or r.get("agent") or "")
    label = r.get("label") or r.get("display_name") or r.get("task") or r.get("run_name") or ""
    model = str(r.get("model") or "").strip()
    effort = str(r.get("effort") or "").strip().lower()
    effort_mode = str(r.get("effort_mode") or "").strip().lower()
    panel_state = str(r.get("panel_state") or "").strip().lower()
    if panel_state not in ALLOWED_PANEL_STATES:
        panel_state = ""
    terminal_theme = _normalize_terminal_theme(str(r.get("terminal_theme") or ""))
    model = "" if model in _MODEL_DEFAULTS else model
    return {
        "source_run_id": r.get("run_id", ""),
        "source_run_dir": r.get("run_dir", ""),
        "source_metadata_path": _run_metadata_path(r),
        "kind": r.get("kind", ""),
        "task": r.get("task", ""),
        "run_name": r.get("run_name", ""),
        "display_name": r.get("display_name") or label,
        "label": label,
        "agent": agent,
        "model": model,
        "effort": effort,
        "effort_mode": effort_mode,
        "model_source": r.get("model_source", ""),
        "effort_source": r.get("effort_source", ""),
        "effort_mode_source": r.get("effort_mode_source", ""),
        "cwd": r.get("cwd", ""),
        "started_at": r.get("started_at", ""),
        "tmux_session": r.get("tmux_session", ""),
        "resume_id": r.get("resume_id", ""),
        "resume_cmd": r.get("resume_cmd", ""),
        "resume_source": resume.get("source") or r.get("resume_source", ""),
        "resume_recorded_at": resume.get("recorded_at") or r.get("resume_recorded_at", ""),
        "linked_folders": _normalize_linked_folders(r.get("linked_folders")),
        "panel_state": panel_state,
        "terminal_theme": terminal_theme,
        "slot": slot_idx,
        "order": order_idx,
    }


def _load_active_snapshot(outputs_dir: Path) -> dict[str, Any]:
    path = _active_snapshot_path(outputs_dir)
    data = _safe_read_json(path) if path.exists() else None
    return data if isinstance(data, dict) else {}


def _active_snapshot_previous_slot_maps(
    previous: dict[str, Any],
) -> tuple[list[str], dict[str, int], dict[str, int]]:
    slots = previous.get("slots") if isinstance(previous.get("slots"), list) else []
    slot_ids = [str(x) if x else "" for x in slots]
    slot_by_run = {run_id: idx for idx, run_id in enumerate(slot_ids) if run_id}
    slot_by_resume: dict[str, int] = {}
    sessions = previous.get("sessions") if isinstance(previous.get("sessions"), list) else []
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        run_id = str(entry.get("source_run_id") or "")
        resume_id = str(entry.get("resume_id") or "")
        slot_idx: int | None = None
        raw_slot = entry.get("slot")
        if isinstance(raw_slot, int) and raw_slot >= 0:
            slot_idx = raw_slot
        elif run_id in slot_by_run:
            slot_idx = slot_by_run[run_id]
        if slot_idx is None:
            continue
        if run_id:
            slot_by_run.setdefault(run_id, slot_idx)
        if resume_id:
            slot_by_resume.setdefault(resume_id, slot_idx)
    return slot_ids, slot_by_run, slot_by_resume


def _build_active_snapshot(
    outputs_dir: Path,
    *,
    layout_name: str = "",
    slot_ids: list[str] | None = None,
    saved_by: str = "manual",
    previous_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the active-session restore snapshot.

    Manual saves pass browser slots. Background autosave has no browser state,
    so it reuses the previous slot mapping by run id, then by native resume id.
    """
    previous = previous_snapshot if isinstance(previous_snapshot, dict) else {}
    explicit_slots = slot_ids is not None
    if explicit_slots:
        active_slot_ids = [str(x) if x else "" for x in (slot_ids or [])]
        slot_pos = {run_id: i for i, run_id in enumerate(active_slot_ids) if run_id}
        previous_slot_ids: list[str] = []
        previous_run_slots: dict[str, int] = {}
        previous_resume_slots: dict[str, int] = {}
    else:
        previous_slot_ids, previous_run_slots, previous_resume_slots = (
            _active_snapshot_previous_slot_maps(previous)
        )
        active_slot_ids = previous_slot_ids
        slot_pos = previous_run_slots
        if not layout_name:
            layout_name = str(previous.get("layout") or "")

    live_runs = [r for r in _discover_runs(outputs_dir) if r.get("alive")]
    live_runs.sort(key=lambda r: (
        0 if r.get("run_id") in slot_pos else 1,
        slot_pos.get(r.get("run_id", ""), 10_000),
        r.get("started_at") or "",
        r.get("run_id") or "",
    ))

    candidates: list[tuple[dict[str, Any], int | None, int]] = []
    skipped: list[dict[str, str]] = []
    for discovered_idx, r in enumerate(live_runs):
        ok, reason = _ensure_resume_metadata_for_run(r)
        agent = _norm_agent(
            (r.get("resume") or {}).get("agent")
            if isinstance(r.get("resume"), dict) else r.get("agent", "")
        ) or _norm_agent(r.get("agent", ""))
        if not ok:
            skipped.append({
                "run_id": r.get("run_id", ""),
                "display_name": r.get("display_name") or r.get("task") or r.get("run_name") or "",
                "reason": reason,
            })
            continue
        if agent not in {"cursor", "claude", "codex"}:
            skipped.append({
                "run_id": r.get("run_id", ""),
                "display_name": r.get("display_name") or r.get("task") or r.get("run_name") or "",
                "reason": f"unsupported agent: {agent or 'unknown'}",
            })
            continue
        slot_idx: int | None = slot_pos.get(r.get("run_id", ""))
        if not explicit_slots and slot_idx is None:
            resume_id = str(r.get("resume_id") or "")
            slot_idx = previous_resume_slots.get(resume_id)
        candidates.append((r, slot_idx, discovered_idx))

    candidates.sort(key=lambda item: (
        0 if item[1] is not None else 1,
        item[1] if item[1] is not None else 10_000,
        item[0].get("started_at") or "",
        item[0].get("run_id") or "",
        item[2],
    ))
    sessions_out = [
        _snapshot_entry_from_run(r, slot_idx, order_idx)
        for order_idx, (r, slot_idx, _discovered_idx) in enumerate(candidates)
    ]

    saved_source_ids = {s.get("source_run_id", "") for s in sessions_out}
    if explicit_slots:
        snapshot_slots = [
            run_id if run_id in saved_source_ids else ""
            for run_id in active_slot_ids
        ]
    else:
        max_slot = max(
            [len(active_slot_ids) - 1] +
            [int(s["slot"]) for s in sessions_out if isinstance(s.get("slot"), int)]
        )
        snapshot_slots = list(active_slot_ids)
        while len(snapshot_slots) <= max_slot:
            snapshot_slots.append("")
        for s in sessions_out:
            slot = s.get("slot")
            run_id = str(s.get("source_run_id") or "")
            if isinstance(slot, int) and slot >= 0 and run_id:
                snapshot_slots[slot] = run_id
        snapshot_slots = [
            run_id if run_id in saved_source_ids else ""
            for run_id in snapshot_slots
        ]

    snapshot = {
        "schema_version": 3,
        "saved_at": _iso_now(),
        "saved_by": saved_by,
        "layout": layout_name,
        "slots": snapshot_slots,
        "sessions": sessions_out,
        "skipped": skipped,
    }
    if saved_by.startswith("auto"):
        snapshot["autosave_interval_seconds"] = _active_snapshot_autosave_interval()
    return snapshot


def _save_active_snapshot(
    outputs_dir: Path,
    *,
    layout_name: str = "",
    slot_ids: list[str] | None = None,
    saved_by: str = "manual",
) -> tuple[Path, dict[str, Any]]:
    with _ACTIVE_SNAPSHOT_LOCK:
        previous = _load_active_snapshot(outputs_dir)
        snapshot = _build_active_snapshot(
            outputs_dir,
            layout_name=layout_name,
            slot_ids=slot_ids,
            saved_by=saved_by,
            previous_snapshot=previous,
        )
        path = _active_snapshot_path(outputs_dir)
        _safe_write_json(path, snapshot)
        return path, snapshot


_CODEX_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
_CLAUDE_CLI_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_MODEL_DEFAULTS = {"", "default"}


def _json_string_fields(obj: Any, keys: set[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                out.append((k, v))
            out.extend(_json_string_fields(v, keys))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_json_string_fields(item, keys))
    return out


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _resume_source_transcript_path(source_path: str) -> Path | None:
    if not source_path:
        return None
    try:
        path = Path(source_path).expanduser()
    except (OSError, ValueError):
        return None
    if path.suffix == ".jsonl" and path.is_file():
        return path
    return None


def _codex_transcript_path(resume_id: str, started_at: str = "") -> Path | None:
    if not resume_id:
        return None
    root = Path.home() / ".codex" / "sessions"
    started_epoch = _parse_iso_epoch(started_at)
    if started_epoch:
        day = datetime.fromtimestamp(started_epoch)
        day_dir = root / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")
        try:
            matches = sorted(day_dir.glob(f"*{resume_id}*.jsonl"))
        except OSError:
            matches = []
        if matches:
            return matches[-1]
    if not _truthy_env("ORCH_SLOW_TRANSCRIPT_SCAN"):
        return None
    try:
        matches = sorted(root.rglob(f"*{resume_id}*.jsonl"))
    except OSError:
        return None
    return matches[-1] if matches else None


def _codex_transcript_model_effort(
    resume_id: str,
    *,
    started_at: str = "",
    source_path: str = "",
) -> dict[str, str]:
    path = (
        _resume_source_transcript_path(source_path)
        or _codex_transcript_path(resume_id, started_at)
    )
    if not path or not path.exists():
        return {}
    model = ""
    effort = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not (
                    '"model":' in line
                    or '"model_reasoning_effort":' in line
                    or '"reasoning_effort":' in line
                    or '"effort":' in line
                ):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for key, value in _json_string_fields(
                    obj,
                    {"model", "model_reasoning_effort", "reasoning_effort", "effort"},
                ):
                    value = value.strip()
                    if key == "model" and value and value not in _MODEL_DEFAULTS:
                        model = value
                    elif key in {"model_reasoning_effort", "reasoning_effort", "effort"}:
                        value = value.lower()
                        if value in _CODEX_EFFORTS:
                            effort = value
    except OSError:
        return {}
    out: dict[str, str] = {}
    if model:
        out["model"] = model
        out["model_source"] = str(path)
    if effort:
        out["effort"] = effort
        out["effort_source"] = str(path)
    return out


_CLAUDE_EFFORT_MODE_RE = re.compile(
    r"Set effort level to (?P<mode>[a-zA-Z_-]+).*?:\s*"
    r"(?P<effort>low|medium|high|xhigh|max)",
    re.I,
)


def _claude_transcript_path(resume_id: str, cwd: str = "") -> Path | None:
    if not resume_id:
        return None
    if cwd:
        path = _claude_project_dir(cwd) / f"{resume_id}.jsonl"
        if path.is_file():
            return path
    if not _truthy_env("ORCH_SLOW_TRANSCRIPT_SCAN"):
        return None
    root = Path.home() / ".claude" / "projects"
    try:
        matches = sorted(root.rglob(f"{resume_id}.jsonl"))
    except OSError:
        return None
    return matches[-1] if matches else None


def _claude_transcript_model_effort(
    resume_id: str,
    *,
    cwd: str = "",
    source_path: str = "",
) -> dict[str, str]:
    path = (
        _resume_source_transcript_path(source_path)
        or _claude_transcript_path(resume_id, cwd)
    )
    if not path or not path.exists():
        return {}
    model = ""
    effort = ""
    effort_mode = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "Set effort level to " in line:
                    m = _CLAUDE_EFFORT_MODE_RE.search(line)
                    if m:
                        effort_mode = m.group("mode").strip().lower()
                        effort = m.group("effort").strip().lower()
                if '"model":' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for key, value in _json_string_fields(obj, {"model"}):
                    value = value.strip()
                    if key == "model" and _is_real_cli_model(value):
                        model = value
    except OSError:
        return {}
    out: dict[str, str] = {}
    if model:
        out["model"] = model
        out["model_source"] = str(path)
    if effort in _CLAUDE_CLI_EFFORTS:
        out["effort"] = effort
        out["effort_source"] = str(path)
    if effort_mode:
        out["effort_mode"] = effort_mode
        out["effort_mode_source"] = str(path)
    return out


def _native_transcript_model_effort(r: dict[str, Any]) -> dict[str, str]:
    """Recover concrete model/effort from native transcripts when possible."""
    resume = r.get("resume") if isinstance(r.get("resume"), dict) else {}
    agent = _norm_agent(resume.get("agent") or r.get("agent") or "")
    resume_id = str(r.get("resume_id") or "").strip()
    source_path = str(resume.get("source_path") or r.get("resume_source_path") or "")
    cwd = str(r.get("cwd") or "")
    started_at = str(r.get("started_at") or "")
    if not resume_id:
        return {}
    if agent == "codex":
        return _codex_transcript_model_effort(
            resume_id,
            started_at=started_at,
            source_path=source_path,
        )
    if agent == "claude":
        return _claude_transcript_model_effort(
            resume_id,
            cwd=cwd,
            source_path=source_path,
        )
    return {}


def _is_real_cli_model(model: str) -> bool:
    model = str(model or "").strip()
    if not model or model in _MODEL_DEFAULTS:
        return False
    # Claude writes placeholder API-error messages with model "<synthetic>".
    # That is not a resumable CLI model and must never be passed to --model.
    if re.fullmatch(r"<[^>]+>", model):
        return False
    return True


def _clean_cli_model_arg(model: str) -> str:
    model = str(model or "").strip()
    return model if _is_real_cli_model(model) else ""


def _run_with_native_model_effort(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    resume = out.get("resume") if isinstance(out.get("resume"), dict) else {}
    agent = _norm_agent(resume.get("agent") or out.get("agent") or "")
    model = str(out.get("model") or "").strip()
    effort = str(out.get("effort") or "").strip().lower()
    effort_mode = str(out.get("effort_mode") or "").strip().lower()
    needs_model = model in _MODEL_DEFAULTS
    needs_effort = not effort
    needs_effort_mode = not effort_mode
    if agent not in {"codex", "claude"} or not (
        needs_model or needs_effort or needs_effort_mode
    ):
        return out
    native = _native_transcript_model_effort(out)
    if needs_model and native.get("model"):
        out["model"] = native["model"]
        out["model_source"] = native.get("model_source", "")
    if needs_effort and native.get("effort"):
        out["effort"] = native["effort"]
        out["effort_source"] = native.get("effort_source", "")
    if needs_effort_mode and native.get("effort_mode"):
        out["effort_mode"] = native["effort_mode"]
        out["effort_mode_source"] = native.get("effort_mode_source", "")
    return out


def _persist_resume_metadata(r: dict[str, Any], meta: dict[str, str],
                             status: str | None = None) -> bool:
    if not meta:
        return False
    run_dir = r.get("run_dir")
    if not run_dir:
        return False
    flat = {
        "resume_agent": meta.get("resume_agent", ""),
        "resume_id": meta.get("resume_id", ""),
        "resume_cmd": meta.get("resume_cmd", ""),
        "resume_source": meta.get("resume_source", ""),
        "resume_source_path": meta.get("resume_source_path", ""),
        "resume_recorded_at": meta.get("resume_recorded_at", _iso_now()),
        "resume_confidence": meta.get("resume_confidence", ""),
    }
    origin = {
        "run_id": meta.get("resumed_from_run_id", ""),
        "run_dir": meta.get("resumed_from_run_dir", ""),
        "resume_source": meta.get("resumed_from_resume_source", ""),
    }
    native = {
        "provider": flat["resume_agent"],
        "current_id": flat["resume_id"],
        "cmd": flat["resume_cmd"],
        "source": flat["resume_source"],
        "source_path": flat["resume_source_path"],
        "captured_at": flat["resume_recorded_at"],
        "confidence": flat["resume_confidence"],
        "status": "captured",
    }
    if any(origin.values()):
        native["resumed_from"] = {k: v for k, v in origin.items() if v}

    def merge_native_resume(data: dict[str, Any]) -> None:
        existing = data.get("native_resume")
        if not isinstance(existing, dict):
            existing = {}
        history = existing.get("history")
        if not isinstance(history, list):
            history = []
        prev_id = str(existing.get("current_id") or data.get("resume_id") or "")
        if prev_id and prev_id != flat["resume_id"]:
            history.append({
                "id": prev_id,
                "provider": existing.get("provider") or data.get("resume_agent", ""),
                "cmd": existing.get("cmd") or data.get("resume_cmd", ""),
                "source": existing.get("source") or data.get("resume_source", ""),
                "source_path": (
                    existing.get("source_path")
                    or data.get("resume_source_path", "")
                ),
                "captured_at": (
                    existing.get("captured_at")
                    or data.get("resume_recorded_at", "")
                ),
                "confidence": (
                    existing.get("confidence")
                    or data.get("resume_confidence", "")
                ),
            })
        data["native_resume"] = {**existing, **native, "history": history[-20:]}

    try:
        if r.get("kind") == "task":
            path = Path(run_dir) / "state.json"
            data = _safe_read_json(path) or {}
            task = r.get("task", "")
            if task not in data or not isinstance(data[task], dict):
                return False
            data[task].update(flat)
            if origin["run_id"]:
                data[task]["resumed_from_run_id"] = origin["run_id"]
            if origin["run_dir"]:
                data[task]["resumed_from_run_dir"] = origin["run_dir"]
            if origin["resume_source"]:
                data[task]["resumed_from_resume_source"] = origin["resume_source"]
            merge_native_resume(data[task])
            if status:
                data[task]["status"] = status
            if status == "stopped":
                data[task]["stopped_at"] = _iso_now()
            _safe_write_json(path, data)
            return True

        path = Path(run_dir) / "session.json"
        data = _safe_read_json(path) or {}
        data.update(flat)
        if origin["run_id"]:
            data["resumed_from_run_id"] = origin["run_id"]
        if origin["run_dir"]:
            data["resumed_from_run_dir"] = origin["run_dir"]
        if origin["resume_source"]:
            data["resumed_from_resume_source"] = origin["resume_source"]
        merge_native_resume(data)
        data["resume"] = {
            "agent": flat["resume_agent"],
            "id": flat["resume_id"],
            "cmd": flat["resume_cmd"],
            "source": flat["resume_source"],
            "recorded_at": flat["resume_recorded_at"],
            "source_path": flat["resume_source_path"],
            "confidence": flat["resume_confidence"],
        }
        if status:
            data["status"] = status
        if status == "stopped":
            data["stopped_at"] = _iso_now()
        _safe_write_json(path, data)
        return True
    except OSError:
        return False


def _persist_resume_capture_status(
    r: dict[str, Any],
    status: str,
    *,
    error: str = "",
    candidates: Optional[list[dict[str, str]]] = None,
) -> bool:
    run_dir = r.get("run_dir")
    if not run_dir:
        return False
    capture = {
        "status": status,
        "updated_at": _iso_now(),
    }
    if error:
        capture["error"] = error
    if candidates is not None:
        capture["candidates"] = [
            {
                "id": c.get("resume_id", ""),
                "source": c.get("resume_source", ""),
                "source_path": c.get("resume_source_path", ""),
                "distance_s": c.get("resume_candidate_distance_s", ""),
                "confidence": c.get("resume_confidence", ""),
            }
            for c in candidates[:8]
        ]
    try:
        if r.get("kind") == "task":
            path = Path(run_dir) / "state.json"
            data = _safe_read_json(path) or {}
            task = r.get("task", "")
            if task not in data or not isinstance(data[task], dict):
                return False
            existing = data[task].get("native_resume")
            if not isinstance(existing, dict):
                existing = {}
            data[task]["native_resume"] = {**existing, "capture": capture}
            data[task]["resume_capture_status"] = status
            if error:
                data[task]["resume_capture_error"] = error
            _safe_write_json(path, data)
            return True

        path = Path(run_dir) / "session.json"
        data = _safe_read_json(path) or {}
        existing = data.get("native_resume")
        if not isinstance(existing, dict):
            existing = {}
        data["native_resume"] = {**existing, "capture": capture}
        data["resume_capture_status"] = status
        if error:
            data["resume_capture_error"] = error
        _safe_write_json(path, data)
        return True
    except OSError:
        return False


_AGENT_EXIT_MARKER = "--- Agent exited ---"


def _agent_exited(session: str, pane_text: str | None = None) -> bool:
    if not session or not tmux_alive(session):
        return True
    text = pane_text if pane_text is not None else tmux_capture(session)
    return _AGENT_EXIT_MARKER in text


def _graceful_stop_agent(session: str, agent: str,
                         timeout_s: float = 12.0) -> dict[str, Any]:
    if not session:
        return {"ok": False, "reason": "no tmux session"}
    if not tmux_alive(session):
        return {"ok": True, "reason": "tmux session already gone"}

    initial_pane = tmux_capture(session)
    if _agent_exited(session, initial_pane):
        return {"ok": True, "reason": "agent already exited"}

    ok, err = tmux_send_key(session, "C-c")
    if not ok:
        return {"ok": False, "reason": f"failed to send C-c: {err}"}

    deadline = time.time() + max(2.0, timeout_s)
    second_sigint_sent = False
    last_reason = "waiting for agent exit"
    while time.time() < deadline:
        time.sleep(0.35)
        if not tmux_alive(session):
            return {"ok": True, "reason": "tmux session exited"}
        pane = tmux_capture(session)
        if _agent_exited(session, pane):
            return {"ok": True, "reason": "agent exited"}
        if not second_sigint_sent and time.time() > deadline - (timeout_s * 0.65):
            # Several TUIs use first Ctrl+C as "interrupt current turn" and a
            # second Ctrl+C as "exit". Do this while still leaving most of the
            # timeout for the CLI to print/persist resume metadata.
            ok2, err2 = tmux_send_key(session, "C-c")
            second_sigint_sent = True
            if not ok2:
                last_reason = f"failed to send second C-c: {err2}"
    return {"ok": False, "reason": last_reason}


def _detect_background_active(text: str) -> tuple[bool, str]:
    """Detect agent status lines that mean work is still running off-screen.

    Match only Codex/agent status rows, not arbitrary assistant prose that may
    mention phrases such as "2 shells still running" while explaining behavior.
    """
    for line in reversed(text.splitlines()[-24:]):
        clean = line.strip()
        if not clean:
            continue
        for pattern in _BACKGROUND_ACTIVE_PATTERNS:
            m = pattern.search(clean)
            if m:
                return True, m.group(0)
        if any(pattern.search(clean) for pattern in _BACKGROUND_STATUS_LINE_PATTERNS):
            return False, ""
    return False, ""


def _probe_session_activity(session: str) -> dict[str, Any]:
    """Capture one visible pane snapshot and update busy-tracking state.

    `screen_busy` is true when visible content changed within the last ~2s.
    `background_active` is true when the agent status line says shell work is
    still running even if the screen is currently quiet.

    Empty `session` (e.g. legacy run with no tmux) returns idle values without
    doing work.
    """
    idle = {
        "busy": False,
        "screen_busy": False,
        "background_active": False,
        "background_active_reason": "",
        "background_active_started_ts": 0.0,
        "background_active_age_s": None,
    }
    if not session:
        return idle
    # Use a joined, fixed-size text tail rather than raw visible rows. Raw
    # viewport captures change during panel/window reshapes even when the
    # agent produced no new content, which would make activity sorting noisy.
    text = tmux_capture_activity(session)
    if not text:
        # Session vanished mid-call; clear cache so a later revival
        # doesn't look "busy forever" due to a stale hash comparison.
        _SESSION_BUSY_HASH.pop(session, None)
        _SESSION_LAST_CHANGE.pop(session, None)
        _SESSION_ACTIVITY_STREAK_START.pop(session, None)
        _SESSION_LAST_SUSTAINED_ACTIVE.pop(session, None)
        _SESSION_BACKGROUND_ACTIVE_START.pop(session, None)
        return idle
    h = hashlib.md5(text.encode("utf-8", "replace")).digest()
    prev = _SESSION_BUSY_HASH.get(session)
    now = time.time()
    if prev is None:
        _SESSION_BUSY_HASH[session] = h
    elif prev != h:
        previous_change = _SESSION_LAST_CHANGE.get(session, 0.0)
        if previous_change and (now - previous_change) <= _ACTIVITY_CONTINUITY_GAP_SECONDS:
            _SESSION_ACTIVITY_STREAK_START.setdefault(session, previous_change)
        else:
            _SESSION_ACTIVITY_STREAK_START[session] = now
        _SESSION_BUSY_HASH[session] = h
        _SESSION_LAST_CHANGE[session] = now
    else:
        last_change = _SESSION_LAST_CHANGE.get(session, 0.0)
        if not last_change or (now - last_change) > _ACTIVITY_CONTINUITY_GAP_SECONDS:
            _SESSION_ACTIVITY_STREAK_START.pop(session, None)
    last_change = _SESSION_LAST_CHANGE.get(session, 0.0)
    screen_busy = (now - last_change) < _BUSY_IDLE_SECONDS
    background_active, background_reason = _detect_background_active(text)
    if background_active:
        elapsed_s = _background_reason_elapsed_seconds(background_reason)
        if elapsed_s is not None:
            background_started_ts = now - elapsed_s
            _SESSION_BACKGROUND_ACTIVE_START[session] = background_started_ts
        else:
            background_started_ts = _SESSION_BACKGROUND_ACTIVE_START.setdefault(session, now)
    else:
        _SESSION_BACKGROUND_ACTIVE_START.pop(session, None)
        background_started_ts = 0.0
    return {
        "busy": screen_busy or background_active,
        "screen_busy": screen_busy,
        "background_active": background_active,
        "background_active_reason": background_reason,
        "background_active_started_ts": background_started_ts,
        "background_active_age_s": round(now - background_started_ts, 1) if background_started_ts else None,
    }


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AGENT_LOG_HEADING_RE = re.compile(
    r"^##\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})(?:\s+(.*))?$",
    re.MULTILINE,
)
_AGENT_LOG_FIELD_RE = re.compile(
    r"^\*\*(User asked|Did|Result|Problems / notes|Next)\*\*:\s*(.*)$",
    re.MULTILINE,
)


def _parse_local_datetime(value: str, *, end_of_date: bool = False) -> float:
    value = (value or "").strip()
    if not value:
        raise ValueError("empty date/time")
    if _DATE_ONLY_RE.fullmatch(value):
        dt = datetime.strptime(value, "%Y-%m-%d")
        ts = time.mktime(dt.timetuple())
        return ts + (24 * 60 * 60 if end_of_date else 0)
    parsed = _parse_iso_epoch(value)
    if parsed is None:
        raise ValueError(f"invalid date/time: {value}")
    return parsed


def _summary_time_range(date: str | None,
                        start: str | None,
                        end: str | None) -> tuple[float, float, str]:
    if date and (start or end):
        raise HTTPException(400, "use either date=YYYY-MM-DD or start/end, not both")
    if date:
        try:
            start_ts = _parse_local_datetime(date)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return start_ts, start_ts + 24 * 60 * 60, date
    if not start:
        today = time.strftime("%Y-%m-%d")
        start_ts = _parse_local_datetime(today)
        return start_ts, start_ts + 24 * 60 * 60, today
    try:
        start_ts = _parse_local_datetime(start)
        end_ts = _parse_local_datetime(end, end_of_date=True) if end else start_ts + 24 * 60 * 60
    except ValueError as e:
        raise HTTPException(400, str(e))
    if end_ts <= start_ts:
        raise HTTPException(400, "end must be after start")
    return start_ts, end_ts, ""


def _normalized_idle_minutes(value: int | None) -> int:
    if value is None:
        return 60
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return 60
    return max(15, min(240, minutes))


def _local_iso(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _local_hm(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%H:%M", time.localtime(ts))


def _file_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _run_dir_mtime(r: dict[str, Any]) -> float:
    raw = r.get("run_dir") or ""
    if not raw:
        return 0.0
    try:
        return Path(raw).expanduser().stat().st_mtime
    except OSError:
        return 0.0


def _session_log_path(r: dict[str, Any]) -> Path | None:
    run_dir = r.get("run_dir") or ""
    log_file = r.get("log_file") or ""
    if not run_dir or not log_file:
        return None
    return Path(run_dir) / log_file


def _session_bounds(r: dict[str, Any], now_ts: float) -> tuple[float, float]:
    run_name_start = run_name_started_at(r.get("run_name", ""))
    start_ts = (
        _parse_iso_epoch(r.get("started_at"))
        or _parse_iso_epoch(run_name_start)
        or _run_dir_mtime(r)
        or now_ts
    )
    end_candidates = [
        _parse_iso_epoch(r.get("stopped_at")),
        _parse_iso_epoch(r.get("resume_recorded_at")),
        _parse_iso_epoch(r.get("last_activity")),
        _file_mtime(_session_log_path(r)) if _session_log_path(r) else 0.0,
        _run_dir_mtime(r),
    ]
    if r.get("alive"):
        end_candidates.append(now_ts)
    end_ts = max([x for x in end_candidates if x] or [start_ts])
    if end_ts < start_ts:
        end_ts = start_ts
    # Give instant sessions a visible width in the Gantt chart.
    if end_ts == start_ts:
        end_ts = start_ts + 60
    return float(start_ts), float(end_ts)


def _overlaps(start_ts: float, end_ts: float,
              range_start: float, range_end: float) -> bool:
    return start_ts < range_end and end_ts > range_start


def _clip_range(start_ts: float, end_ts: float,
                range_start: float, range_end: float) -> tuple[float, float]:
    return max(start_ts, range_start), min(end_ts, range_end)


def _agent_log_entries_for_range(path: Path,
                                 range_start: float,
                                 range_end: float,
                                 max_entries: int = 20) -> list[dict[str, str]]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    matches = list(_AGENT_LOG_HEADING_RE.finditer(text))
    out: list[dict[str, str]] = []
    for idx, m in enumerate(matches):
        stamp = f"{m.group(1)} {m.group(2)}"
        try:
            ts = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M"))
        except (ValueError, OSError):
            continue
        if not (range_start <= ts < range_end):
            continue
        body_start = m.end()
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        if len(body) > 3000:
            body = body[:3000] + "\n\n... (truncated)"
        fields = _agent_log_entry_fields(body)
        out.append({
            "timestamp": _local_iso(ts),
            "title": (m.group(3) or "").strip(),
            "body": body,
            "user_asked": fields.get("User asked", ""),
            "did": fields.get("Did", ""),
            "result": fields.get("Result", ""),
            "problems": fields.get("Problems / notes", ""),
            "next": fields.get("Next", ""),
        })
    return out[-max_entries:]


def _agent_log_entry_fields(body: str) -> dict[str, str]:
    matches = list(_AGENT_LOG_FIELD_RE.finditer(body or ""))
    fields: dict[str, str] = {}
    for idx, m in enumerate(matches):
        key = m.group(1)
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        value = (m.group(2) + "\n" + body[start:end]).strip()
        value = re.sub(r"\n{3,}", "\n\n", value)
        fields[key] = value
    return fields


def _first_nonempty_lines(text: str, max_lines: int = 3,
                          max_chars: int = 420) -> list[str]:
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-* ").strip()
        if not line:
            continue
        out.append(line)
        if len(out) >= max_lines:
            break
    if not out and text:
        out = [text.strip()]
    cleaned = []
    for line in out:
        if len(line) > max_chars:
            line = line[:max_chars].rstrip() + "..."
        cleaned.append(line)
    return cleaned


def _summarize_agent_log_entries(entries: list[dict[str, str]]) -> dict[str, Any]:
    results: list[str] = []
    actions: list[str] = []
    problems: list[str] = []
    next_items: list[str] = []
    titles: list[str] = []
    for entry in entries:
        title = (entry.get("title") or "").strip()
        if title:
            titles.append(title)
        results.extend(_first_nonempty_lines(entry.get("result", ""), 2))
        actions.extend(_first_nonempty_lines(entry.get("did", ""), 2))
        problems.extend(_first_nonempty_lines(entry.get("problems", ""), 2))
        next_items.extend(_first_nonempty_lines(entry.get("next", ""), 2))

    def uniq(items: list[str], limit: int) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= limit:
                break
        return out

    headline = ""
    if results:
        headline = results[0]
    elif titles:
        headline = titles[-1]
    elif actions:
        headline = actions[0]
    return {
        "headline": headline,
        "titles": uniq(titles, 5),
        "results": uniq(results, 5),
        "actions": uniq(actions, 5),
        "problems": uniq(problems, 4),
        "next": uniq(next_items, 4),
    }


def _read_task_metadata(folder: Path) -> dict[str, Any]:
    meta = _safe_read_json(folder / ".orch" / "task.json")
    return meta if isinstance(meta, dict) else {}


def _linked_item_summary(rec: dict[str, str],
                         range_start: float,
                         range_end: float) -> dict[str, Any]:
    raw_path = rec.get("path") or ""
    raw_type = rec.get("type") or ""
    item: dict[str, Any] = {
        "path": raw_path,
        "label": rec.get("label") or (
            _default_linked_url_label(raw_path) if _is_linked_url(raw_path) else Path(raw_path).name
        ),
        "type": raw_type,
        "exists": False,
        "metadata": {},
        "agent_log_entries": [],
        "readme_preview": "",
        "mtime": "",
    }
    if raw_type == "url" or _is_linked_url(raw_path):
        try:
            url = _coerce_linked_url(raw_path)
        except ValueError:
            return item
        item.update({
            "path": url,
            "label": rec.get("label") or _default_linked_url_label(url),
            "type": "url",
            "exists": True,
        })
        return item
    try:
        path = Path(raw_path).expanduser().resolve()
    except OSError:
        return item
    item["path"] = str(path)
    item["exists"] = path.exists()
    item["type"] = "file" if path.is_file() else "folder"
    if not path.exists():
        return item
    item["mtime"] = _local_iso(_file_mtime(path))
    if path.is_file():
        return item
    item["metadata"] = _read_task_metadata(path)
    readme = path / "README.md"
    if readme.exists():
        item["readme_preview"] = _read_text_preview(readme, max_chars=2500)
    agent_log = path / "AGENT_LOG.md"
    if agent_log.exists():
        item["agent_log_entries"] = _agent_log_entries_for_range(
            agent_log, range_start, range_end)
    item["summary"] = _summarize_agent_log_entries(item.get("agent_log_entries") or [])
    return item


def _linked_item_for_session(item: dict[str, Any],
                             session_start: float,
                             session_end: float) -> dict[str, Any]:
    scoped = dict(item)
    entries = []
    for entry in item.get("agent_log_entries") or []:
        ts = _parse_iso_epoch(entry.get("timestamp"))
        if ts is None:
            continue
        if session_start <= ts <= session_end:
            entries.append(entry)
    scoped["agent_log_entries"] = entries
    scoped["summary"] = _summarize_agent_log_entries(entries)
    return scoped


def _session_activity_events(r: dict[str, Any],
                             linked_items: list[dict[str, Any]],
                             session_start: float,
                             session_end: float,
                             range_start: float,
                             range_end: float) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def add(ts: float | None, source: str, label: str = "") -> None:
        if ts is None:
            return
        if range_start <= ts < range_end and session_start <= ts <= session_end:
            events.append({"ts": float(ts), "source": source, "label": label})

    add(session_start, "session_start", "session started")
    add(_parse_iso_epoch(r.get("last_activity")), "last_activity", "last activity")
    add(_parse_iso_epoch(r.get("stopped_at")), "session_stop", "session stopped")
    add(_parse_iso_epoch(r.get("resume_recorded_at")), "resume_saved", "resume metadata saved")
    log_path = _session_log_path(r)
    if log_path:
        add(_file_mtime(log_path), "log_mtime", "terminal log updated")
    for item in linked_items:
        for entry in item.get("agent_log_entries") or []:
            add(_parse_iso_epoch(entry.get("timestamp")), "agent_log",
                entry.get("title") or item.get("label") or item.get("path") or "")
    events.sort(key=lambda e: e["ts"])
    deduped: list[dict[str, Any]] = []
    for event in events:
        if deduped and abs(event["ts"] - deduped[-1]["ts"]) < 60 and event["source"] == deduped[-1]["source"]:
            continue
        deduped.append(event)
    return deduped


def _activity_spans_from_events(events: list[dict[str, Any]],
                                session_start: float,
                                session_end: float,
                                range_start: float,
                                range_end: float,
                                idle_minutes: int) -> list[dict[str, Any]]:
    idle_s = _normalized_idle_minutes(idle_minutes) * 60
    pad_s = min(15 * 60, max(5 * 60, idle_s / 6))
    if not events:
        return []

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for event in events:
        if not current:
            current = [event]
            continue
        if event["ts"] - current[-1]["ts"] <= idle_s:
            current.append(event)
        else:
            groups.append(current)
            current = [event]
    if current:
        groups.append(current)

    spans = []
    for group in groups:
        raw_start = group[0]["ts"] - pad_s
        raw_end = group[-1]["ts"] + pad_s
        start_ts = max(range_start, session_start, raw_start)
        end_ts = min(range_end, max(session_end, group[-1]["ts"]), raw_end)
        if end_ts <= start_ts:
            end_ts = min(range_end, start_ts + 60)
        sources = sorted({str(e.get("source") or "") for e in group if e.get("source")})
        spans.append({
            "start": _local_iso(start_ts),
            "end": _local_iso(end_ts),
            "duration_minutes": round(max(1.0, (end_ts - start_ts) / 60.0), 1),
            "event_count": len(group),
            "sources": sources,
            "confidence": "high" if any(e.get("source") == "agent_log" for e in group) else "medium",
        })
    return spans


def _item_category(item: dict[str, Any], activity_minutes: float = 0.0) -> str:
    label = f"{item.get('label') or ''} {item.get('path') or ''}".lower()
    entries = item.get("agent_log_entries") or []
    if any(word in label for word in ("杂", "slack", "cleanup", "git status", "daily", "orchestrator")):
        return "misc"
    if len(entries) >= 2 or activity_minutes >= 30:
        return "key"
    if entries:
        return "support"
    return "unknown"


def _productivity_temperature(sessions: list[dict[str, Any]],
                              range_start: float,
                              range_end: float,
                              bucket_minutes: int = 30) -> dict[str, Any]:
    bucket_minutes = max(10, min(120, int(bucket_minutes or 30)))
    bucket_s = bucket_minutes * 60
    total_s = max(1.0, range_end - range_start)
    bucket_count = max(1, int((total_s + bucket_s - 1) // bucket_s))
    raw_buckets: list[dict[str, Any]] = [
        {
            "start_ts": range_start + idx * bucket_s,
            "end_ts": min(range_end, range_start + (idx + 1) * bucket_s),
            "active_seconds": 0.0,
            "sessions": set(),
        }
        for idx in range(bucket_count)
    ]
    intervals: list[tuple[float, float]] = []

    for session in sessions:
        session_name = str(session.get("display_name") or session.get("task") or "")
        for activity in session.get("activity_spans") or []:
            span_start = _parse_iso_epoch(activity.get("start"))
            span_end = _parse_iso_epoch(activity.get("end"))
            if span_start is None or span_end is None:
                continue
            span_start = max(range_start, float(span_start))
            span_end = min(range_end, float(span_end))
            if span_end <= span_start:
                continue
            intervals.append((span_start, span_end))
            first_idx = max(0, int((span_start - range_start) // bucket_s))
            last_idx = min(bucket_count - 1, int(max(0.0, span_end - range_start - 0.001) // bucket_s))
            for idx in range(first_idx, last_idx + 1):
                bucket = raw_buckets[idx]
                overlap = max(0.0, min(span_end, bucket["end_ts"]) - max(span_start, bucket["start_ts"]))
                if overlap <= 0:
                    continue
                bucket["active_seconds"] += overlap
                if session_name:
                    bucket["sessions"].add(session_name)

    max_active = max((float(b["active_seconds"]) for b in raw_buckets), default=0.0)
    peak_bucket = max(raw_buckets, key=lambda b: float(b["active_seconds"]), default=None)
    buckets = []
    for bucket in raw_buckets:
        active_s = float(bucket["active_seconds"])
        intensity = (active_s / max_active) if max_active > 0 else 0.0
        if active_s <= 0:
            level = "idle"
        elif intensity < 0.34:
            level = "cool"
        elif intensity < 0.67:
            level = "warm"
        elif intensity < 0.92:
            level = "hot"
        else:
            level = "peak"
        buckets.append({
            "start": _local_iso(bucket["start_ts"]),
            "end": _local_iso(bucket["end_ts"]),
            "active_minutes": round(active_s / 60.0, 1),
            "session_count": len(bucket["sessions"]),
            "intensity": round(intensity, 3),
            "level": level,
        })

    coverage_s = 0.0
    if intervals:
        intervals.sort()
        merged: list[list[float]] = []
        for span_start, span_end in intervals:
            if not merged or span_start > merged[-1][1]:
                merged.append([span_start, span_end])
            else:
                merged[-1][1] = max(merged[-1][1], span_end)
        coverage_s = sum(span_end - span_start for span_start, span_end in merged)

    peak = {}
    if peak_bucket and float(peak_bucket["active_seconds"]) > 0:
        peak = {
            "start": _local_iso(peak_bucket["start_ts"]),
            "end": _local_iso(peak_bucket["end_ts"]),
            "active_minutes": round(float(peak_bucket["active_seconds"]) / 60.0, 1),
            "session_count": len(peak_bucket["sessions"]),
        }
    return {
        "bucket_minutes": bucket_minutes,
        "coverage_minutes": round(coverage_s / 60.0, 1),
        "coverage_ratio": round(coverage_s / total_s, 3),
        "max_bucket_activity_minutes": round(max_active / 60.0, 1),
        "peak": peak,
        "buckets": buckets,
    }


def _collect_daily_summary(outputs_dir: Path,
                           range_start: float,
                           range_end: float,
                           idle_minutes: int = 60) -> dict[str, Any]:
    idle_minutes = _normalized_idle_minutes(idle_minutes)
    now_ts = time.time()
    sessions: list[dict[str, Any]] = []
    linked_by_path: dict[str, dict[str, Any]] = {}
    all_entries: list[dict[str, str]] = []
    seen_entries: set[tuple[str, str, str]] = set()
    for r in _discover_runs(outputs_dir):
        start_ts, end_ts = _session_bounds(r, now_ts)
        if not _overlaps(start_ts, end_ts, range_start, range_end):
            continue
        clipped_start, clipped_end = _clip_range(start_ts, end_ts, range_start, range_end)
        day_linked_items = [
            _linked_item_summary(rec, range_start, range_end)
            for rec in _normalize_linked_folders(r.get("linked_folders"))
        ]
        session_linked_items = [
            _linked_item_for_session(item, clipped_start, clipped_end)
            for item in day_linked_items
        ]
        for item in day_linked_items:
            path = item.get("path") or ""
            if not path:
                continue
            existing = linked_by_path.setdefault(path, item)
            refs = existing.setdefault("session_refs", [])
            refs.append({
                "run_id": r.get("run_id", ""),
                "display_name": r.get("display_name") or r.get("task") or r.get("run_name") or "",
                "agent": r.get("agent", ""),
            })
        activity_events = _session_activity_events(
            r, session_linked_items, start_ts, end_ts, range_start, range_end)
        activity_spans = _activity_spans_from_events(
            activity_events, start_ts, end_ts, range_start, range_end, idle_minutes)
        activity_minutes = sum(float(span.get("duration_minutes") or 0.0)
                               for span in activity_spans)
        lifecycle_minutes = max(0.0, (clipped_end - clipped_start) / 60.0)
        for item in session_linked_items:
            item["category"] = _item_category(item, activity_minutes)
        for item in day_linked_items:
            for entry in item.get("agent_log_entries") or []:
                entry_key = (
                    item.get("path") or "",
                    entry.get("timestamp") or "",
                    entry.get("title") or "",
                )
                if entry_key in seen_entries:
                    continue
                seen_entries.add(entry_key)
                all_entries.append(entry)
        session_summary = _summarize_agent_log_entries([
            entry
            for item in session_linked_items
            for entry in (item.get("agent_log_entries") or [])
        ])
        sessions.append({
            "run_id": r.get("run_id", ""),
            "run_name": r.get("run_name", ""),
            "task": r.get("task", ""),
            "display_name": r.get("display_name") or r.get("task") or r.get("run_name") or "",
            "agent": r.get("agent", ""),
            "kind": r.get("kind", ""),
            "status": r.get("status", ""),
            "alive": bool(r.get("alive")),
            "cwd": r.get("cwd", ""),
            "started_at": _local_iso(start_ts),
            "ended_at": _local_iso(end_ts),
            "visible_start": _local_iso(clipped_start),
            "visible_end": _local_iso(clipped_end),
            "duration_minutes": round(lifecycle_minutes, 1),
            "lifecycle_minutes": round(lifecycle_minutes, 1),
            "activity_minutes": round(activity_minutes, 1),
            "activity_event_count": len(activity_events),
            "activity_spans": activity_spans,
            "summary": session_summary,
            "linked_items": session_linked_items,
            "log_path": str(_session_log_path(r) or ""),
        })
    sessions.sort(key=lambda x: x.get("visible_start") or "")
    agent_totals: dict[str, float] = {}
    agent_lifecycle_totals: dict[str, float] = {}
    for s in sessions:
        agent = s.get("agent") or "unknown"
        agent_totals[agent] = agent_totals.get(agent, 0.0) + float(s.get("activity_minutes") or 0.0)
        agent_lifecycle_totals[agent] = (
            agent_lifecycle_totals.get(agent, 0.0) + float(s.get("lifecycle_minutes") or 0.0)
        )
    for item in linked_by_path.values():
        refs = item.get("session_refs") or []
        ref_activity = sum(
            float(s.get("activity_minutes") or 0.0)
            for s in sessions
            if any(ref.get("run_id") == s.get("run_id") for ref in refs)
        )
        item["category"] = item.get("category") or _item_category(item, ref_activity)
    category_counts: dict[str, int] = {}
    for item in linked_by_path.values():
        category = item.get("category") or "unknown"
        category_counts[category] = category_counts.get(category, 0) + 1
    review = _summarize_agent_log_entries(all_entries)
    review["category_counts"] = category_counts
    temperature = _productivity_temperature(sessions, range_start, range_end)
    return {
        "range": {
            "start": _local_iso(range_start),
            "end": _local_iso(range_end),
            "timezone": time.tzname[time.localtime().tm_isdst > 0],
            "idle_threshold_minutes": idle_minutes,
        },
        "session_count": len(sessions),
        "activity_minutes": round(sum(float(s.get("activity_minutes") or 0.0) for s in sessions), 1),
        "lifecycle_minutes": round(sum(float(s.get("lifecycle_minutes") or 0.0) for s in sessions), 1),
        "activity_span_count": sum(len(s.get("activity_spans") or []) for s in sessions),
        "agent_log_entry_count": len(all_entries),
        "agent_totals_minutes": {k: round(v, 1) for k, v in sorted(agent_totals.items())},
        "agent_lifecycle_totals_minutes": {
            k: round(v, 1) for k, v in sorted(agent_lifecycle_totals.items())
        },
        "productivity_temperature": temperature,
        "review": review,
        "sessions": sessions,
        "linked_items": sorted(linked_by_path.values(), key=lambda x: x.get("path") or ""),
    }


def _daily_summary_html(summary: dict[str, Any]) -> str:
    rng = summary.get("range", {})
    sessions = summary.get("sessions", [])
    linked_items = summary.get("linked_items", [])
    start_ts = _parse_iso_epoch(rng.get("start")) or time.time()
    end_ts = _parse_iso_epoch(rng.get("end")) or start_ts + 1
    span = max(1.0, end_ts - start_ts)
    hours = max(1.0, span / 3600.0)
    timeline_width = max(1100, int(hours * 92))

    def esc(value: Any) -> str:
        return html_lib.escape(str(value or ""))

    def fmt_minutes(value: Any) -> str:
        minutes = float(value or 0.0)
        if minutes >= 60:
            return f"{minutes / 60.0:.1f} h"
        return f"{minutes:.0f} min"

    def bullet_list(items: list[str], empty: str) -> str:
        if not items:
            return f"<p class='muted'>{esc(empty)}</p>"
        return "<ul>" + "".join(f"<li>{esc(item)}</li>" for item in items) + "</ul>"

    def category_label(category: str) -> str:
        return {
            "key": "Key work",
            "support": "Support",
            "misc": "Misc",
            "unknown": "Unclassified",
        }.get(category or "unknown", "Unclassified")

    ticks = []
    tick_step_s = 3600
    first_tick = int(start_ts // tick_step_s) * tick_step_s
    if first_tick < start_ts:
        first_tick += tick_step_s
    tick = first_tick
    while tick <= end_ts:
        left = max(0.0, min(100.0, ((tick - start_ts) / span) * 100.0))
        ticks.append(
            f"<div class='tick' style='left:{left:.2f}%'><span>{esc(_local_hm(tick))}</span></div>"
        )
        tick += tick_step_s
    day_start = time.strftime("%b %d %H:%M", time.localtime(start_ts))
    day_end = time.strftime("%b %d %H:%M", time.localtime(end_ts))
    temperature = summary.get("productivity_temperature") or {}
    temp_segments = []
    for bucket in temperature.get("buckets") or []:
        bucket_start = _parse_iso_epoch(bucket.get("start")) or start_ts
        bucket_end = _parse_iso_epoch(bucket.get("end")) or bucket_start
        left = max(0.0, min(100.0, ((bucket_start - start_ts) / span) * 100.0))
        width = max(0.2, min(100.0 - left, ((bucket_end - bucket_start) / span) * 100.0))
        intensity = max(0.0, min(1.0, float(bucket.get("intensity") or 0.0)))
        level = esc(bucket.get("level") or "idle")
        label = (
            f"{bucket.get('start', '')} -> {bucket.get('end', '')}"
            f" | {bucket.get('active_minutes', 0)} agent-min"
            f" | sessions {bucket.get('session_count', 0)}"
        )
        temp_segments.append(
            f"<div class='temp-segment {level}' style='left:{left:.2f}%;width:{width:.2f}%;--heat:{intensity:.3f}' "
            f"title='{esc(label)}'></div>"
        )
    peak = temperature.get("peak") or {}
    peak_label = "no peak"
    peak_marker = ""
    if peak:
        peak_start_ts = _parse_iso_epoch(peak.get("start")) or start_ts
        peak_end_ts = _parse_iso_epoch(peak.get("end")) or peak_start_ts
        peak_left = max(0.0, min(100.0, ((peak_start_ts - start_ts) / span) * 100.0))
        peak_width = max(0.2, min(100.0 - peak_left, ((peak_end_ts - peak_start_ts) / span) * 100.0))
        peak_label = (
            f"peak {esc(_local_hm(_parse_iso_epoch(peak.get('start'))))}-"
            f"{esc(_local_hm(_parse_iso_epoch(peak.get('end'))))}"
            f" · {esc(peak.get('active_minutes'))} agent-min"
        )
        peak_marker = (
            f"<div class='temp-peak-marker' style='left:{peak_left:.2f}%;width:{peak_width:.2f}%' "
            f"title='{peak_label}'></div>"
        )
    temp_meta = (
        f"coverage {fmt_minutes(temperature.get('coverage_minutes'))}"
        f" · {peak_label}"
    )

    rows = []
    for s in sessions:
        title = esc(s.get("display_name") or s.get("task"))
        agent = esc(s.get("agent") or "unknown")
        bars = []
        for activity in s.get("activity_spans") or []:
            vs = _parse_iso_epoch(activity.get("start")) or start_ts
            ve = _parse_iso_epoch(activity.get("end")) or vs
            left = max(0.0, min(100.0, ((vs - start_ts) / span) * 100.0))
            width = max(0.45, min(100.0 - left, ((ve - vs) / span) * 100.0))
            confidence = esc(activity.get("confidence") or "medium")
            label = (
                f"{activity.get('start', '')} -> {activity.get('end', '')}"
                f" | {activity.get('duration_minutes', 0)} min"
                f" | {activity.get('event_count', 0)} events"
            )
            bars.append(
                f"<div class='gantt-bar {confidence}' style='left:{left:.2f}%;width:{width:.2f}%' "
                f"title='{esc(label)}'></div>"
            )
        rows.append(f"""
          <div class="gantt-row">
            <div class="gantt-label">
              <strong>{title}</strong>
              <span>{agent} · {esc(s.get('status'))} · active {esc(fmt_minutes(s.get('activity_minutes')))} · {esc(s.get('activity_event_count'))} events</span>
            </div>
            <div class="gantt-track">{''.join(bars) or "<span class='no-activity'>no activity evidence</span>"}</div>
          </div>
        """)

    cards = []
    for s in sessions:
        linked = s.get("linked_items") or []
        session_summary = s.get("summary") or {}
        linked_html = []
        for item in linked:
            item_summary = item.get("summary") or {}
            category = item.get("category") or "unknown"
            headline = item_summary.get("headline") or item.get("readme_preview") or "No summary yet."
            linked_html.append(f"""
              <div class="linked">
                <div class="linked-head">
                  <span class="badge {esc(category)}">{esc(category_label(category))}</span>
                  <strong>{esc(item.get('label') or Path(str(item.get('path') or '')).name)}</strong>
                </div>
                <div class="linked-path">{esc(item.get('path'))}</div>
                <p>{esc(headline)}</p>
              </div>
            """)
        cards.append(f"""
          <article class="session-card">
            <h3>{esc(s.get('display_name'))}</h3>
            <p class="muted">{esc(s.get('agent'))} · active {esc(fmt_minutes(s.get('activity_minutes')))} · lifecycle {esc(fmt_minutes(s.get('lifecycle_minutes')))}</p>
            <div class="mini-review">
              {bullet_list(session_summary.get('results') or session_summary.get('actions') or [], "No structured result recorded for this session.")}
            </div>
            {''.join(linked_html) or "<p class='muted'>No linked folders/files.</p>"}
          </article>
        """)

    totals = "".join(
        f"<span class='pill'>{esc(agent)}: {minutes} min</span>"
        for agent, minutes in (summary.get("agent_totals_minutes") or {}).items()
    )
    review = summary.get("review") or {}
    categories = review.get("category_counts") or {}
    category_pills = "".join(
        f"<span class='pill'>{esc(category_label(category))}: {esc(count)}</span>"
        for category, count in sorted(categories.items())
    )
    kpis = [
        ("Sessions", summary.get("session_count")),
        ("Linked items", len(linked_items)),
        ("Log entries", summary.get("agent_log_entry_count")),
        ("Active time", fmt_minutes(summary.get("activity_minutes"))),
        ("Activity spans", summary.get("activity_span_count")),
    ]
    kpi_html = "".join(
        f"<div class='kpi'><span>{esc(label)}</span><strong>{esc(value)}</strong></div>"
        for label, value in kpis
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Agent Daily Review</title>
  <style>
    body {{ margin: 0; padding: 24px; background: #0d1117; color: #e6edf3; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    h1, h2, h3, h4 {{ margin: 0; }}
    h1 {{ font-size: 24px; margin-bottom: 4px; }}
    h2 {{ font-size: 17px; margin: 28px 0 12px; }}
    h3 {{ font-size: 15px; }}
    ul {{ margin: 8px 0 0; padding-left: 18px; }}
    li {{ margin: 4px 0; }}
    .muted {{ color: #8b949e; }}
    .top {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }}
    .pills {{ display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }}
    .pill {{ border: 1px solid #30363d; background: #161b22; border-radius: 999px; padding: 4px 9px; color: #c9d1d9; }}
    .kpis {{ display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); gap: 10px; margin-top: 18px; }}
    .kpi {{ border: 1px solid #30363d; border-radius: 8px; background: #161b22; padding: 12px; }}
    .kpi span {{ display: block; color: #8b949e; font-size: 12px; }}
    .kpi strong {{ display: block; margin-top: 4px; font-size: 20px; }}
    .review-grid {{ display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: 14px; }}
    .review-card {{ border: 1px solid #30363d; border-radius: 8px; background: #161b22; padding: 14px; }}
    .timeline-scroll {{ border: 1px solid #30363d; border-radius: 8px; overflow-x: auto; background: #010409; }}
    .timeline-inner {{ width: {timeline_width}px; min-width: 100%; }}
    .ruler {{ display: grid; grid-template-columns: 280px 1fr; height: 38px; border-bottom: 1px solid #30363d; background: #111820; }}
    .ruler-label {{ padding: 9px 10px; color: #8b949e; font-size: 12px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }}
    .ruler-track {{ position: relative; min-width: 0; }}
    .tick {{ position: absolute; top: 0; bottom: 0; width: 1px; background: #30363d; }}
    .tick span {{ position: absolute; top: 9px; left: 6px; color: #8b949e; font-size: 11px; white-space: nowrap; }}
    .temperature-row {{ display: grid; grid-template-columns: 280px 1fr; min-height: 50px; border-bottom: 1px solid #30363d; background: #0d1117; }}
    .temperature-label {{ padding: 8px 10px; background: #161b22; overflow: hidden; }}
    .temperature-label strong, .temperature-label span {{ display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .temperature-label span {{ color: #8b949e; font-size: 12px; }}
    .temperature-panel {{ position: relative; margin: 9px 12px 8px; }}
    .temperature-track {{ position: relative; height: 18px; border: 1px solid #30363d; border-radius: 999px; overflow: hidden; background: linear-gradient(180deg, #20262f, #111820); box-shadow: inset 0 1px 0 rgba(255,255,255,.05); }}
    .temperature-legend {{ display: flex; justify-content: space-between; margin-top: 4px; color: #8b949e; font-size: 10px; letter-spacing: .02em; text-transform: uppercase; }}
    .temperature-legend span {{ display: inline-flex; align-items: center; gap: 5px; }}
    .temperature-legend span::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; opacity: .85; }}
    .temperature-legend .cool {{ color: #3fb950; }}
    .temperature-legend .warm {{ color: #d29922; }}
    .temperature-legend .peak {{ color: #f85149; }}
    .temp-segment {{ position: absolute; top: 0; bottom: 0; border-right: 1px solid rgba(1, 4, 9, .35); opacity: calc(.24 + var(--heat) * .76); }}
    .temp-segment.idle {{ background: #161b22; opacity: .45; }}
    .temp-segment.cool {{ background: #238636; }}
    .temp-segment.warm {{ background: #d29922; }}
    .temp-segment.hot {{ background: #fb8500; }}
    .temp-segment.peak {{ background: #f85149; }}
    .temp-peak-marker {{ position: absolute; top: -4px; bottom: -4px; border: 1px solid rgba(255,255,255,.75); border-radius: 999px; box-shadow: 0 0 0 1px rgba(248,81,73,.45), 0 0 14px rgba(248,81,73,.55); pointer-events: none; }}
    .gantt {{ background: #010409; }}
    .gantt-row {{ display: grid; grid-template-columns: 280px 1fr; border-bottom: 1px solid #21262d; min-height: 50px; }}
    .gantt-row:last-child {{ border-bottom: 0; }}
    .gantt-label {{ padding: 8px 10px; background: #161b22; overflow: hidden; }}
    .gantt-label strong, .gantt-label span {{ display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .gantt-label span {{ color: #8b949e; font-size: 12px; }}
    .gantt-track {{ position: relative; margin: 12px; background: #161b22; border: 1px solid #30363d; border-radius: 999px; overflow: hidden; }}
    .gantt-bar {{ position: absolute; top: 3px; bottom: 3px; border-radius: 999px; min-width: 5px; }}
    .gantt-bar.high {{ background: linear-gradient(90deg, #58a6ff, #3fb950); }}
    .gantt-bar.medium {{ background: linear-gradient(90deg, #d2a8ff, #58a6ff); }}
    .gantt-bar.low {{ background: #6e7681; }}
    .no-activity {{ color: #6e7681; font-size: 12px; position: absolute; left: 10px; top: 5px; }}
    .session-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 14px; }}
    .session-card {{ border: 1px solid #30363d; border-radius: 8px; background: #161b22; padding: 14px; min-width: 0; }}
    .session-card h3 {{ font-size: 15px; margin-bottom: 4px; }}
    .mini-review {{ color: #c9d1d9; }}
    .linked {{ margin-top: 12px; border-top: 1px solid #30363d; padding-top: 10px; }}
    .linked-head {{ display: flex; align-items: center; gap: 8px; min-width: 0; }}
    .linked-head strong {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .linked-path {{ color: #58a6ff; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; overflow-wrap: anywhere; }}
    .linked p {{ margin: 6px 0 0; color: #c9d1d9; }}
    .badge {{ border-radius: 999px; padding: 2px 7px; font-size: 11px; border: 1px solid #30363d; color: #c9d1d9; background: #0d1117; white-space: nowrap; }}
    .badge.key {{ color: #3fb950; border-color: rgba(63,185,80,.45); }}
    .badge.support {{ color: #58a6ff; border-color: rgba(88,166,255,.45); }}
    .badge.misc {{ color: #d29922; border-color: rgba(210,153,34,.45); }}
    @media (max-width: 900px) {{
      .top, .review-grid {{ display: block; }}
      .kpis {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
      .ruler, .temperature-row, .gantt-row {{ grid-template-columns: 220px 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="top">
    <div>
      <h1>Agent Daily Review</h1>
      <div class="muted">{esc(rng.get('start'))} -> {esc(rng.get('end'))} · {esc(rng.get('timezone'))} · idle threshold {esc(rng.get('idle_threshold_minutes'))} min</div>
    </div>
    <div class="pills">{totals}</div>
  </div>
  <div class="kpis">{kpi_html}</div>
  <h2>Daily Review</h2>
  <div class="review-grid">
    <section class="review-card">
      <h3>Highlights</h3>
      {bullet_list(review.get('results') or review.get('titles') or [], "No structured highlights found in AGENT_LOG.")}
    </section>
    <section class="review-card">
      <h3>Work Done</h3>
      {bullet_list(review.get('actions') or [], "No structured action list found.")}
    </section>
    <section class="review-card">
      <h3>Risks / Notes</h3>
      {bullet_list(review.get('problems') or [], "No notable problems recorded.")}
    </section>
    <section class="review-card">
      <h3>Follow-ups</h3>
      {bullet_list(review.get('next') or [], "No explicit follow-ups recorded.")}
      <div class="pills" style="justify-content:flex-start;margin-top:12px">{category_pills}</div>
    </section>
  </div>
  <h2>Timeline</h2>
  <div class="timeline-scroll">
    <div class="timeline-inner">
      <div class="ruler">
        <div class="ruler-label">{esc(day_start)} -> {esc(day_end)}</div>
        <div class="ruler-track">{''.join(ticks)}</div>
      </div>
      <div class="temperature-row">
        <div class="temperature-label">
          <strong>Productivity Heatline</strong>
          <span>{temp_meta}</span>
        </div>
        <div class="temperature-panel">
          <div class="temperature-track">
            {''.join(temp_segments) or "<span class='no-activity'>no activity evidence</span>"}
            {peak_marker}
          </div>
          <div class="temperature-legend">
            <span class="cool">cool</span>
            <span class="warm">warm</span>
            <span class="peak">peak</span>
          </div>
        </div>
      </div>
      <div class="gantt">{''.join(rows) or "<p class='muted' style='padding:16px'>No sessions in this range.</p>"}</div>
    </div>
  </div>
  <h2>Sessions and Subtasks Review</h2>
  <div class="session-grid">{''.join(cards) or "<p class='muted'>No session details.</p>"}</div>
  <h2>Linked Items</h2>
  <p class="muted">{len(linked_items)} unique linked files/folders. This report summarizes structured AGENT_LOG fields instead of dumping raw logs.</p>
</body>
</html>"""


def _discover_runs(outputs_dir: Path) -> list[dict[str, Any]]:
    if not outputs_dir.exists():
        return []
    runs: list[dict[str, Any]] = []
    live_sessions = tmux_list_sessions()
    # O(1) membership test replaces the ~46 `tmux has-session` subprocess
    # calls that used to dominate _discover_runs (~1s per call). The live
    # set is authoritative because `tmux_list_sessions()` already asked
    # the server for the full list on the same call.
    live_set = set(live_sessions)
    matched_sessions: set[str] = set()
    orphan_labels = _load_orphan_labels(outputs_dir)
    run_dirs = sorted(
        (d for d in outputs_dir.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime if d.exists() else 0,
        reverse=True,
    )
    metadata_paths: list[Path] = []
    for run_dir in run_dirs:
        state_file = run_dir / "state.json"
        sess_file = run_dir / "session.json"
        if state_file.exists():
            metadata_paths.append(state_file)
        elif sess_file.exists():
            metadata_paths.append(sess_file)
    _prime_json_cache(metadata_paths)

    for run_dir in run_dirs:
        if not run_dir.is_dir():
            continue
        run_id = run_dir.name

        # Full mode: outputs/<name>-<ts>/state.json  (one entry per task)
        state_file = run_dir / "state.json"
        if state_file.exists():
            data = _safe_read_json(state_file) or {}
            for task_name, st in data.items():
                session = st.get("tmux_session", "")
                alive = bool(session) and session in live_set
                if alive:
                    matched_sessions.add(session)
                label = (st.get("label") or "").strip()
                cwd = st.get("cwd", "") or ""
                agent = st.get("agent", "") or ""
                started_at = st.get("started_at", "") or ""
                auto_title = _auto_title_for(agent, cwd, started_at, label, alive)
                row = {
                    "run_id": f"{run_id}::{task_name}",
                    "run_dir": str(run_dir),
                    "run_name": run_id,
                    "task": task_name,
                    "kind": "task",
                    "tmux_session": session,
                    "alive": alive,
                    "status": st.get("status", "unknown"),
                    "mode": st.get("mode", "manual"),
                    "round": st.get("round", 0),
                    "max_rounds": st.get("max_rounds"),
                    "started_at": started_at,
                    "last_activity": st.get("last_activity", ""),
                    "log_file": st.get("log_file") or f"logs/{task_name}.log",
                    "label": label,
                    "auto_title": auto_title,
                    "display_name": label or auto_title or task_name,
                    "agent": agent,
                    "model": st.get("model", "") or "",
                    "effort": st.get("effort", "") or "",
                    "cwd": cwd,
                    "panel_state": st.get("panel_state", ""),
                    "terminal_theme": _normalize_terminal_theme(st.get("terminal_theme", "")),
                    "linked_folders": _normalize_linked_folders(st.get("linked_folders")),
                }
                _add_resume_fields(row, st)
                runs.append(row)
            continue

        # Light mode: outputs/<name>-<ts>/session.json
        sess_file = run_dir / "session.json"
        if sess_file.exists():
            data = _safe_read_json(sess_file) or {}
            session = data.get("tmux_session", "")
            task_name = data.get("name", run_id)
            alive = bool(session) and session in live_set
            if alive:
                matched_sessions.add(session)
            label = (data.get("label") or "").strip()
            cwd = data.get("cwd", "") or ""
            agent = data.get("agent", "") or ""
            started_at = data.get("started_at", "") or ""
            auto_title = _auto_title_for(agent, cwd, started_at, label, alive)
            saved_status = data.get("status", "")
            row = {
                "run_id": f"{run_id}::{task_name}",
                "run_dir": str(run_dir),
                "run_name": run_id,
                "task": task_name,
                "kind": "run",
                "agent": agent,
                "model": data.get("model", ""),
                "effort": data.get("effort", ""),
                "cwd": cwd,
                "tmux_session": session,
                "alive": alive,
                "status": "running" if alive else (saved_status or "finished"),
                "mode": "manual",
                "started_at": started_at,
                "log_file": data.get("log_file") or f"logs/{task_name}.log",
                "label": label,
                "auto_title": auto_title,
                "display_name": label or auto_title or task_name,
                "panel_state": data.get("panel_state", ""),
                "terminal_theme": _normalize_terminal_theme(data.get("terminal_theme", "")),
                "linked_folders": _normalize_linked_folders(data.get("linked_folders")),
            }
            _add_resume_fields(row, data)
            runs.append(row)
            continue

        # Legacy run dir (no session.json / state.json): try to pair with a
        # live tmux session by name prefix `orch-<task>-*`.
        log_dir = run_dir / "logs"
        if log_dir.exists():
            logs = sorted(log_dir.glob("*.log"))
            if logs:
                task = logs[0].stem
                orphan_session = _match_orphan_session(run_id, live_sessions)
                if orphan_session and orphan_session not in matched_sessions:
                    matched_sessions.add(orphan_session)
                    legacy_cwd = tmux_get_cwd(orphan_session)
                    legacy_label = orphan_labels.get(orphan_session, "")
                    # Prefer the tmux session's own creation time; fall
                    # back to the run dir name's timestamp suffix. Either
                    # is fine as long as it's *per-session* distinct so
                    # the title cache + transcript picker can tell this
                    # entry apart from siblings sharing the same cwd.
                    legacy_started = (
                        tmux_session_started_at(orphan_session)
                        or run_name_started_at(run_id)
                    )
                    legacy_auto = _auto_title_for(
                        "", legacy_cwd, legacy_started, legacy_label, True
                    )
                    runs.append({
                        "run_id": f"{run_id}::{task}",
                        "run_dir": str(run_dir),
                        "run_name": run_id,
                        "task": task,
                        "kind": "run",
                        "tmux_session": orphan_session,
                        "alive": True,
                        "status": "running",
                        "mode": "manual",
                        "log_file": f"logs/{logs[0].name}",
                        "cwd": legacy_cwd,
                        "started_at": legacy_started,
                        "label": legacy_label,
                        "auto_title": legacy_auto,
                        "display_name": legacy_label or legacy_auto or task,
                    })
                else:
                    runs.append({
                        "run_id": f"{run_id}::{task}",
                        "run_dir": str(run_dir),
                        "run_name": run_id,
                        "task": task,
                        "kind": "legacy",
                        "tmux_session": "",
                        "alive": False,
                        "status": "archived",
                        "mode": "manual",
                        "log_file": f"logs/{logs[0].name}",
                        "label": "",
                        "auto_title": None,
                        "display_name": task,
                    })

    # Also surface any `orch-*` tmux sessions that don't map to any run dir.
    # These are live agents (e.g. started manually) that the user may still
    # want to control from the dashboard.
    for sess in live_sessions:
        if sess in matched_sessions or not sess.startswith("orch-"):
            continue
        # Skip internal "shadow" sessions used by the ttyd proxy — they
        # share windows with a real session and shouldn't appear on their own.
        if sess.endswith("-web"):
            continue
        task = sess.replace("orch-", "", 1)
        cwd = tmux_get_cwd(sess)
        label = orphan_labels.get(sess, "")
        # tmux's own session_created gives us a unique started_at per
        # session. Critical for title attribution: without this, every
        # orphan-tmux entry with the same cwd used to collapse onto a
        # single cached title.
        started = tmux_session_started_at(sess)
        auto = _auto_title_for("", cwd, started, label, True)
        runs.append({
            "run_id": f"tmux::{sess}",
            "run_dir": "",
            "run_name": sess,
            "task": task,
            "kind": "orphan",
            "tmux_session": sess,
            "alive": True,
            "status": "running",
            "mode": "manual",
            "log_file": "",
            "cwd": cwd,
            "started_at": started,
            "label": label,
            "auto_title": auto,
            "display_name": label or auto or task,
        })

    # Tag every alive run with `busy` based on recent pane-content changes
    # OR an agent status line that says background shell work is still running.
    # This is what lets sidebar dots go green even for sessions that
    # aren't currently attached to an open pane (and therefore have no
    # front-end pane available to mirror activity from).
    # Dead sessions are always `busy=False`.
    for r in runs:
        if r.get("alive"):
            sess = r.get("tmux_session", "")
            activity = _probe_session_activity(sess)
            r["busy"] = bool(activity.get("busy"))
            r["screen_busy"] = bool(activity.get("screen_busy"))
            r["background_active"] = bool(activity.get("background_active"))
            r["background_active_reason"] = activity.get("background_active_reason", "")
            bg_started_ts = float(activity.get("background_active_started_ts") or 0.0)
            r["background_active_started_at"] = _local_iso(bg_started_ts)
            r["background_active_age_s"] = activity.get("background_active_age_s")
            now = time.time()
            changed_ts = _SESSION_LAST_CHANGE.get(sess, 0.0)
            streak_ts = _SESSION_ACTIVITY_STREAK_START.get(sess, 0.0)
            streak_age = (now - streak_ts) if streak_ts else 0.0
            recently_changed = (
                bool(changed_ts)
                and (now - changed_ts) <= _ACTIVITY_CONTINUITY_GAP_SECONDS
            )
            sustained_active = (
                bool(streak_ts)
                and recently_changed
                and streak_age >= _PANEL_SORT_ACTIVE_SECONDS
            )
            if sustained_active:
                _SESSION_LAST_SUSTAINED_ACTIVE[sess] = changed_ts or now
            sustained_ts = _SESSION_LAST_SUSTAINED_ACTIVE.get(sess, 0.0)
            r["activity_last_change_at"] = _local_iso(changed_ts)
            r["activity_last_change_age_s"] = (
                round(now - changed_ts, 1) if changed_ts else None
            )
            r["activity_streak_started_at"] = _local_iso(streak_ts)
            r["activity_streak_age_s"] = (
                round(streak_age, 1) if streak_ts and recently_changed else None
            )
            r["activity_sustained_active"] = sustained_active
            r["activity_last_sustained_at"] = _local_iso(sustained_ts)
            r["activity_last_sustained_age_s"] = (
                round(now - sustained_ts, 1) if sustained_ts else None
            )
            r["activity_sustained_threshold_s"] = _PANEL_SORT_ACTIVE_SECONDS
        else:
            r["busy"] = False
            r["screen_busy"] = False
            r["background_active"] = False
            r["background_active_reason"] = ""
            r["background_active_started_at"] = ""
            r["background_active_age_s"] = None
            r["activity_last_change_at"] = ""
            r["activity_last_change_age_s"] = None
            r["activity_streak_started_at"] = ""
            r["activity_streak_age_s"] = None
            r["activity_sustained_active"] = False
            r["activity_last_sustained_at"] = ""
            r["activity_last_sustained_age_s"] = None
            r["activity_sustained_threshold_s"] = _PANEL_SORT_ACTIVE_SECONDS

    # Garbage-collect cache entries for sessions that are no longer alive,
    # so the dicts don't grow without bound across long-running dashboard
    # processes.
    live = {r.get("tmux_session", "") for r in runs if r.get("alive")}
    for dead_sess in list(_SESSION_BUSY_HASH.keys()):
        if dead_sess not in live:
            _SESSION_BUSY_HASH.pop(dead_sess, None)
            _SESSION_LAST_CHANGE.pop(dead_sess, None)
            _SESSION_ACTIVITY_STREAK_START.pop(dead_sess, None)
            _SESSION_LAST_SUSTAINED_ACTIVE.pop(dead_sess, None)

    return runs


def _lookup_run(outputs_dir: Path, run_id: str) -> Optional[dict[str, Any]]:
    for r in _discover_runs(outputs_dir):
        if r["run_id"] == run_id:
            return r
    return None


def _lookup_run_light(outputs_dir: Path, run_id: str) -> Optional[dict[str, Any]]:
    """Read one run's metadata without polling tmux/busy state.

    Folder/file APIs only need persisted metadata and linked_folders. Calling
    _discover_runs() here is unnecessarily expensive because it scans every
    session and probes all live panes for busy state.
    """
    run_name, sep, task_name = run_id.partition("::")
    if sep == "::" and run_name == "tmux" and task_name:
        if Path(task_name).name != task_name:
            return None
        alive = tmux_alive(task_name)
        return {
            "run_id": run_id,
            "run_dir": "",
            "run_name": task_name,
            "task": task_name.replace("orch-", "", 1),
            "kind": "orphan",
            "tmux_session": task_name,
            "alive": alive,
            "status": "running" if alive else "finished",
            "mode": "manual",
            "log_file": "",
            "label": "",
            "auto_title": None,
            "display_name": task_name.replace("orch-", "", 1),
            "linked_folders": [],
            "busy": False,
        }
    if sep != "::" or not run_name or run_name == "tmux":
        return _lookup_run(outputs_dir, run_id)
    if Path(run_name).name != run_name:
        return None

    try:
        root = outputs_dir.resolve()
        run_dir = (outputs_dir / run_name).resolve()
    except OSError:
        return None
    if not _path_is_within(run_dir, root) or not run_dir.is_dir():
        return None

    state_file = run_dir / "state.json"
    if state_file.exists():
        data = _safe_read_json(state_file) or {}
        st = data.get(task_name)
        if isinstance(st, dict):
            label = (st.get("label") or "").strip()
            row = {
                "run_id": run_id,
                "run_dir": str(run_dir),
                "run_name": run_name,
                "task": task_name,
                "kind": "task",
                "tmux_session": st.get("tmux_session", ""),
                "alive": False,
                "status": st.get("status", "unknown"),
                "mode": st.get("mode", "manual"),
                "round": st.get("round", 0),
                "max_rounds": st.get("max_rounds"),
                "started_at": st.get("started_at", ""),
                "last_activity": st.get("last_activity", ""),
                "log_file": st.get("log_file") or f"logs/{task_name}.log",
                "label": label,
                "auto_title": None,
                "display_name": label or task_name,
                "agent": st.get("agent", "") or "",
                "model": st.get("model", "") or "",
                "effort": st.get("effort", "") or "",
                "cwd": st.get("cwd", "") or "",
                "panel_state": st.get("panel_state", ""),
                "terminal_theme": _normalize_terminal_theme(st.get("terminal_theme", "")),
                "linked_folders": _normalize_linked_folders(st.get("linked_folders")),
                "busy": False,
            }
            _add_resume_fields(row, st)
            return row

    sess_file = run_dir / "session.json"
    if sess_file.exists():
        data = _safe_read_json(sess_file) or {}
        task = data.get("name", run_name)
        if task_name == task:
            label = (data.get("label") or "").strip()
            row = {
                "run_id": run_id,
                "run_dir": str(run_dir),
                "run_name": run_name,
                "task": task_name,
                "kind": "run",
                "agent": data.get("agent", "") or "",
                "model": data.get("model", "") or "",
                "effort": data.get("effort", "") or "",
                "cwd": data.get("cwd", "") or "",
                "tmux_session": data.get("tmux_session", "") or "",
                "alive": False,
                "status": data.get("status", "") or "finished",
                "mode": "manual",
                "started_at": data.get("started_at", ""),
                "log_file": data.get("log_file") or f"logs/{task_name}.log",
                "label": label,
                "auto_title": None,
                "display_name": label or task_name,
                "panel_state": data.get("panel_state", ""),
                "terminal_theme": _normalize_terminal_theme(data.get("terminal_theme", "")),
                "linked_folders": _normalize_linked_folders(data.get("linked_folders")),
                "busy": False,
            }
            _add_resume_fields(row, data)
            return row

    log_dir = run_dir / "logs"
    if log_dir.exists():
        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "run_name": run_name,
            "task": task_name,
            "kind": "legacy",
            "tmux_session": "",
            "alive": False,
            "status": "archived",
            "mode": "manual",
            "log_file": f"logs/{task_name}.log",
            "label": "",
            "auto_title": None,
            "display_name": task_name,
            "linked_folders": [],
            "busy": False,
        }

    return _lookup_run(outputs_dir, run_id)


def _capture_native_resume_for_run(
    outputs_dir: Path,
    run_id: str,
    *,
    preallocated_meta: Optional[dict[str, str]] = None,
    preallocation_error: str = "",
    attempts: tuple[float, ...] = (0.7, 1.5, 3.0, 6.0, 10.0),
) -> None:
    last_error = preallocation_error
    if preallocation_error:
        print(f"[resume-capture] preallocation failed for {run_id}: "
              f"{preallocation_error}", file=sys.stderr, flush=True)

    for delay in attempts:
        time.sleep(delay)
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            last_error = "run metadata not found yet"
            continue
        if r.get("resume_id"):
            if (
                preallocated_meta
                and preallocated_meta.get("resume_id") == r.get("resume_id")
            ):
                _persist_resume_metadata(r, preallocated_meta)
            _persist_resume_capture_status(r, "captured")
            return

        if preallocated_meta and preallocated_meta.get("resume_id"):
            if _persist_resume_metadata(r, preallocated_meta):
                _persist_resume_capture_status(r, "captured")
                print(f"[resume-capture] persisted preallocated "
                      f"{preallocated_meta.get('resume_agent')} id for {run_id}",
                      file=sys.stderr, flush=True)
                return
            last_error = "failed to persist preallocated resume metadata"
            continue

        agent = _norm_agent(r.get("agent", ""))
        if agent == "codex":
            meta, candidates = _find_codex_resume_near_start(
                r.get("cwd", ""), r.get("started_at", ""))
            if meta and _persist_resume_metadata(r, meta):
                _persist_resume_capture_status(r, "captured", candidates=candidates)
                print(f"[resume-capture] captured codex id "
                      f"{meta.get('resume_id')} for {run_id}",
                      file=sys.stderr, flush=True)
                return
            if candidates:
                last_error = f"ambiguous codex candidates: {len(candidates)}"
                _persist_resume_capture_status(
                    r, "ambiguous", error=last_error, candidates=candidates)
            else:
                last_error = "no codex session_meta candidate found yet"
                _persist_resume_capture_status(r, "pending", error=last_error)
        else:
            meta = _discover_resume_metadata(r)
            if meta and _persist_resume_metadata(r, meta):
                _persist_resume_capture_status(r, "captured")
                print(f"[resume-capture] captured {agent} id "
                      f"{meta.get('resume_id')} for {run_id}",
                      file=sys.stderr, flush=True)
                return
            last_error = f"no {agent or 'agent'} resume metadata found yet"
            _persist_resume_capture_status(r, "pending", error=last_error)

    r = _lookup_run_light(outputs_dir, run_id)
    if r and not r.get("resume_id"):
        _persist_resume_capture_status(r, "missing", error=last_error)
    if last_error:
        print(f"[resume-capture] failed for {run_id}: {last_error}",
              file=sys.stderr, flush=True)


def _schedule_native_resume_capture(
    outputs_dir: Path,
    run_id: str,
    *,
    preallocated_meta: Optional[dict[str, str]] = None,
    preallocation_error: str = "",
) -> None:
    thread = threading.Thread(
        target=_capture_native_resume_for_run,
        args=(outputs_dir, run_id),
        kwargs={
            "preallocated_meta": preallocated_meta,
            "preallocation_error": preallocation_error,
        },
        daemon=True,
    )
    thread.start()


def _path_is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _dashboard_trash_dir() -> Path:
    # Test hooks can redirect this away from the user's real Trash.
    raw = os.environ.get("ORCH_TRASH_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".Trash"


def _unique_trash_target(trash_dir: Path, name: str) -> Path:
    safe_name = name.strip() or "orchestrator-session"
    target = trash_dir / safe_name
    if not target.exists():
        return target
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for i in range(1, 1000):
        suffix = f"-deleted-{stamp}" if i == 1 else f"-deleted-{stamp}-{i}"
        target = trash_dir / f"{safe_name}{suffix}"
        if not target.exists():
            return target
    raise RuntimeError("could not create unique Trash target")


# ---------------------------------------------------------------------------
# Archived projects: `projects/<name>/[<subdir>/]<ts>-<task>.log`
#
# This is the long-term memory layout produced by `orch organize` — it moves
# finished sessions from `outputs/` into topical project folders, keeping
# only the per-session `.log` (ANSI-coloured tmux transcript). Optionally an
# overgrown project gets split into subdirectories (still .log files, just
# nested one level deeper).
#
# The dashboard treats this tree as STRICTLY READ-ONLY. We only expose
# listing + full-log reading + "start a new session preloaded with this
# log as context". Writing/deleting is left to `orch organize` / `orch
# prune` / manual editing on disk.
# ---------------------------------------------------------------------------

_PROJ_TS_RE = re.compile(r"^(\d{8})-(\d{6})-(.+?)\.log$")


def _parse_archived_log_name(filename: str) -> Optional[dict]:
    """Parse `YYYYMMDD-HHMMSS-<task>.log` filename into structured fields.
    Returns None if the filename doesn't match the conventional pattern —
    such files are still listed (with raw name) but without timestamp/task
    inference."""
    m = _PROJ_TS_RE.match(filename)
    if not m:
        return None
    date, time, task = m.groups()
    started_at = (
        f"{date[0:4]}-{date[4:6]}-{date[6:8]}T"
        f"{time[0:2]}:{time[2:4]}:{time[4:6]}"
    )
    return {"date": date, "time": time, "task": task,
            "started_at": started_at}


def _list_archived_logs(project_dir: Path) -> list[dict]:
    """Walk one project folder and return a list of every `.log` file
    (any depth). Each entry carries enough metadata for the sidebar to
    show title / date / size without opening the file."""
    if not project_dir.is_dir():
        return []
    entries: list[dict] = []
    for root, _dirs, files in os.walk(project_dir):
        root_path = Path(root)
        for f in files:
            if not f.endswith(".log"):
                continue
            fp = root_path / f
            try:
                st_info = fp.stat()
            except OSError:
                continue
            rel = fp.relative_to(project_dir).as_posix()
            parsed = _parse_archived_log_name(f) or {}
            entries.append({
                "rel": rel,
                "name": f,
                "subdir": rel[:-len(f) - 1] if "/" in rel else "",
                "task": parsed.get("task", f[:-4]),
                "started_at": parsed.get("started_at", ""),
                "size": st_info.st_size,
                "mtime": st_info.st_mtime,
            })
    # Sort newest first by mtime (timestamp in filename is usually a good
    # proxy, but mtime handles files that were edited after archiving).
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def _list_projects(projects_dir: Path) -> list[dict]:
    """Enumerate the projects/ root: one dict per project folder."""
    if not projects_dir.is_dir():
        return []
    out: list[dict] = []
    for child in sorted(projects_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        logs = _list_archived_logs(child)
        if not logs:
            continue  # skip empty directories
        out.append({
            "name": child.name,
            "count": len(logs),
            "latest_mtime": max(e["mtime"] for e in logs),
        })
    # Sort projects by most recently touched (descending).
    out.sort(key=lambda p: p["latest_mtime"], reverse=True)
    return out


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _search_archived_logs(projects_dir: Path, query: str,
                          limit: int = 200,
                          snippet_ctx: int = 60) -> dict:
    """Full-text grep across every `.log` in `projects/<*>/` (recursive).

    Case-insensitive substring match (no regex — keeps this endpoint
    safe from user-supplied regex DoS). For each hit we return ONE
    snippet — the first match per file — with the match highlighted
    in the surrounding `snippet_ctx` chars on each side.

    ANSI escape sequences are stripped from the snippet so the sidebar
    preview stays readable. Result is capped at `limit` files to keep
    the sidebar render cheap.
    """
    q = (query or "").strip()
    if not q:
        return {"results": [], "truncated": False, "scanned": 0}
    q_lower = q.lower()
    results: list[dict] = []
    scanned = 0
    truncated = False
    if not projects_dir.is_dir():
        return {"results": results, "truncated": False, "scanned": 0}

    for proj_dir in sorted(projects_dir.iterdir(), key=lambda p: p.name.lower()):
        if not proj_dir.is_dir() or proj_dir.name.startswith("."):
            continue
        for root, _dirs, files in os.walk(proj_dir):
            root_path = Path(root)
            for f in files:
                if not f.endswith(".log"):
                    continue
                fp = root_path / f
                scanned += 1
                try:
                    # 8 MB cap per file — archived sessions are usually
                    # well under this, and we only need the first match.
                    raw = fp.read_text(errors="replace")
                    if len(raw) > 8 * 1024 * 1024:
                        raw = raw[: 8 * 1024 * 1024]
                except OSError:
                    continue
                low = raw.lower()
                idx = low.find(q_lower)
                if idx < 0:
                    continue
                start = max(0, idx - snippet_ctx)
                end = min(len(raw), idx + len(q) + snippet_ctx)
                raw_snippet = raw[start:end]
                snippet = _ANSI_RE.sub("", raw_snippet)
                # Collapse huge whitespace runs so the preview stays readable.
                snippet = re.sub(r"\s+", " ", snippet).strip()
                try:
                    mtime = fp.stat().st_mtime
                except OSError:
                    mtime = 0
                results.append({
                    "project": proj_dir.name,
                    "rel": fp.relative_to(proj_dir).as_posix(),
                    "snippet": snippet,
                    "mtime": mtime,
                })
                if len(results) >= limit:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break

    # Newest first so recent work floats to the top of the sidebar.
    results.sort(key=lambda r: r["mtime"], reverse=True)
    return {"results": results, "truncated": truncated, "scanned": scanned}


def _safe_project_log_path(projects_dir: Path, project: str,
                           rel: str) -> Optional[Path]:
    """Resolve `projects_dir/<project>/<rel>` rejecting any path that
    escapes via `..` or symlinks. Returns None if invalid / missing /
    not a .log file."""
    if "/" in project or project in ("", ".", ".."):
        return None
    if not rel.endswith(".log"):
        return None
    base = (projects_dir / project).resolve()
    if not base.is_dir():
        return None
    target = (base / rel).resolve()
    try:
        target.relative_to(base)  # reject path traversal
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target


# ---------------------------------------------------------------------------
# state.json edit helper (full mode only)
# ---------------------------------------------------------------------------

ALLOWED_STATE_FIELDS = {
    "mode",
    "status",
    "max_rounds",
    "idle_timeout",
}
ALLOWED_PANEL_STATES = {"", "p0", "p1", "p2", "blocked", "watching", "done"}
ALLOWED_TERMINAL_THEMES = {"", "soft-dark", "soft-light", "soft-green", "light"}


def _update_state_json(run_dir: Path, task_name: str, updates: dict) -> bool:
    state_file = run_dir / "state.json"
    if not state_file.exists():
        return False
    data = _safe_read_json(state_file) or {}
    if task_name not in data:
        return False
    for k, v in updates.items():
        if k in ALLOWED_STATE_FIELDS:
            data[task_name][k] = v
    state_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return True


def _persist_panel_state(r: dict[str, Any], panel_state: str) -> None:
    panel_state = (panel_state or "").strip().lower()
    if panel_state not in ALLOWED_PANEL_STATES:
        raise HTTPException(
            400,
            f"panel_state must be one of {sorted(ALLOWED_PANEL_STATES)}",
        )
    if r.get("kind") == "task":
        state_file = Path(r["run_dir"]) / "state.json"
        data = _safe_read_json(state_file) or {}
        task = r.get("task", "")
        if task not in data or not isinstance(data[task], dict):
            raise HTTPException(404, "task not found in state.json")
        data[task]["panel_state"] = panel_state
        _safe_write_json(state_file, data)
        return
    if r.get("kind") == "run" and r.get("run_dir"):
        sess_file = Path(r["run_dir"]) / "session.json"
        if not sess_file.exists():
            raise HTTPException(400, "session.json not found")
        data = _safe_read_json(sess_file) or {}
        data["panel_state"] = panel_state
        _safe_write_json(sess_file, data)
        return
    raise HTTPException(
        400,
        f"kind={r.get('kind', '')} does not support panel_state",
    )


def _persist_terminal_theme(r: dict[str, Any], terminal_theme: str) -> str:
    terminal_theme = _normalize_terminal_theme(terminal_theme)
    if terminal_theme not in ALLOWED_TERMINAL_THEMES:
        raise HTTPException(
            400,
            f"terminal_theme must be one of {sorted(ALLOWED_TERMINAL_THEMES)}",
        )
    if r.get("kind") == "task":
        state_file = Path(r["run_dir"]) / "state.json"
        data = _safe_read_json(state_file) or {}
        task = r.get("task", "")
        if task not in data or not isinstance(data[task], dict):
            raise HTTPException(404, "task not found in state.json")
        data[task]["terminal_theme"] = terminal_theme
        _safe_write_json(state_file, data)
        return terminal_theme
    if r.get("kind") == "run" and r.get("run_dir"):
        sess_file = Path(r["run_dir"]) / "session.json"
        if not sess_file.exists():
            raise HTTPException(400, "session.json not found")
        data = _safe_read_json(sess_file) or {}
        data["terminal_theme"] = terminal_theme
        _safe_write_json(sess_file, data)
        return terminal_theme
    raise HTTPException(
        400,
        f"kind={r.get('kind', '')} does not support terminal_theme",
    )


def _copy_snapshot_ui_metadata_to_spawned_run(
    spawn_result: dict[str, Any],
    entry: dict[str, Any],
    *,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    has_panel_state = "panel_state" in entry
    has_terminal_theme = "terminal_theme" in entry
    panel_state = str(entry.get("panel_state") or "").strip().lower()
    terminal_theme = _normalize_terminal_theme(str(entry.get("terminal_theme") or ""))
    updates: dict[str, str] = {}
    warnings: list[str] = []
    if has_panel_state:
        if panel_state in ALLOWED_PANEL_STATES:
            updates["panel_state"] = panel_state
        else:
            warnings.append(f"unsupported panel_state: {panel_state}")
    if has_terminal_theme:
        if terminal_theme in ALLOWED_TERMINAL_THEMES:
            updates["terminal_theme"] = terminal_theme
        else:
            warnings.append(f"unsupported terminal_theme: {terminal_theme}")
    if not updates:
        return {
            "copied": False,
            "panel_state": "",
            "terminal_theme": "",
            "warning": "; ".join(warnings),
        }

    run_dir_raw = str(spawn_result.get("run_dir") or "").strip()
    if not run_dir_raw:
        warnings.append("spawned run has no run directory")
        return {
            "copied": False,
            "panel_state": updates.get("panel_state", ""),
            "terminal_theme": updates.get("terminal_theme", ""),
            "warning": "; ".join(warnings),
        }
    try:
        run_dir = Path(run_dir_raw).expanduser().resolve()
    except OSError as exc:
        warnings.append(str(exc))
        return {
            "copied": False,
            "panel_state": updates.get("panel_state", ""),
            "terminal_theme": updates.get("terminal_theme", ""),
            "warning": "; ".join(warnings),
        }

    session_json = run_dir / "session.json"
    deadline = time.time() + max(0.5, timeout_s)
    while time.time() < deadline:
        if session_json.exists():
            break
        time.sleep(0.1)
    if not session_json.exists():
        warnings.append(f"session.json not found at {session_json}")
        return {
            "copied": False,
            "panel_state": updates.get("panel_state", ""),
            "terminal_theme": updates.get("terminal_theme", ""),
            "warning": "; ".join(warnings),
        }

    data = _safe_read_json(session_json) or {}
    data.update(updates)
    try:
        _safe_write_json(session_json, data)
    except OSError as exc:
        warnings.append(str(exc))
        return {
            "copied": False,
            "panel_state": updates.get("panel_state", ""),
            "terminal_theme": updates.get("terminal_theme", ""),
            "warning": "; ".join(warnings),
        }
    return {
        "copied": True,
        "panel_state": updates.get("panel_state", ""),
        "terminal_theme": updates.get("terminal_theme", ""),
        "warning": "; ".join(warnings),
    }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ttyd integration: lazily spawn `ttyd tmux attach -t <session>` per run so
# users can get a real pty in the browser (full scroll, keybindings, etc.).
# ---------------------------------------------------------------------------

SHADOW_SUFFIX = "-web"


def shadow_name(session: str) -> str:
    """Canonical shadow session name for a given original session."""
    if session.endswith(SHADOW_SUFFIX):
        return session  # already a shadow
    return session + SHADOW_SUFFIX


def ensure_shadow_session(session: str, cols: int = 0,
                          rows: int = 0) -> Optional[str]:
    """Ensure a grouped shadow session exists for `session` and return its name.

    The shadow is a grouped session (`tmux new-session -t <group>`) sharing
    the original's windows/panes, so capture-pane / ttyd attached to the
    shadow sees the same buffer as iTerm on the original. Safe to call
    repeatedly (idempotent).

    CAVEAT — reflow between iTerm and the browser IS possible.

    `window-size` is a *window* option, and grouped sessions share the same
    window object. With the default `window-size=latest`, tmux redraws the
    window at whichever client (iTerm or ttyd/browser) last attached or
    resized. In practice we've seen this manifest as occasional visual
    reflow on the iTerm side when the browser viewport is a different
    size. If you hit that, options to try (not currently applied because
    the trade-offs were worse in earlier attempts):

      - `window-size=largest`: iTerm stays at its native size as long as
        it's wider than the browser; browser letterboxes instead.
      - `window-size=manual` + fixed -x/-y: pin a geometry and make both
        clients letterbox; simplest but cuts off the smaller client.

    The `cols`/`rows` parameters are accepted for backwards compatibility
    but currently ignored — tmux follows `window-size=latest`.

    Options set on the shadow:
      - mouse on: browser wheel events drive tmux's own scrollback (50k
        lines). `mouse` is a per-session option, independent from the
        original session iTerm attaches to.
      - status off, history-limit 50000: shadow-only cosmetics/buffer.
      - window-size latest + aggressive-resize on: leave sizing to tmux's
        default behavior; see caveat above.
    """
    if not session or not tmux_alive(session):
        return None
    if session.endswith(SHADOW_SUFFIX):
        return session
    shadow = shadow_name(session)
    is_new = not tmux_alive(shadow)
    if is_new:
        try:
            r = subprocess.run(
                ["tmux", "new-session", "-d", "-s", shadow, "-t", session],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return None
        except (subprocess.SubprocessError, FileNotFoundError):
            return None
    # Re-apply every call (idempotent) so policy changes take effect on
    # already-running shadows without requiring manual kill-session.
    # `mouse` is session-scoped; changing it on the shadow does NOT affect
    # the iTerm-attached original session.
    for opt, val in (
        ("mouse", "on"),
        ("history-limit", "50000"),
        ("status", "off"),
    ):
        subprocess.run(
            ["tmux", "set-option", "-t", shadow, opt, val],
            capture_output=True, text=True, timeout=5,
        )
    # NOTE: window-size is a *window* option and grouped sessions share
    # their windows, so setting it on the shadow also affects how the
    # iTerm-side original draws. See docstring caveat for alternatives.
    for opt, val in (
        ("window-size", "latest"),
        ("aggressive-resize", "on"),
    ):
        subprocess.run(
            ["tmux", "set-option", "-w", "-t", shadow, opt, val],
            capture_output=True, text=True, timeout=5,
        )
    return shadow


def kill_shadow_session(session: str) -> None:
    shadow = shadow_name(session)
    try:
        subprocess.run(["tmux", "kill-session", "-t", shadow],
                       capture_output=True, text=True, timeout=3)
    except (subprocess.SubprocessError, FileNotFoundError):
        pass


class TtydManager:
    def __init__(self, enabled: bool, base_port: int = 7800,
                 reserved_ports: Optional[set[int]] = None):
        self.enabled = enabled
        self.base_port = base_port
        self.reserved_ports = set(reserved_ports or set())
        self._procs: dict[str, tuple[subprocess.Popen, int, str]] = {}
        self._next_port = base_port
        # Sweep orphan ttyd processes left behind by a previous dashboard
        # that crashed / was SIGKILLed before it could call stop_all. Without
        # this, the new dashboard's TtydManager will try to bind ports
        # starting at base_port, find them occupied, silently advance to
        # higher ports, but still proxy to the orphans' port numbers
        # (because from the outside both look like "ttyd on 7800"). Double
        # attaches = tmux churn = input lag. Run once at construction.
        if enabled:
            self._sweep_orphans()

    @staticmethod
    def _sweep_orphans() -> int:
        """Kill stray ttyd processes that aren't owned by this TtydManager.

        Heuristic: any `ttyd -p <port>` process whose parent is launchd
        (PPID=1) is an orphan from a prior dashboard crash. We don't touch
        ttyd processes with a live parent — that would race with a sibling
        dashboard running on a different port.
        """
        try:
            out = subprocess.run(
                ["ps", "-eo", "pid,ppid,command"],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return 0
        killed = 0
        for line in out.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            pid, ppid, cmd = parts
            if "ttyd -p" not in cmd:
                continue
            try:
                pid_i, ppid_i = int(pid), int(ppid)
            except ValueError:
                continue
            # PPID=1 means launchd reparented it → orphan from a dead
            # dashboard. Leave other ttyds alone (unit tests, other users).
            if ppid_i != 1:
                continue
            try:
                os.kill(pid_i, 9)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
        return killed

    def available(self) -> bool:
        if not self.enabled:
            return False
        try:
            subprocess.run(["ttyd", "--version"], capture_output=True, timeout=3)
            return True
        except (subprocess.SubprocessError, FileNotFoundError):
            return False

    @staticmethod
    def _port_listening(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.2):
                return True
        except OSError:
            return False

    def _next_free_port(self) -> Optional[int]:
        for _ in range(1000):
            port = self._next_port
            self._next_port += 1
            if port in self.reserved_ports:
                continue
            if self._port_listening(port):
                continue
            return port
        return None

    def _wait_for_port(self, proc: subprocess.Popen, port: int,
                       timeout_s: float = 1.2) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if proc.poll() is not None:
                return False
            if self._port_listening(port):
                return True
            time.sleep(0.05)
        return False

    @staticmethod
    def _stop_proc(proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=0.8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def ensure(self, session: str, theme: str = "") -> Optional[int]:
        """Start (or reuse) a ttyd attached to a *shadow* tmux client for
        `session`. See `ensure_shadow_session` for shadow semantics.
        """
        if not self.enabled or not session or not tmux_alive(session):
            return None
        theme = _normalize_terminal_theme(theme)
        existing = self._procs.get(session)
        if existing:
            proc, port, existing_theme = existing
            if proc.poll() is None and existing_theme == theme:
                return port
            if proc.poll() is None:
                self._stop_proc(proc)
            self._procs.pop(session, None)

        shadow = ensure_shadow_session(session)
        if not shadow:
            return None

        for _ in range(20):
            port = self._next_free_port()
            if port is None:
                return None
            client_options = [
                "-t", "titleFixed=" + session,
                "-t", "fontSize=13",
                # Browser-side xterm scrollback. With tmux mouse=on in
                # the shadow session, wheel events are consumed by tmux
                # for its own copy-mode scrollback (history-limit=50000)
                # and browser scrollback stays mostly untouched. We still
                # keep a sane buffer here in case some event slips
                # through (e.g. before the `mouse` option takes effect
                # on a freshly-created shadow).
                "-t", "scrollback=20000",
                "-t", "disableLeaveAlert=true",
                "-t", "rightClickSelectsWord=true",
                "-t", "macOptionIsMeta=true",
                # With tmux `mouse on`, left-drag is intercepted by
                # tmux so the browser never sees a text selection and
                # Cmd+C copies nothing. This xterm.js option makes
                # Option+Drag bypass the mouse protocol and do a
                # native browser selection instead, which Cmd+C can
                # then copy. So the UX becomes:
                #   - normal drag   -> tmux copy-mode (scroll/select)
                #   - Option + drag -> browser text select -> Cmd+C
                "-t", "macOptionClickForcesSelection=true",
            ]
            theme_option = _ttyd_theme_client_option(theme)
            if theme_option:
                client_options.extend(["-t", theme_option])
            try:
                proc = subprocess.Popen(
                    [
                        "ttyd",
                        "-p", str(port),
                        "-i", "127.0.0.1",     # only expose locally; dashboard proxies it
                        "-W",                   # writable (allow input)
                        *client_options,
                        "tmux", "attach", "-t", shadow,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, FileNotFoundError):
                return None
            if self._wait_for_port(proc, port):
                self._procs[session] = (proc, port, theme)
                return port
            self._stop_proc(proc)
        return None

    def port_for(self, session: str) -> Optional[int]:
        """Return the port of a live ttyd for `session` (or start a default one).

        `/api/sessions/{run_id}/tty` is responsible for applying any
        per-session theme before the iframe is created. The reverse-proxy
        routes only know the tmux session name, so they must reuse an existing
        ttyd instead of calling `ensure(..., theme="")` and accidentally
        replacing a themed terminal with the default one.
        """
        existing = self._procs.get(session)
        if existing:
            proc, port, _theme = existing
            if proc.poll() is None:
                return port
            self._procs.pop(session, None)
        return self.ensure(session)

    def theme_for(self, session: str) -> str:
        """Return the live ttyd theme for `session` without scanning runs.

        The iframe proxy serves ttyd's index for every pane on browser reload.
        Looking up the theme through `_discover_runs()` there is expensive
        because it scans every output dir and probes tmux activity; the ttyd
        manager already knows the theme selected when `/api/.../tty` called
        `ensure()`.
        """
        existing = self._procs.get(session)
        if not existing:
            return ""
        proc, _port, theme = existing
        if proc.poll() is not None:
            self._procs.pop(session, None)
            return ""
        return _normalize_terminal_theme(theme)

    def reap_dead(self) -> int:
        """Terminate ttyd processes whose underlying tmux session is gone.

        Called periodically from `_discover_runs` so a session that was
        killed from iTerm (tmux kill-session) doesn't leave a ttyd +
        shadow session dangling forever. Returns the number of entries
        reaped. Safe to call from any thread — subprocess.terminate and
        the dict ops are cheap.
        """
        reaped = 0
        for session, (proc, _, _) in list(self._procs.items()):
            if tmux_alive(session) and proc.poll() is None:
                continue
            try:
                proc.terminate()
            except Exception:
                pass
            kill_shadow_session(session)
            self._procs.pop(session, None)
            reaped += 1
        return reaped

    def stop_all(self):
        for session, (proc, _, _) in list(self._procs.items()):
            try:
                proc.terminate()
            except Exception:
                pass
            kill_shadow_session(session)
        self._procs.clear()


def create_app(outputs_dir: Path, token: Optional[str] = None,
               ttyd_enabled: bool = False, ttyd_host: str = "127.0.0.1",
               bind_host: str = "127.0.0.1", port: int = 7860,
               scheme: str = "http",
               publish_icloud: bool = False,
               projects_dir: Optional[Path] = None) -> FastAPI:
    # `projects/` is the archival tree produced by `orch organize`. It lives
    # next to `outputs/` by default. We keep the handle around so projects
    # endpoints can read it (no writes happen here — writes are done by
    # organize/prune on disk).
    projects_dir = _configured_projects_dir(outputs_dir, projects_dir)
    ttyd = TtydManager(enabled=ttyd_enabled, reserved_ports={port})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if (app.state.active_snapshot_autosave_enabled
                and app.state.active_snapshot_autosave_thread is None):
            stop_event = threading.Event()
            interval = float(app.state.active_snapshot_autosave_interval)

            def autosave_loop():
                while not stop_event.wait(interval):
                    try:
                        path, snapshot = _save_active_snapshot(
                            outputs_dir,
                            saved_by="auto-hourly",
                        )
                        sessions = snapshot.get("sessions") if isinstance(
                            snapshot.get("sessions"), list) else []
                        skipped = snapshot.get("skipped") if isinstance(
                            snapshot.get("skipped"), list) else []
                        print(
                            "INFO: active snapshot autosaved "
                            f"{len(sessions)} session(s), skipped {len(skipped)} -> {path}",
                            file=sys.stderr,
                            flush=True,
                        )
                    except Exception as exc:
                        print(
                            f"WARNING: active snapshot autosave failed: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )

            thread = threading.Thread(
                target=autosave_loop,
                daemon=True,
                name="orch-active-snapshot-autosave",
            )
            app.state.active_snapshot_autosave_stop = stop_event
            app.state.active_snapshot_autosave_thread = thread
            thread.start()

        try:
            yield
        finally:
            stop_event = app.state.active_snapshot_autosave_stop
            thread = app.state.active_snapshot_autosave_thread
            if isinstance(stop_event, threading.Event):
                stop_event.set()
            if isinstance(thread, threading.Thread):
                thread.join(timeout=2)
            app.state.active_snapshot_autosave_stop = None
            app.state.active_snapshot_autosave_thread = None

    app = FastAPI(
        title="Agent Orchestrator Dashboard",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.ttyd = ttyd
    app.state.ttyd_host = ttyd_host
    app.state.bind_host = bind_host
    app.state.port = port
    app.state.scheme = scheme
    app.state.active_snapshot_autosave_enabled = _active_snapshot_autosave_enabled()
    app.state.active_snapshot_autosave_interval = _active_snapshot_autosave_interval()
    app.state.active_snapshot_autosave_stop = None
    app.state.active_snapshot_autosave_thread = None

    # Optional background publisher: write current URL to iCloud Drive so a
    # phone can pick up the latest address without guessing. Writes on IP
    # change only (not on every tick) to avoid spurious iCloud sync traffic.
    if publish_icloud:
        icloud = icloud_drive_dir()
        if icloud:
            target = icloud / "orch-dashboard.txt"

            def publisher():
                last = None
                while True:
                    try:
                        ip = pick_best_ip(bind_host)
                        url = build_access_url(ip, port, scheme, token)
                        if url and url != last:
                            body = (
                                "Agent Orchestrator Dashboard\n"
                                f"updated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                                f"url:     {url}\n"
                                "\n"
                                "All reachable candidates:\n"
                            )
                            for iface, cand_ip in list_local_ipv4():
                                body += f"  [{iface:10s}] {build_access_url(cand_ip, port, scheme, token)}\n"
                            try:
                                target.write_text(body)
                                last = url
                            except OSError:
                                pass
                    except Exception:
                        pass
                    time.sleep(30)

            t = threading.Thread(target=publisher, daemon=True, name="orch-icloud-pub")
            t.start()
            app.state.icloud_file = str(target)
        else:
            app.state.icloud_file = None
    else:
        app.state.icloud_file = None

    def auth_check(authorization: Optional[str]):
        if not token:
            return
        expected = f"Bearer {token}"
        if not _token_matches(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid token")

    @app.middleware("http")
    async def _token_gate(request: Request, call_next):
        authenticated = False
        if token and not request.url.path.startswith(("/api/health", "/static", "/favicon")):
            # API calls use Authorization; the initial page can use ?token=.
            # A same-origin HttpOnly cookie carries auth into ttyd iframe
            # assets and its WebSocket handshake.
            q_tok = request.query_params.get("token")
            h_tok = request.headers.get("authorization", "")
            c_tok = request.cookies.get("orch_token")
            authenticated = _dashboard_auth_matches(
                token, query_token=q_tok, authorization=h_tok,
                cookie_token=c_tok,
            )
            if not authenticated and request.url.path != "/":
                # Allow GET / to render a page that prompts for a token.
                return JSONResponse({"error": "invalid token"}, status_code=401)
        response = await call_next(request)
        if (token and authenticated
                and not _token_matches(request.cookies.get("orch_token"), token)):
            response.set_cookie(
                "orch_token", token, httponly=True, samesite="strict",
                secure=(scheme == "https"),
            )
        return response

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "auth": bool(token),
            "ttyd": ttyd.available(),
            "bind_host": bind_host,
            "port": port,
            "scheme": scheme,
            "active_snapshot_autosave": {
                "enabled": bool(app.state.active_snapshot_autosave_enabled),
                "interval_seconds": app.state.active_snapshot_autosave_interval,
            },
        }

    @app.get("/api/config")
    def dashboard_config():
        return _dashboard_client_config()

    @app.get("/api/host")
    def host_info():
        """Return the 'best' URL a phone on your VPN / LAN can use to reach
        this dashboard. Scans network interfaces; prefers VPN/tunnel IPs
        over home LAN, falls back to loopback."""
        ifaces = list_local_ipv4()
        best_ip = pick_best_ip(bind_host)
        url = build_access_url(best_ip, port, scheme, token)
        candidates = []
        for iface, ip in ifaces:
            candidates.append({
                "iface": iface,
                "ip": ip,
                "url": build_access_url(ip, port, scheme, token),
            })
        return {
            "bind_host": bind_host,
            "port": port,
            "scheme": scheme,
            "best_ip": best_ip,
            "best_url": url,
            "candidates": candidates,
        }

    @app.get("/api/sessions/{run_id}/tty")
    def get_tty(run_id: str, request: Request):
        """Return a *same-origin* URL that reverse-proxies to ttyd for this
        session. Using a same-origin URL avoids cross-port iframe/websocket
        quirks (some browsers/extensions block direct 127.0.0.1:<random>)."""
        if not ttyd.enabled:
            return {"ok": False, "reason": "ttyd not enabled (start dashboard with --ttyd)"}
        if not ttyd.available():
            return {"ok": False, "reason": "ttyd binary not found in PATH"}
        if httpx is None or websockets is None:
            return {"ok": False, "reason": "ttyd reverse-proxy deps missing "
                                           "(pip install httpx websockets)"}
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        session = r.get("tmux_session", "")
        if not session or not tmux_alive(session):
            return {"ok": False, "reason": "tmux session not alive"}
        port = ttyd.ensure(session, theme=str(r.get("terminal_theme") or ""))
        if not port:
            return {"ok": False, "reason": "failed to launch ttyd"}
        # NOTE trailing slash matters — ttyd serves SPA from `/`.
        return {"ok": True, "url": f"/tty/{session}/"}

    # ---- Reverse proxy for ttyd (HTTP + WebSocket) ----
    # Keeps the iframe same-origin with the dashboard.
    # Session is the tmux session name (url-safe in practice since we generate
    # them as `orch-<slug>`).

    async def _proxy_http(session: str, subpath: str, request: Request):
        port = ttyd.port_for(session)
        if not port:
            raise HTTPException(502, "ttyd not available for this session")
        url = f"http://127.0.0.1:{port}/{subpath}"
        # Forward headers except Host (and hop-by-hop). Body too.
        fwd_headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in ("host", "connection", "content-length")}
        # Use stream to handle arbitrary sizes (ttyd index is ~730KB).
        async with httpx.AsyncClient(timeout=30) as client:
            req = client.build_request(request.method, url,
                                       headers=fwd_headers,
                                       params=request.query_params,
                                       content=await request.body())
            resp = await client.send(req, stream=False)
        resp_headers = {k: v for k, v in resp.headers.items()
                        if k.lower() not in ("content-encoding", "transfer-encoding",
                                             "content-length", "connection")}
        content = resp.content
        content_type = resp.headers.get("content-type", "")
        if (resp.status_code == 200 and (not subpath or subpath == "index.html")
                and "text/html" in content_type.lower()):
            theme = ttyd.theme_for(session)
            patched = _patch_ttyd_index_theme(content, theme)
            if patched != content:
                content = patched
                resp_headers.pop("etag", None)
                resp_headers["cache-control"] = "no-cache, no-store, must-revalidate"
        return Response(content=content, status_code=resp.status_code,
                        headers=resp_headers, media_type=resp.headers.get("content-type"))

    @app.get("/tty/{session}")
    async def tty_root_redirect(session: str):
        # Canonical URL has trailing slash so ttyd's relative asset paths resolve.
        return Response(status_code=307, headers={"location": f"/tty/{session}/"})

    @app.get("/tty/{session}/")
    async def tty_index(session: str, request: Request):
        if not tmux_alive(session):
            raise HTTPException(404, "tmux session not alive")
        return await _proxy_http(session, "", request)

    @app.api_route(
        "/tty/{session}/{subpath:path}",
        methods=["GET", "POST", "HEAD"],
        include_in_schema=False,
    )
    async def tty_asset(session: str, subpath: str, request: Request):
        # Don't let HTTP catch WS upgrade paths.
        if subpath == "ws":
            raise HTTPException(426, "use websocket upgrade")
        return await _proxy_http(session, subpath, request)

    @app.websocket("/tty/{session}/ws")
    async def tty_ws(ws: WebSocket, session: str):
        if token and not _dashboard_auth_matches(
            token, query_token=ws.query_params.get("token"),
            cookie_token=ws.cookies.get("orch_token"),
        ):
            await ws.close(code=1008, reason="invalid token")
            return
        await ws.accept(subprotocol="tty")  # ttyd uses the "tty" subprotocol
        port = ttyd.port_for(session)
        if not port:
            await ws.close(code=1011, reason="ttyd not available")
            return
        upstream_url = f"ws://127.0.0.1:{port}/ws"
        try:
            async with websockets.connect(upstream_url, subprotocols=["tty"],
                                          max_size=None, ping_interval=None) as up:
                async def c2s():
                    try:
                        while True:
                            msg = await ws.receive()
                            if msg["type"] == "websocket.disconnect":
                                return
                            data = msg.get("bytes") if msg.get("bytes") is not None else msg.get("text")
                            if data is None:
                                continue
                            await up.send(data)
                    except (WebSocketDisconnect, WsClosed):
                        return
                async def s2c():
                    try:
                        async for msg in up:
                            if isinstance(msg, bytes):
                                await ws.send_bytes(msg)
                            else:
                                await ws.send_text(msg)
                    except (WebSocketDisconnect, WsClosed):
                        return
                await asyncio.gather(c2s(), s2c())
        except Exception as e:
            try:
                await ws.close(code=1011, reason=str(e)[:120])
            except Exception:
                pass

    @app.get("/api/sessions")
    def list_sessions():
        # Reap ttyd processes whose tmux session is gone so a killed
        # session doesn't leave a zombie ttyd + shadow behind. Cheap
        # (dict scan + subprocess.poll); runs on every poll tick.
        try:
            ttyd.reap_dead()
        except Exception:
            pass
        return {"sessions": _discover_runs(outputs_dir)}

    @app.get("/api/sessions/{run_id}")
    def get_session(run_id: str):
        r = _lookup_run(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        return r

    @app.get("/api/daily-summary")
    def daily_summary(date: Optional[str] = None,
                      start: Optional[str] = None,
                      end: Optional[str] = None,
                      idle_minutes: Optional[int] = 60):
        """Collect session + linked-folder activity for one day or range.

        Query examples:
          /api/daily-summary?date=2026-05-18
          /api/daily-summary?date=2026-05-18&idle_minutes=60
          /api/daily-summary?start=2026-05-18&end=2026-05-19
          /api/daily-summary?start=2026-05-18T09:00:00&end=2026-05-18T18:00:00
        """
        range_start, range_end, _ = _summary_time_range(date, start, end)
        return _collect_daily_summary(outputs_dir, range_start, range_end,
                                      idle_minutes=_normalized_idle_minutes(idle_minutes))

    @app.get("/api/daily-summary/html")
    def daily_summary_html(date: Optional[str] = None,
                           start: Optional[str] = None,
                           end: Optional[str] = None,
                           idle_minutes: Optional[int] = 60):
        range_start, range_end, _ = _summary_time_range(date, start, end)
        summary = _collect_daily_summary(outputs_dir, range_start, range_end,
                                         idle_minutes=_normalized_idle_minutes(idle_minutes))
        return Response(
            content=_daily_summary_html(summary),
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.delete("/api/sessions/{run_id}")
    def delete_session(run_id: str):
        rows = _discover_runs(outputs_dir)
        r = next((row for row in rows if row.get("run_id") == run_id), None)
        if not r:
            raise HTTPException(404, "run not found")

        run_dir_raw = (r.get("run_dir") or "").strip()
        if not run_dir_raw:
            raise HTTPException(400, "session has no run folder")
        run_dir = Path(run_dir_raw).expanduser()
        if not run_dir.exists():
            raise HTTPException(404, "run folder not found")
        if not run_dir.is_dir():
            raise HTTPException(400, "run folder is not a directory")

        try:
            run_dir_resolved = run_dir.resolve()
            outputs_root = outputs_dir.resolve()
        except OSError as e:
            raise HTTPException(500, f"failed to resolve run folder: {e}")

        if run_dir_resolved == outputs_root or not _path_is_within(run_dir_resolved, outputs_root):
            raise HTTPException(400, "refusing to delete folder outside outputs/")

        same_folder = []
        for row in rows:
            raw = (row.get("run_dir") or "").strip()
            if not raw:
                continue
            try:
                if Path(raw).expanduser().resolve() == run_dir_resolved:
                    same_folder.append(row)
            except OSError:
                continue

        live_rows = [
            row for row in same_folder
            if row.get("alive")
            or (row.get("tmux_session") and tmux_alive(str(row.get("tmux_session"))))
        ]
        if live_rows:
            raise HTTPException(409, "run folder still has live session entries")

        trash_dir = _dashboard_trash_dir()
        try:
            trash_dir.mkdir(parents=True, exist_ok=True)
            trash_target = _unique_trash_target(trash_dir, run_dir_resolved.name)
            shutil.move(str(run_dir_resolved), str(trash_target))
        except OSError as e:
            raise HTTPException(500, f"failed to move run folder to Trash: {e}")
        except RuntimeError as e:
            raise HTTPException(500, str(e))

        affected = [row.get("run_id", "") for row in same_folder if row.get("run_id")]
        return {
            "ok": True,
            "run_id": run_id,
            "run_dir": str(run_dir_resolved),
            "trashed": str(trash_target),
            "affected_run_ids": affected or [run_id],
            "entries_removed": len(affected) or 1,
        }

    @app.get("/api/sessions/{run_id}/pane", response_class=PlainTextResponse)
    def get_pane(run_id: str):
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        session = r.get("tmux_session", "")
        if session and tmux_alive(session):
            text = tmux_capture(session)
            if text:
                return text
        # Fall back to the log file if tmux is gone.
        run_dir = r.get("run_dir", "")
        log_file = r.get("log_file", "")
        if run_dir and log_file:
            log_path = Path(run_dir) / log_file
            if log_path.exists():
                try:
                    return log_path.read_text()
                except OSError:
                    return ""
        return ""

    @app.get("/api/sessions/{run_id}/stream")
    async def stream_pane(run_id: str, request: Request):
        """SSE stream: push pane content only when it changes (hash-based diff)."""
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")

        async def event_gen():
            last_hash = ""
            # Initial snapshot: always send once so the client has something.
            force_first = True
            while True:
                if await request.is_disconnected():
                    break
                # Re-resolve each tick so we pick up state.json/mode changes.
                cur = _lookup_run_light(outputs_dir, run_id) or r
                session = cur.get("tmux_session", "")
                if session and tmux_alive(session):
                    text = tmux_capture(session)
                else:
                    run_dir = cur.get("run_dir", "")
                    log_file = cur.get("log_file", "")
                    if run_dir and log_file:
                        p = Path(run_dir) / log_file
                        text = p.read_text() if p.exists() else ""
                    else:
                        text = ""
                h = hashlib.md5(text.encode("utf-8", "replace")).hexdigest()
                if h != last_hash or force_first:
                    last_hash = h
                    force_first = False
                    # Heartbeat + state metadata every push.
                    meta = {
                        "alive": cur.get("alive"),
                        "status": cur.get("status"),
                        "mode": cur.get("mode"),
                        "round": cur.get("round"),
                        "max_rounds": cur.get("max_rounds"),
                        "panel_state": cur.get("panel_state", ""),
                    }
                    yield f"event: meta\ndata: {json.dumps(meta)}\n\n"
                    # Text can contain blank lines → prefix every line with "data: ".
                    safe = text.replace("\r", "")
                    payload = "\n".join("data: " + ln for ln in safe.split("\n"))
                    yield f"event: pane\n{payload}\n\n"
                else:
                    # Periodic heartbeat so intermediaries don't drop the connection.
                    yield ": ping\n\n"
                await asyncio.sleep(0.8)

        headers = {
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return StreamingResponse(event_gen(), media_type="text/event-stream", headers=headers)

    @app.get("/api/sessions/{run_id}/log", response_class=PlainTextResponse)
    def get_log(run_id: str, tail: int = 0):
        r = _lookup_run(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        log_path = Path(r["run_dir"]) / r.get("log_file", "")
        if not log_path.exists():
            return ""
        try:
            text = log_path.read_text()
        except OSError:
            return ""
        if tail > 0:
            lines = text.splitlines()
            text = "\n".join(lines[-tail:])
        return text

    @app.get("/api/sessions/{run_id}/folders")
    def get_session_folders(run_id: str):
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        folders = [
            _linked_folder_summary(rec)
            for rec in _normalize_linked_folders(r.get("linked_folders"))
        ]
        return {"run_id": run_id, "folders": folders}

    @app.get("/api/sessions/{run_id}/folders/search")
    def search_session_folders(run_id: str, q: str = Query(""),
                               max_results: int = Query(80, ge=1, le=200),
                               max_scanned: int = Query(30000, ge=1000, le=200000)):
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        tokens = _linked_search_tokens(q)
        if not tokens:
            return {"run_id": run_id, "query": q, "results": [], "omitted": False, "scanned": 0}
        results: list[dict[str, Any]] = []
        scanned_total = 0
        omitted_any = False
        for rec in _normalize_linked_folders(r.get("linked_folders")):
            raw = rec.get("path") or ""
            if not raw or rec.get("type") == "url" or _is_linked_url(raw):
                continue
            try:
                linked_path = _resolve_linked_path_for_run(r, raw)
            except HTTPException:
                continue
            label = rec.get("label") or linked_path.name
            if linked_path.is_file():
                entry = _folder_file_entry(linked_path.parent, linked_path, "")
                score = _linked_search_score(
                    tokens,
                    name=entry.get("name") or linked_path.name,
                    rel=entry.get("rel") or linked_path.name,
                    full_path=str(linked_path),
                    label=label,
                )
                if score is not None:
                    results.append({
                        "score": score,
                        "folder": str(linked_path),
                        "folder_label": label,
                        "folder_type": "file",
                        "entry": entry,
                    })
                continue
            remaining = max(0, max_results * 4 - len(results))
            if not remaining:
                break
            folder_results, omitted, scanned = _search_linked_folder_files(
                linked_path,
                label=label,
                tokens=tokens,
                max_results=remaining,
                max_scanned=max(1000, max_scanned - scanned_total),
            )
            scanned_total += scanned
            omitted_any = omitted_any or omitted
            for item in folder_results:
                results.append({
                    "score": item.get("score") or 0,
                    "folder": str(linked_path),
                    "folder_label": label,
                    "folder_type": "folder",
                    "entry": item.get("entry") or {},
                })
            if scanned_total >= max_scanned:
                omitted_any = True
                break
        results.sort(key=lambda item: (
            -float(item.get("score") or 0),
            str(item.get("folder_label") or ""),
            str((item.get("entry") or {}).get("rel") or ""),
        ))
        return {
            "run_id": run_id,
            "query": q,
            "results": results[:max_results],
            "omitted": omitted_any,
            "scanned": scanned_total,
        }

    @app.post("/api/sessions/{run_id}/folders")
    async def post_session_folder(run_id: str, request: Request):
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        body = await request.json()
        raw_path = str(body.get("path") or "")
        if _is_linked_url(raw_path):
            try:
                url = _coerce_linked_url(raw_path)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            label = (body.get("label") or _default_linked_url_label(url)).strip()
            changed = _persist_linked_url(r, url, label)
            return {
                "ok": True,
                "changed": changed,
                "task_metadata": {"path": "", "warning": "", "skipped": "url"},
                "warning": "",
                "folder": _linked_folder_summary({
                    "path": url,
                    "label": label,
                    "type": "url",
                    "created_at": _iso_now(),
                }),
            }
        linked_path = _resolve_linked_path(raw_path)
        item_type = "file" if linked_path.is_file() else "folder"
        label = (body.get("label") or linked_path.name).strip()
        changed = _persist_linked_path(r, linked_path, label, item_type)
        task_metadata = {"path": "", "warning": ""}
        if item_type == "folder" and _should_write_folder_task_metadata(linked_path):
            task_metadata = _update_folder_task_metadata(
                linked_path,
                _folder_task_metadata_context(r),
                label,
            )
        elif item_type == "folder":
            task_metadata = {
                "path": "",
                "warning": "",
                "skipped": "project/worktree folder",
            }
        return {
            "ok": True,
            "changed": changed,
            "task_metadata": task_metadata,
            "warning": task_metadata.get("warning", ""),
            "folder": _linked_folder_summary({
                "path": str(linked_path),
                "label": label,
                "type": item_type,
                "created_at": _iso_now(),
            }),
        }

    @app.delete("/api/sessions/{run_id}/folders")
    async def delete_session_folder(run_id: str, request: Request):
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        body = await request.json()
        raw_path = str(body.get("path") or "").strip()
        if not raw_path:
            raise HTTPException(400, "path is required")
        changed = _persist_unlink_folder(r, raw_path)
        return {"ok": True, "changed": changed}

    @app.get("/api/sessions/{run_id}/folders/children")
    def get_session_folder_children(run_id: str, folder: str = Query(...),
                                    rel: str = Query("")):
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        linked_folder = _resolve_linked_path_for_run(r, folder)
        if linked_folder.is_file():
            return {
                "folder": str(linked_folder),
                "path": str(linked_folder),
                "rel": "",
                "entries": [],
                "omitted": False,
            }
        logical_rel = _clean_folder_rel(rel)
        entries, omitted, target = _folder_direct_entries(
            linked_folder, logical_rel, include_self=False)
        return {
            "folder": str(linked_folder),
            "path": str(target),
            "rel": logical_rel,
            "entries": entries,
            "omitted": omitted,
        }

    @app.post("/api/sessions/{run_id}/folders/tag")
    async def post_session_folder_tag(run_id: str, request: Request):
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        body = await request.json()
        linked_folder = _resolve_linked_path_for_run(r, str(body.get("folder") or ""))
        logical_rel = _clean_folder_rel(str(body.get("rel") or ""))
        target = _resolve_folder_target(linked_folder, logical_rel)
        color = str(body.get("color") or "").strip().lower()
        meta = _set_finder_tag_color(target, color)
        return {
            "ok": True,
            "folder": str(linked_folder),
            "path": str(target),
            "rel": logical_rel,
            "meta": meta,
        }

    @app.get("/api/sessions/{run_id}/folders/file")
    def get_session_folder_file(run_id: str, folder: str = Query(...),
                                rel: str = Query(...)):
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        linked_folder = _resolve_linked_path_for_run(r, folder)
        logical_rel = _clean_folder_rel(rel)
        target = _resolve_folder_file(linked_folder, logical_rel)
        preview = _read_file_preview(target)
        return {
            "folder": str(linked_folder),
            "path": str(target),
            "rel": logical_rel,
            "name": target.name,
            "ext": target.suffix.lower(),
            **preview,
        }

    @app.get("/api/sessions/{run_id}/folders/file/raw")
    def get_session_folder_file_raw(run_id: str, folder: str = Query(...),
                                    rel: str = Query(...)):
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        linked_folder = _resolve_linked_path_for_run(r, folder)
        target = _resolve_folder_file(linked_folder, rel)
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(
            target,
            media_type=media_type,
            filename=target.name,
            headers={
                "Content-Disposition": f'inline; filename="{target.name}"',
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
            },
        )

    @app.post("/api/sessions/{run_id}/folders/file/open")
    async def post_open_session_folder_file(run_id: str, request: Request):
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        body = await request.json()
        linked_folder = _resolve_linked_path_for_run(
            r, str(body.get("folder") or body.get("path") or ""))
        target = _resolve_folder_file(linked_folder, str(body.get("rel") or ""))
        ok, err = _open_path_on_host(target)
        if not ok:
            raise HTTPException(500, err)
        return {"ok": True, "path": str(target)}

    @app.post("/api/sessions/{run_id}/folders/file/quicklook")
    async def post_quicklook_session_folder_file(run_id: str, request: Request):
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        body = await request.json()
        linked_folder = _resolve_linked_path_for_run(
            r, str(body.get("folder") or body.get("path") or ""))
        target = _resolve_folder_file(linked_folder, str(body.get("rel") or ""))
        ok, err = _quicklook_file_on_host(target)
        if not ok:
            raise HTTPException(500, err)
        return {"ok": True, "path": str(target)}

    @app.post("/api/sessions/{run_id}/folders/open")
    async def post_open_session_folder(run_id: str, request: Request):
        r = _lookup_run_light(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        body = await request.json()
        folder = _resolve_linked_path_for_run(r, str(body.get("path") or ""))
        ok, err = _open_path_on_host(folder)
        if not ok:
            raise HTTPException(500, err)
        return {"ok": True, "path": str(folder)}

    # -------- Archived projects (read-only view of `projects/`) ----------
    @app.get("/api/projects")
    def get_projects():
        """List project folders under `projects/` (populated by
        `orch organize`). Empty folders are hidden.

        Each entry: {name, count (.log files, recursive), latest_mtime}.
        Used by the sidebar "Projects" view. Strictly read-only."""
        return {"projects": _list_projects(projects_dir)}

    @app.get("/api/projects/{project}/logs")
    def get_project_logs(project: str):
        """List every `.log` under `projects/<project>/` (recursive, so
        archives that were split into subdirs still show up)."""
        pdir = projects_dir / project
        if not pdir.is_dir():
            raise HTTPException(404, f"project '{project}' not found")
        return {"project": project, "logs": _list_archived_logs(pdir)}

    @app.get("/api/projects/search")
    def search_projects(
        q: str = Query(..., min_length=1, max_length=200),
        limit: int = Query(200, ge=1, le=1000),
    ):
        """Grep archived logs for `q` (case-insensitive substring).

        Returns up to `limit` matches, each with a short snippet for
        the sidebar preview. Sorted newest-first by file mtime."""
        return _search_archived_logs(projects_dir, q, limit=limit)

    @app.get("/api/projects/{project}/logs/{rel:path}",
             response_class=PlainTextResponse)
    def get_project_log(project: str, rel: str):
        """Return the raw ANSI-coloured contents of one archived log.

        The caller (browser) strips ANSI for the viewer. We don't do it
        here because we may also want to re-use this bytes-faithful view
        later (e.g. piping into xterm.js). `rel` is the path relative to
        the project folder, so subdir-split archives work transparently."""
        p = _safe_project_log_path(projects_dir, project, rel)
        if p is None:
            raise HTTPException(404, "log not found")
        try:
            return p.read_text(errors="replace")
        except OSError as e:
            raise HTTPException(500, f"read failed: {e}")

    @app.post("/api/projects/{project}/logs/{rel:path}/clone")
    async def post_project_log_clone(project: str, rel: str,
                                     request: Request):
        """Start a new (background) tmux session and immediately prompt the
        agent with a structured summary request pointing at this archived
        log. The new session becomes a fresh outputs/<name>-<ts> entry; we
        don't try to resurrect the old one.

        Body (all optional):
          { "agent": "cursor"|"claude"|"codex", "model": "...",
            "cwd": "...", "label": "...", "mode": "background"|"iterm" }

        We default agent=cursor, mode=background because most users clone
        to "continue from this archived run" — iTerm spawn still works but
        can't deliver the first prompt without TCC-blessed automation.
        """
        p = _safe_project_log_path(projects_dir, project, rel)
        if p is None:
            raise HTTPException(404, "log not found")
        body = await request.json() if await request.body() else {}
        agent = (body.get("agent") or "cursor").strip()
        model = (body.get("model") or "").strip()
        effort = (body.get("effort") or "").strip().lower()
        terminal_theme = (
            _normalize_terminal_theme(str(body.get("terminal_theme") or ""))
            if "terminal_theme" in body else None
        )
        cwd = _resolve_default_cwd(body.get("cwd") or "")
        mode = (body.get("mode") or "background").strip()
        if mode not in ("iterm", "background"):
            raise HTTPException(400, "mode must be 'iterm' or 'background'")
        if mode == "iterm":
            raise HTTPException(400,
                "iterm mode cannot deliver first prompt; use background")

        parsed = _parse_archived_log_name(p.name) or {}
        old_task = parsed.get("task") or p.stem
        default_label = body.get("label")
        if not default_label:
            # "<project>-<old_task>-clone" keeps the origin visible in the
            # sidebar without being too verbose.
            default_label = f"{project}-{old_task}-clone"
        label = default_label.strip()

        result = _spawn_session(
            agent,
            model,
            label,
            cwd,
            mode,
            effort=effort,
            terminal_theme=terminal_theme,
        )
        new_tmux = (result.get("tmux_session") or "").strip()

        first_prompt = textwrap.dedent(f"""\
            我们来继续 `{project}` 这个项目里之前的一段对话。

            归档日志路径（请使用 `Read` 工具按需阅读，不必一次性读完）：
              {p}

            操作建议：
              1. 先用 Read 工具读日志 **开头 ~200 行** 和 **结尾 ~200 行**，
                 快速了解上次的任务目标和最终状态。
              2. 如果需要更多上下文，再定点读中间片段。
              3. 读完后给我一段简短的中文总结（上次做了什么、遗留了什么），
                 然后等我下一步指令。
        """).rstrip()

        # Schedule prompt delivery as a background task so this endpoint
        # returns immediately. The old blocking version (wait up to ~7s for
        # tmux + 1s grace + send) caused the frontend fetch to time out
        # even though the session was created successfully.
        if new_tmux:
            asyncio.create_task(
                _deliver_first_prompt(new_tmux, first_prompt))

        return {**result, "first_prompt": first_prompt,
                "archived_log": str(p), "project": project,
                "prompt_pending": bool(new_tmux)}

    @app.post("/api/sessions/{run_id}/send")
    async def post_send(run_id: str, request: Request):
        r = _lookup_run(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        session = r.get("tmux_session", "")
        if not session or not tmux_alive(session):
            raise HTTPException(409, "tmux session not alive")
        # Read raw body first to enforce a generous cap on paste size (4 MB)
        # without relying on framework defaults. tmux_send chunks internally.
        raw = await request.body()
        MAX_PASTE = 4 * 1024 * 1024
        if len(raw) > MAX_PASTE:
            raise HTTPException(413,
                f"paste too large ({len(raw)} bytes > {MAX_PASTE}); "
                "split into smaller pieces")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON body")
        text = body.get("text", "")
        enter = bool(body.get("enter", False))
        literal = bool(body.get("literal", True))
        ok, err = tmux_send(session, text, literal=literal, enter=enter)
        if not ok:
            # Log the real tmux stderr to the dashboard log so we can
            # diagnose intermittent 500s (the browser only sees the
            # status message).
            print(f"[send failed] run={run_id} session={session} "
                  f"chars={len(text)} err={err}", flush=True)
            raise HTTPException(500,
                f"tmux send-keys failed ({len(text)} chars): {err}")
        return {"ok": ok, "session": session, "bytes_sent": len(text)}

    @app.post("/api/sessions/{run_id}/label")
    async def post_label(run_id: str, request: Request):
        r = _lookup_run(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        body = await request.json()
        label = (body.get("label") or "").strip()

        if r["kind"] == "task":
            state_file = Path(r["run_dir"]) / "state.json"
            data = _safe_read_json(state_file) or {}
            if r["task"] not in data:
                raise HTTPException(404, "task not in state.json")
            data[r["task"]]["label"] = label
            state_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        elif r["kind"] == "run" and r.get("run_dir"):
            sess_file = Path(r["run_dir"]) / "session.json"
            if sess_file.exists():
                data = _safe_read_json(sess_file) or {}
                data["label"] = label
                sess_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            else:
                # Legacy run dir without session.json → store as orphan label
                # keyed by tmux session name.
                sess = r.get("tmux_session", "")
                if not sess:
                    raise HTTPException(400, "cannot persist label: no session.json and no tmux session")
                labels = _load_orphan_labels(outputs_dir)
                if label:
                    labels[sess] = label
                else:
                    labels.pop(sess, None)
                _save_orphan_labels(outputs_dir, labels)
        elif r["kind"] == "orphan":
            sess = r.get("tmux_session", "")
            if not sess:
                raise HTTPException(400, "orphan has no tmux session")
            labels = _load_orphan_labels(outputs_dir)
            if label:
                labels[sess] = label
            else:
                labels.pop(sess, None)
            _save_orphan_labels(outputs_dir, labels)
        else:
            raise HTTPException(400, f"kind={r['kind']} does not support labeling")

        # Invalidate the title cache so next poll recomputes auto_title if needed.
        _TITLE_CACHE.invalidate()
        return {"ok": True, "label": label}

    @app.post("/api/sessions/{run_id}/state")
    async def post_state(run_id: str, request: Request):
        r = _lookup_run(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        if r["kind"] != "task":
            raise HTTPException(400, "only full-mode tasks have state.json")
        body = await request.json()
        updates = {k: v for k, v in body.items() if k in ALLOWED_STATE_FIELDS}
        if not updates:
            raise HTTPException(400, f"no allowed fields; use {sorted(ALLOWED_STATE_FIELDS)}")
        ok = _update_state_json(Path(r["run_dir"]), r["task"], updates)
        if not ok:
            raise HTTPException(500, "failed to update state.json")
        return {"ok": True, "updates": updates}

    @app.post("/api/sessions/{run_id}/panel-state")
    async def post_panel_state(run_id: str, request: Request):
        r = _lookup_run(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        body = await request.json()
        panel_state = str(body.get("panel_state", "") or "").strip().lower()
        _persist_panel_state(r, panel_state)
        return {"ok": True, "panel_state": panel_state}

    @app.post("/api/sessions/{run_id}/terminal-theme")
    async def post_terminal_theme(run_id: str, request: Request):
        r = _lookup_run(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        body = await request.json()
        terminal_theme = _persist_terminal_theme(
            r, str(body.get("terminal_theme", "") or "")
        )
        session = r.get("tmux_session", "")
        if session and tmux_alive(session):
            # Rebuild the per-session ttyd if its theme changed. The next
            # iframe attach then receives the patched ttyd index HTML.
            ttyd.ensure(session, theme=terminal_theme)
        return {"ok": True, "terminal_theme": terminal_theme}

    def _spawn_session(
        agent: str, model: str, label: str, cwd: str, mode: str,
        resume_id: str = "",
        resume_meta: Optional[dict[str, str]] = None,
        effort: str = "",
        effort_mode: str = "",
        terminal_theme: Optional[str] = None,
    ) -> dict:
        """Build `run.sh` args, spawn the session, and return
        {"ok": True, "method": ..., "command": ..., "tmux_session": ...(optional)}.

        Shared by /api/create and /api/sessions/{id}/clone. Raises HTTPException
        on failure. In background mode we parse `Session: orch-...` from the
        run.sh stdout to return the new tmux session name so the caller can
        follow up (e.g. inject a first prompt for "clone w/ log").
        """
        model = _clean_cli_model_arg(model)
        orch_bin = str(SCRIPTS_DIR / "run.sh")
        runtime_env = os.environ.copy()
        runtime_env["ORCH_OUTPUTS_DIR"] = str(outputs_dir)
        projects_root = _dashboard_client_config()["projects_root"]
        if projects_root:
            runtime_env["ORCH_PROJECTS_ROOT"] = projects_root
        arg_list: list[str] = [orch_bin]
        parts = [
            "env",
            f"ORCH_OUTPUTS_DIR={shlex.quote(str(outputs_dir))}",
        ]
        if projects_root:
            parts.append(f"ORCH_PROJECTS_ROOT={shlex.quote(projects_root)}")
        parts.append(shlex.quote(orch_bin))
        agent_kind = _norm_agent(agent)
        safe_name = (
            re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-") or "session"
            if label else "interactive"
        )[:40]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base_run_name = f"{safe_name}-{stamp}"
        run_name = base_run_name
        for i in range(2, 1000):
            if not (outputs_dir / run_name).exists():
                break
            run_name = f"{base_run_name}-{i}"
        run_dir = outputs_dir / run_name
        run_id = f"{run_name}::{safe_name}"
        preallocated_meta: dict[str, str] = {}
        preallocation_error = ""

        if resume_id and resume_meta:
            if resume_meta.get("resume_id") == resume_id:
                preallocated_meta = dict(resume_meta)
            else:
                preallocation_error = "inherited resume metadata id mismatch"
        elif not resume_id:
            preallocated_meta, preallocation_error = _preallocate_native_resume_meta(agent_kind)

        if model:
            arg_list += ["--model", model]
            parts += ["--model", shlex.quote(model)]
        effort = (effort or "").strip().lower()
        effort_mode = (effort_mode or "").strip().lower()
        if effort and agent_kind in {"claude", "codex"}:
            arg_list += ["--effort", effort]
            parts += ["--effort", shlex.quote(effort)]
        if terminal_theme is not None:
            terminal_theme = _normalize_terminal_theme(terminal_theme)
            arg_list += ["--theme", terminal_theme]
            parts += ["--theme", shlex.quote(terminal_theme)]
        if label:
            arg_list += ["--label", label]
            parts += ["--label", shlex.quote(label)]
        arg_list += ["--run-name", run_name]
        parts += ["--run-name", shlex.quote(run_name)]
        if resume_id:
            arg_list += ["--resume-id", resume_id]
            parts += ["--resume-id", shlex.quote(resume_id)]
        elif preallocated_meta.get("resume_id"):
            native_id = preallocated_meta["resume_id"]
            native_source = preallocated_meta.get("resume_source", "")
            arg_list += ["--native-session-id", native_id]
            parts += ["--native-session-id", shlex.quote(native_id)]
            if native_source:
                arg_list += ["--native-resume-source", native_source]
                parts += ["--native-resume-source", shlex.quote(native_source)]
        if mode == "background":
            arg_list += ["--no-attach"]
        arg_list += [agent]
        parts += [shlex.quote(agent)]
        arg_list += [safe_name]
        parts += [shlex.quote(safe_name)]
        arg_list += [cwd]
        parts += [shlex.quote(cwd)]
        cmd = " ".join(parts)

        def finish_result(result: dict) -> dict:
            result.setdefault("run_dir", str(run_dir))
            result.setdefault("run_id", run_id)
            if preallocated_meta.get("resume_id"):
                result["preallocated_resume_id"] = preallocated_meta["resume_id"]
                result["preallocated_resume_source"] = preallocated_meta.get("resume_source", "")
            if preallocation_error:
                result["resume_preallocation_error"] = preallocation_error
            _schedule_native_resume_capture(
                outputs_dir,
                run_id,
                preallocated_meta=preallocated_meta,
                preallocation_error=preallocation_error,
            )
            if effort_mode:
                session_json = Path(result.get("run_dir") or run_dir) / "session.json"
                try:
                    for _ in range(25):
                        if session_json.exists():
                            break
                        time.sleep(0.1)
                    if session_json.exists():
                        data = _safe_read_json(session_json) or {}
                        data["effort_mode"] = effort_mode
                        _safe_write_json(session_json, data)
                    else:
                        result["effort_mode_warning"] = (
                            "session.json not ready for effort_mode metadata"
                        )
                except OSError as exc:
                    result["effort_mode_warning"] = (
                        f"failed to persist effort_mode metadata: {exc}"
                    )
            tmux_name = str(result.get("tmux_session") or "").strip()
            if agent_kind == "claude" and effort_mode == "ultracode":
                if tmux_name:
                    try:
                        asyncio.get_running_loop().create_task(
                            _deliver_first_prompt(
                                tmux_name,
                                "/effort ultracode",
                                ready_timeout=45.0,
                                grace=4.0,
                            )
                        )
                        result["effort_mode_pending"] = effort_mode
                    except RuntimeError:
                        result["effort_mode_warning"] = (
                            "could not schedule /effort ultracode"
                        )
                else:
                    result["effort_mode_warning"] = (
                        "iterm mode cannot auto-send /effort ultracode"
                    )
            return result

        if mode == "background":
            try:
                r = subprocess.run(
                    arg_list,
                    cwd=cwd if os.path.isdir(cwd) else None,
                    env=runtime_env,
                    capture_output=True, text=True, timeout=20,
                )
                if r.returncode != 0:
                    tail = (r.stderr or r.stdout or "").strip().splitlines()[-5:]
                    err_msg = "\n".join(tail) or f"exit {r.returncode}"
                    print(f"[_spawn_session bg] failed rc={r.returncode} "
                          f"err={err_msg!r}", file=sys.stderr, flush=True)
                    raise HTTPException(500,
                        f"run.sh --no-attach failed: {err_msg}")
                # run.sh prints "Session: orch-<name>-<pid>" early on stdout.
                tmux_name = ""
                stdout_run_dir = ""
                for line in (r.stdout or "").splitlines():
                    if line.startswith("Session:"):
                        tmux_name = line.split(":", 1)[1].strip()
                    elif line.startswith("Output:"):
                        stdout_run_dir = line.split(":", 1)[1].strip()
                print(f"[_spawn_session bg] spawned tmux={tmux_name!r} "
                      f"cmd={' '.join(arg_list)}", file=sys.stderr, flush=True)
                result = {"ok": True, "method": "background",
                          "command": cmd, "tmux_session": tmux_name,
                          "run_dir": stdout_run_dir or str(run_dir),
                          "run_id": run_id}
                return finish_result(result)
            except subprocess.TimeoutExpired:
                raise HTTPException(500, "run.sh took >20s to return")
            except FileNotFoundError as e:
                raise HTTPException(500, f"cannot execute run.sh: {e}")

        # mode == "iterm": requires Automation permission.
        ok, method, err = _spawn_in_new_terminal(cmd, cwd=cwd)
        if not ok:
            print(f"[_spawn_session iterm] spawn failed method={method!r} "
                  f"err={err!r} cmd={cmd!r}", file=sys.stderr, flush=True)
            raise HTTPException(500,
                f"failed to spawn terminal ({method}): {err}\n\n"
                f"If the dashboard runs as a LaunchAgent, macOS blocks it "
                f"from driving iTerm/Terminal. Use 'Start in background' "
                f"instead.")
        print(f"[_spawn_session iterm] spawned via {method}: {cmd}",
              file=sys.stderr, flush=True)
        result = {"ok": True, "method": method, "command": cmd,
                  "tmux_session": "", "run_dir": str(run_dir),
                  "run_id": run_id}
        return finish_result(result)

    def _resolve_default_cwd(raw: str) -> str:
        default_cwd = os.path.expanduser(
            _dashboard_client_config()["projects_root"]
        )
        if not os.path.isdir(default_cwd):
            default_cwd = os.path.expanduser("~")
        cwd = (raw or default_cwd).strip()
        if cwd:
            cwd = os.path.expanduser(os.path.expandvars(cwd))
        if not os.path.isdir(cwd):
            raise HTTPException(
                400, f"working directory does not exist: {cwd}"
            )
        return cwd

    @app.post("/api/create")
    async def post_create(request: Request):
        """Spawn a new `orch run` session.

        Body:
          agent: "cursor" | "claude" | "codex" (default cursor)
          model: optional model shortcut
          effort: optional Claude Code effort (low|medium|high|xhigh|max)
          terminal_theme: optional terminal palette
          label: optional session label
          cwd:   optional working directory (default: configured projects root)
          mode:  "iterm" (default) or "background"
        """
        body = await request.json()
        agent = (body.get("agent") or "cursor").strip()
        model = (body.get("model") or "").strip()
        effort = (body.get("effort") or "").strip().lower()
        terminal_theme = (
            _normalize_terminal_theme(str(body.get("terminal_theme") or ""))
            if "terminal_theme" in body else None
        )
        label = (body.get("label") or "").strip()
        cwd = _resolve_default_cwd(body.get("cwd") or "")
        mode = (body.get("mode") or "iterm").strip()

        if agent not in ("cursor", "claude", "agent", "codex"):
            raise HTTPException(400,
                "agent must be 'cursor', 'claude', or 'codex'")
        if mode not in ("iterm", "background"):
            raise HTTPException(400, "mode must be 'iterm' or 'background'")

        return _spawn_session(
            agent,
            model,
            label,
            cwd,
            mode,
            effort=effort,
            terminal_theme=terminal_theme,
        )

    @app.get("/api/resumable")
    def get_resumable(
        limit: int = Query(80, ge=1, le=300),
        q: str = Query("", max_length=240),
    ):
        """List ended sessions that have a recorded native resume id."""
        query_parts = [p for p in str(q or "").lower().split() if p]
        rows = []
        for r in _discover_runs(outputs_dir):
            if r.get("alive") or not r.get("resume_id"):
                continue
            row = {
                "run_id": r.get("run_id", ""),
                "display_name": r.get("display_name") or r.get("task") or r.get("run_name"),
                "task": r.get("task", ""),
                "run_name": r.get("run_name", ""),
                "agent": r.get("resume", {}).get("agent") or r.get("agent", ""),
                "model": r.get("model", ""),
                "effort": r.get("effort", ""),
                "effort_mode": r.get("effort_mode", ""),
                "cwd": r.get("cwd", ""),
                "started_at": r.get("started_at", ""),
                "status": r.get("status", ""),
                "resume_id": r.get("resume_id", ""),
                "resume_cmd": r.get("resume_cmd", ""),
                "resume_source": r.get("resume", {}).get("source", ""),
                "resume_recorded_at": r.get("resume", {}).get("recorded_at", ""),
            }
            if query_parts:
                haystack = " ".join(
                    str(row.get(key) or "")
                    for key in (
                        "run_id",
                        "display_name",
                        "task",
                        "run_name",
                        "agent",
                        "model",
                        "effort",
                        "effort_mode",
                        "cwd",
                        "status",
                        "started_at",
                        "resume_id",
                        "resume_cmd",
                        "resume_source",
                        "resume_recorded_at",
                    )
                ).lower()
                if not all(part in haystack for part in query_parts):
                    continue
            rows.append(row)
        rows.sort(key=lambda x: (
            x.get("resume_recorded_at") or x.get("started_at") or ""
        ), reverse=True)
        return {"sessions": rows[:limit]}

    @app.get("/api/active-snapshot")
    def get_active_snapshot():
        snapshot = _load_active_snapshot(outputs_dir)
        sessions = snapshot.get("sessions") if isinstance(snapshot.get("sessions"), list) else []
        return {
            "ok": bool(snapshot),
            "snapshot_path": str(_active_snapshot_path(outputs_dir)),
            "saved_at": snapshot.get("saved_at", ""),
            "layout": snapshot.get("layout", ""),
            "session_count": len(sessions),
            "sessions": sessions,
            "skipped": snapshot.get("skipped", []),
        }

    @app.post("/api/active-snapshot/save")
    async def save_active_snapshot(request: Request):
        """Save all currently alive sessions that have native resume metadata."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        layout_name = str(body.get("layout") or "")
        raw_slots = body.get("slots") if isinstance(body.get("slots"), list) else []
        slot_ids = [str(x) if x else "" for x in raw_slots]
        try:
            path, snapshot = _save_active_snapshot(
                outputs_dir,
                layout_name=layout_name,
                slot_ids=slot_ids,
                saved_by="manual",
            )
        except OSError as exc:
            raise HTTPException(500, f"failed to write active snapshot: {exc}")
        sessions_out = snapshot.get("sessions") if isinstance(snapshot.get("sessions"), list) else []
        skipped = snapshot.get("skipped") if isinstance(snapshot.get("skipped"), list) else []
        return {
            "ok": True,
            "snapshot_path": str(path),
            "saved_at": snapshot["saved_at"],
            "saved_count": len(sessions_out),
            "skipped_count": len(skipped),
            "sessions": sessions_out,
            "skipped": skipped,
        }

    @app.post("/api/active-snapshot/restore")
    async def restore_active_snapshot(request: Request):
        """Restore the last saved active-session snapshot in background tmux."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        mode = (body.get("mode") or "background").strip()
        if mode not in {"background", "iterm"}:
            raise HTTPException(400, "mode must be 'background' or 'iterm'")
        skip_existing = body.get("skip_existing", True)
        snapshot = _load_active_snapshot(outputs_dir)
        entries = snapshot.get("sessions") if isinstance(snapshot.get("sessions"), list) else []
        if not entries:
            raise HTTPException(404, "no saved active-session snapshot")

        active_resume_ids = {
            r.get("resume_id", "")
            for r in _discover_runs(outputs_dir)
            if r.get("alive") and r.get("resume_id")
        }
        restored: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        source_to_new: dict[str, str] = {}

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            source_run_id = str(entry.get("source_run_id") or "")
            resume_id = str(entry.get("resume_id") or "").strip()
            agent = _norm_agent(entry.get("agent", ""))
            if not resume_id:
                skipped.append({"source_run_id": source_run_id, "reason": "missing resume id"})
                continue
            if agent not in {"cursor", "claude", "codex"}:
                skipped.append({"source_run_id": source_run_id, "reason": f"unsupported agent: {agent or 'unknown'}"})
                continue
            if skip_existing and resume_id in active_resume_ids:
                skipped.append({"source_run_id": source_run_id, "reason": "already running"})
                continue

            src = {
                "run_id": source_run_id,
                "run_dir": entry.get("source_run_dir", ""),
                "kind": entry.get("kind", "run"),
                "task": entry.get("task", ""),
                "run_name": entry.get("run_name", ""),
                "display_name": entry.get("display_name", ""),
                "label": entry.get("label", ""),
                "agent": agent,
                "model": entry.get("model", ""),
                "effort": entry.get("effort", ""),
                "effort_mode": entry.get("effort_mode", ""),
                "cwd": entry.get("cwd", ""),
                "resume_id": resume_id,
                "resume_cmd": entry.get("resume_cmd", ""),
                "resume": {
                    "agent": agent,
                    "id": resume_id,
                    "cmd": entry.get("resume_cmd", ""),
                    "source": entry.get("resume_source", "active-snapshot"),
                    "recorded_at": entry.get("resume_recorded_at", ""),
                    "source_path": entry.get("source_metadata_path", ""),
                    "confidence": "exact",
                },
                "linked_folders": _normalize_linked_folders(entry.get("linked_folders")),
            }
            src = _run_with_native_model_effort(src)
            label = str(
                entry.get("label")
                or entry.get("display_name")
                or entry.get("task")
                or "restored"
            )[:80]
            cwd = _resolve_default_cwd(entry.get("cwd") or "")
            model = str(src.get("model") or "").strip()
            if model in _MODEL_DEFAULTS:
                model = ""
            effort = str(src.get("effort") or "").strip().lower()
            effort_mode = str(src.get("effort_mode") or "").strip().lower()
            terminal_theme = (
                _normalize_terminal_theme(str(entry.get("terminal_theme") or ""))
                if "terminal_theme" in entry else None
            )
            meta = _build_resume_meta(
                agent, resume_id, "active-snapshot",
                str(entry.get("source_metadata_path") or ""),
                confidence="exact",
            )
            if source_run_id:
                meta["resumed_from_run_id"] = source_run_id
            if entry.get("source_run_dir"):
                meta["resumed_from_run_dir"] = str(entry.get("source_run_dir") or "")
            if entry.get("resume_source"):
                meta["resumed_from_resume_source"] = str(entry.get("resume_source") or "")

            result = _spawn_session(agent, model, label, cwd, mode,
                                    resume_id=resume_id, resume_meta=meta,
                                    effort=effort, effort_mode=effort_mode,
                                    terminal_theme=terminal_theme)
            active_resume_ids.add(resume_id)
            linked_copy = _copy_linked_folders_to_spawned_run(
                outputs_dir, src, result, exclude_run_id=source_run_id,
                resume_id=resume_id, label=label)
            ui_copy = _copy_snapshot_ui_metadata_to_spawned_run(result, entry)
            out = {
                **result,
                "source_run_id": source_run_id,
                "resume_id": resume_id,
                "model": model,
                "effort": effort,
                "effort_mode": effort_mode,
                "linked_folders_copied": linked_copy.get("copied", 0),
                "linked_folders_warning": linked_copy.get("warning", ""),
                "ui_metadata_copied": ui_copy.get("copied", False),
                "panel_state": ui_copy.get("panel_state", ""),
                "terminal_theme": ui_copy.get("terminal_theme", ""),
                "ui_metadata_warning": ui_copy.get("warning", ""),
            }
            restored.append(out)
            if source_run_id and result.get("run_id"):
                source_to_new[source_run_id] = result["run_id"]

        restored_slots = [
            source_to_new.get(str(run_id or ""), "")
            for run_id in (snapshot.get("slots") or [])
        ]
        for item in restored:
            rid = item.get("run_id", "")
            if rid and rid not in restored_slots:
                restored_slots.append(rid)
        return {
            "ok": True,
            "saved_at": snapshot.get("saved_at", ""),
            "layout": snapshot.get("layout", ""),
            "restored_count": len(restored),
            "skipped_count": len(skipped),
            "restored": restored,
            "skipped": skipped,
            "slots": restored_slots,
        }

    @app.post("/api/resume")
    async def post_resume(request: Request):
        """Start a new background/iTerm session using a saved native resume id."""
        body = await request.json()
        run_id = (body.get("run_id") or "").strip()
        if not run_id:
            raise HTTPException(400, "run_id is required")
        src = _lookup_run(outputs_dir, run_id)
        if not src:
            raise HTTPException(404, "source run not found")
        if src.get("alive"):
            raise HTTPException(409, "source session is still running")
        src = _run_with_native_model_effort(src)
        resume_id = (src.get("resume_id") or "").strip()
        if not resume_id:
            raise HTTPException(409, "source session has no resume id")

        mode = (body.get("mode") or "background").strip()
        if mode not in ("iterm", "background"):
            raise HTTPException(400, "mode must be 'iterm' or 'background'")
        agent = (
            (src.get("resume") or {}).get("agent")
            or src.get("agent")
            or "cursor"
        )
        agent = "cursor" if agent == "agent" else agent
        if agent not in ("cursor", "claude", "codex"):
            raise HTTPException(400,
                "resume source agent must be 'cursor', 'claude', or 'codex'")
        model = src.get("model") or ""
        if model == "default":
            model = ""
        effort = str(src.get("effort") or "").strip().lower()
        effort_mode = str(src.get("effort_mode") or "").strip().lower()
        terminal_theme = (
            _normalize_terminal_theme(str(body.get("terminal_theme") or ""))
            if "terminal_theme" in body
            else (
                _normalize_terminal_theme(str(src.get("terminal_theme") or ""))
                if "terminal_theme" in src else None
            )
        )
        cwd = _resolve_default_cwd(body.get("cwd") or src.get("cwd") or "")
        label = (body.get("label") or "").strip()
        if not label:
            base = src.get("display_name") or src.get("task") or "resumed"
            label = f"{base} (resume)"[:80]
        inherited_resume = _build_inherited_resume_meta(src, agent, resume_id)
        result = _spawn_session(agent, model, label, cwd, mode,
                                resume_id=resume_id,
                                resume_meta=inherited_resume,
                                effort=effort,
                                effort_mode=effort_mode,
                                terminal_theme=terminal_theme)
        linked_copy = _copy_linked_folders_to_spawned_run(
            outputs_dir, src, result, exclude_run_id=run_id,
            resume_id=resume_id, label=label)
        return {
            **result,
            "resumed_from": run_id,
            "resume_id": resume_id,
            "linked_folders_copied": linked_copy.get("copied", 0),
            "linked_folders_run_dir": linked_copy.get("run_dir", ""),
            "linked_folders_warning": linked_copy.get("warning", ""),
        }

    @app.post("/api/organize")
    async def post_organize(request: Request):
        """Spawn `bash organize.sh` in the background.

        Body (all optional):
          stale: duration string ("3h", "1d", "30m", ...). Empty = all
                 inactive sessions in outputs/ that aren't already archived.
          agent: "cursor" (default), "claude", or "codex".

        Returns immediately with the run_id of the organize session
        (e.g. "organize-20260420-121500") so the UI can poll the Active
        list for it. The actual classification runs asynchronously via
        the script's own tmux session (orch-organize-<pid>), which is
        itself discoverable through /api/sessions.
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        stale = (body.get("stale") or "").strip()
        agent = (body.get("agent") or "cursor").strip()
        if agent not in ("cursor", "claude", "agent", "codex"):
            raise HTTPException(400,
                "agent must be 'cursor', 'claude', or 'codex'")

        script = SCRIPTS_DIR / "organize.sh"
        if not script.exists():
            raise HTTPException(500, f"organize.sh not found at {script}")

        # Predict the run_id the script will create. organize.sh uses
        # `RUN_NAME="organize-$(date +%Y%m%d-%H%M%S)"` — we compute the
        # same stamp here (≤1s skew is fine for the UI to latch onto).
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run_id = f"organize-{stamp}"

        cmd = ["bash", str(script)]
        if stale:
            cmd += ["--stale", stale]
        cmd += [agent]

        # Fire-and-forget: organize.sh has its own 30s+ "wait for agent
        # ready" loop; we must not block the HTTP reply on it. Use
        # start_new_session so it survives if this uvicorn reloads.
        try:
            subprocess.Popen(
                cmd,
                cwd=str(PROJECT_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            raise HTTPException(500, f"cannot execute organize.sh: {e}")
        except OSError as e:
            raise HTTPException(500, f"failed to spawn organize.sh: {e}")

        print(f"[/api/organize] spawned {' '.join(cmd)} "
              f"run_id={run_id!r}", file=sys.stderr, flush=True)
        return {"ok": True, "run_id": run_id, "stale": stale, "agent": agent}

    @app.post("/api/sessions/{run_id}/clone")
    async def post_clone(run_id: str, request: Request):
        """Start a new session copying the old one's agent / model / cwd / label.

        Body:
          with_log: bool    — if true, send a first prompt to the new agent
                              pointing it at the old log file so it can pick
                              up where the previous session left off.
          mode:     str     — "background" (default, TCC-safe) or "iterm".

        Returns {ok, method, command, tmux_session, first_prompt?}.
        first_prompt is the exact text we sent to the new tmux session (so
        the UI can display it to the user).
        """
        src = _lookup_run(outputs_dir, run_id)
        if not src:
            raise HTTPException(404, "source run not found")
        src = _run_with_native_model_effort(src)

        body = await request.json()
        with_log = bool(body.get("with_log", False))
        mode = (body.get("mode") or "background").strip()

        agent = src.get("agent") or "cursor"
        model = src.get("model") or ""
        if model == "default":
            model = ""
        effort = str(src.get("effort") or "").strip().lower()
        effort_mode = str(src.get("effort_mode") or "").strip().lower()
        terminal_theme = (
            _normalize_terminal_theme(str(src.get("terminal_theme") or ""))
            if "terminal_theme" in src else None
        )
        cwd = src.get("cwd") or _resolve_default_cwd("")
        # Preserve the user's label so the cloned session is recognizable,
        # but tag it as a clone so there's no confusion in the sidebar.
        orig_label = (src.get("label") or "").strip()
        base = orig_label or (src.get("task") or "clone")
        new_label = f"{base} (clone)"[:80]

        # Resolve absolute log path of the *source* — what we'll tell the new
        # agent to read. If the source has no log on disk, silently drop the
        # with_log request (nothing useful to inherit).
        log_abs = ""
        run_dir = src.get("run_dir") or ""
        log_file = src.get("log_file") or ""
        if run_dir and log_file:
            p = Path(run_dir) / log_file
            if p.exists() and p.stat().st_size > 0:
                log_abs = str(p.resolve())
        if with_log and not log_abs:
            raise HTTPException(409,
                "source session has no log on disk — use plain Clone instead")

        result = _spawn_session(agent, model, new_label, cwd, mode,
                                effort=effort, effort_mode=effort_mode,
                                terminal_theme=terminal_theme)
        linked_copy = _copy_linked_folders_to_spawned_run(
            outputs_dir, src, result, exclude_run_id=run_id, label=new_label)
        new_tmux = (result.get("tmux_session") or "").strip()

        first_prompt = ""
        if with_log:
            if not new_tmux:
                # iTerm mode can't thread a prompt back; require background.
                raise HTTPException(409,
                    "Clone w/ log requires background mode (no iTerm window).")

            # Compose the prompt. We hand the agent the log path + context and
            # ask it to self-bootstrap by reading only what it needs.
            first_prompt = textwrap.dedent(f"""\
                You are the successor to a previous agent session that has
                ended. Continue its work. Do NOT re-execute old steps
                blindly — first understand what was done, then plan next.

                Previous session context:
                - Working directory: {cwd}
                - Agent / model:     {agent} / {model or 'default'}
                - Label:             {orig_label or '(none)'}
                - Started at:        {src.get('started_at', '?')}
                - Log file (UTF-8):  {log_abs}

                Please do these steps in order:
                1. Read the LAST ~300 lines of the log to learn what the
                   previous session was doing when it ended.
                2. Read the FIRST ~80 lines to learn the original task.
                3. If needed, grep / head specific sections for detail.
                   Do NOT read the entire log — it may be large.
                4. Summarize to me in 5-10 lines: what was the goal, what
                   got done, what is still pending.
                5. Propose the next concrete step and wait for my OK before
                   executing it.
                """)

            # Deliver the prompt as a background task so this endpoint
            # returns immediately. Previously we blocked up to ~17s waiting
            # for the new tmux session to become addressable + a grace
            # period, which made the frontend fetch time out even though
            # the session had been created successfully.
            asyncio.create_task(
                _deliver_first_prompt(new_tmux, first_prompt))

        return {
            **result,
            "first_prompt": first_prompt,
            "prompt_pending": bool(new_tmux and with_log),
            "linked_folders_copied": linked_copy.get("copied", 0),
            "linked_folders_run_dir": linked_copy.get("run_dir", ""),
            "linked_folders_warning": linked_copy.get("warning", ""),
        }

    @app.post("/api/sessions/{run_id}/stop")
    def post_stop(run_id: str, timeout: float = Query(12.0, ge=2.0, le=60.0)):
        r = _lookup_run(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        session = r.get("tmux_session", "")
        if not session:
            raise HTTPException(409, "no tmux session associated")

        before_text = tmux_capture(session) if tmux_alive(session) else ""
        resume = _discover_resume_metadata(r, before_text)
        if resume:
            _persist_resume_metadata(r, resume)

        stop_result = _graceful_stop_agent(
            session, r.get("agent", ""), timeout_s=timeout)
        after_text = tmux_capture(session) if tmux_alive(session) else before_text
        resume = _discover_resume_metadata(r, after_text) or resume

        persisted = False
        if resume:
            persisted = _persist_resume_metadata(
                r, resume, status="stopped" if stop_result.get("ok") else None)

        wrapper_killed = False
        if stop_result.get("ok") and tmux_alive(session):
            wrapper_killed = tmux_kill(session)
            kill_shadow_session(session)

        return {
            "ok": bool(stop_result.get("ok")),
            "session": session,
            "graceful_exited": bool(stop_result.get("ok")),
            "reason": stop_result.get("reason", ""),
            "needs_force": not bool(stop_result.get("ok")),
            "wrapper_killed": wrapper_killed,
            "resume": {
                "agent": resume.get("resume_agent", ""),
                "id": resume.get("resume_id", ""),
                "cmd": resume.get("resume_cmd", ""),
                "source": resume.get("resume_source", ""),
                "recorded_at": resume.get("resume_recorded_at", ""),
            } if resume else {},
            "resume_persisted": persisted,
        }

    @app.post("/api/sessions/{run_id}/kill")
    def post_kill(run_id: str):
        r = _lookup_run(outputs_dir, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        session = r.get("tmux_session", "")
        if not session:
            raise HTTPException(409, "no tmux session associated")
        before_text = tmux_capture(session) if tmux_alive(session) else ""
        resume = _discover_resume_metadata(r, before_text)
        if resume:
            _persist_resume_metadata(r, resume)
        ok = tmux_kill(session)
        kill_shadow_session(session)
        return {"ok": ok, "session": session, "forced": True}

    # Static UI
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        def index():
            idx = STATIC_DIR / "index.html"
            if idx.exists():
                # Disable caching for index.html so iteration on the UI
                # (we edit static/index.html live while the server is
                # running) actually shows up on the next reload without
                # needing Cmd-Shift-R + clear-cache gymnastics.
                return FileResponse(
                    str(idx),
                    headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0",
                    },
                )
            return PlainTextResponse("Dashboard UI missing (static/index.html).", 500)

        @app.get("/favicon.ico")
        def favicon():
            ico = STATIC_DIR / "favicon.ico"
            if ico.exists():
                return FileResponse(str(ico))
            return PlainTextResponse("", 204)
    else:
        @app.get("/")
        def index_missing():
            return PlainTextResponse(
                "static/ directory not found. Expected static/index.html next to dashboard.py.",
                500,
            )

    return app


def _ensure_self_signed_cert(cert_dir: Path) -> tuple[Path, Path]:
    """Generate (once) and return (cert, key) paths for self-signed HTTPS.

    We create them on demand in `cert_dir` so `--https` works out of the
    box. Browsers will warn the first time you visit — accept the warning
    once and from then on `navigator.clipboard` works (secure context).
    """
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert = cert_dir / "dashboard.crt"
    key = cert_dir / "dashboard.key"
    if cert.exists() and key.exists():
        return cert, key
    if not shutil.which("openssl"):
        raise SystemExit(
            "openssl not found on PATH — can't generate a self-signed cert.\n"
            "Install it (`brew install openssl`), or pass --certfile/--keyfile."
        )
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "3650",
            "-subj", "/CN=orch-dashboard",
            "-addext", "subjectAltName=DNS:localhost,DNS:*.local,IP:127.0.0.1",
        ],
        check=True, capture_output=True,
    )
    return cert, key


def main():
    ap = argparse.ArgumentParser(description="Agent Orchestrator Dashboard")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default: 127.0.0.1). Non-loopback "
                         "addresses require --token or $ORCH_DASHBOARD_TOKEN.")
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--outputs", default=str(DEFAULT_OUTPUTS_DIR),
                    help="path to orchestrator outputs/ directory")
    ap.add_argument("--token", default=dashboard_token(),
                    help="shared secret; reads $ORCH_DASHBOARD_TOKEN or the "
                         "local token cache")
    ap.add_argument("--ttyd", action="store_true",
                    help="enable ttyd integration for full-pty interaction (requires `ttyd` on PATH)")
    ap.add_argument("--ttyd-host", default="",
                    help="hostname browsers should use to reach ttyd (default: same as --host, "
                         "or 127.0.0.1 if --host is 0.0.0.0). Set to your tunnel/LAN hostname.")
    ap.add_argument("--https", action="store_true",
                    help="serve over HTTPS with an auto-generated self-signed cert "
                         "(required for browser clipboard API over LAN/tunnel)")
    ap.add_argument("--certfile", default="",
                    help="path to TLS cert (implies --https; pair with --keyfile)")
    ap.add_argument("--keyfile", default="",
                    help="path to TLS key (implies --https; pair with --certfile)")
    ap.add_argument("--publish-icloud", action="store_true",
                    help="write current dashboard URL to ~/iCloud Drive/orch-dashboard.txt "
                         "so your phone can always find the latest address")
    ap.add_argument("--reload", action="store_true", help="dev auto-reload")
    args = ap.parse_args()

    try:
        require_dashboard_auth(args.host, args.token)
    except ValueError as exc:
        raise SystemExit(str(exc))

    outputs_dir = Path(args.outputs).resolve()
    outputs_dir.mkdir(parents=True, exist_ok=True)

    try:
        import uvicorn
    except ImportError:
        raise SystemExit("uvicorn not installed. Run: pip install -r requirements.txt")

    # HTTPS: either user-provided cert/key, or auto self-signed when --https.
    ssl_kwargs: dict[str, Any] = {}
    use_https = args.https or bool(args.certfile or args.keyfile)
    cert_path = key_path = None
    if use_https:
        if args.certfile and args.keyfile:
            cert_path, key_path = Path(args.certfile), Path(args.keyfile)
        else:
            cert_path, key_path = _ensure_self_signed_cert(
                outputs_dir.parent / ".dashboard-certs"
            )
        ssl_kwargs = {"ssl_certfile": str(cert_path), "ssl_keyfile": str(key_path)}
    scheme = "https" if use_https else "http"

    ttyd_host = args.ttyd_host or ("127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host)
    app = create_app(outputs_dir, token=args.token,
                     ttyd_enabled=args.ttyd, ttyd_host=ttyd_host,
                     bind_host=args.host, port=args.port, scheme=scheme,
                     publish_icloud=args.publish_icloud)

    print(f"Outputs:  {outputs_dir}")
    print(f"Listen:   {scheme}://{args.host}:{args.port}")
    if use_https:
        print(f"TLS:      cert={cert_path}")
        print(f"          key ={key_path}")
        print("          (self-signed — accept the browser warning once)")
    if args.token:
        print("Token:    configured (hidden; use the URL helper for browser access)")
    else:
        print("Token:    (none — open access; pass --token or $ORCH_DASHBOARD_TOKEN to lock)")
    if args.ttyd:
        print(f"ttyd:     enabled; browsers will connect via the dashboard's same-origin proxy")
    best = pick_best_ip(args.host)
    if best:
        print(f"Phone:    {build_access_url(best, args.port, scheme, None)}")
    if args.publish_icloud and getattr(app.state, "icloud_file", None):
        print(f"iCloud:   {app.state.icloud_file}")
    print()

    try:
        uvicorn.run(app, host=args.host, port=args.port, reload=args.reload,
                    log_level="info", access_log=False, **ssl_kwargs)
    finally:
        app.state.ttyd.stop_all()


if __name__ == "__main__":
    main()
