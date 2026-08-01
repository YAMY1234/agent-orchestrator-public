#!/usr/bin/env python3
"""Shared safety helpers for output session cleanup."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable, Iterator


ARCHIVE_TS_RE = re.compile(r"^(\d{8}-\d{6})")
ORCH_SESSION_RE = re.compile(r"^orch-(?P<task>.+)-(?P<suffix>\d+)$")
OUTPUT_DIR_RE = re.compile(r"^(?P<task>.+)-(?P<ts>\d{8}-\d{6})$")


def probe_live_orch_sessions(timeout: float = 3) -> tuple[set[str], bool]:
    """Return live non-shadow orch tmux sessions and whether probing succeeded."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set(), False

    if result.returncode != 0:
        return set(), True

    sessions = {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith("orch-") and not line.strip().endswith("-web")
    }
    return sessions, True


def list_live_orch_sessions(timeout: float = 3) -> set[str]:
    """Return live non-shadow Agent Orchestrator tmux session names."""
    sessions, _ok = probe_live_orch_sessions(timeout=timeout)
    return sessions


def get_archived_timestamps(projects_dir: str | os.PathLike[str]) -> set[str]:
    """Return YYYYMMDD-HHMMSS timestamps already archived as .log files."""
    projects_path = Path(projects_dir)
    ts_set: set[str] = set()
    if not projects_path.is_dir():
        return ts_set

    for root, _dirs, files in os.walk(projects_path):
        for filename in files:
            if not filename.endswith(".log"):
                continue
            match = ARCHIVE_TS_RE.match(filename)
            if match:
                ts_set.add(match.group(1))
    return ts_set


def output_dir_timestamp(dirname: str) -> str | None:
    match = re.search(r"(\d{8}-\d{6})", dirname)
    return match.group(1) if match else None


def output_dir_task(dirname: str) -> str | None:
    match = OUTPUT_DIR_RE.match(dirname)
    return match.group("task") if match else None


def orch_session_task(session: str) -> str | None:
    match = ORCH_SESSION_RE.match(session)
    return match.group("task") if match else None


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _iter_key_values(obj: object, key: str, depth: int = 0) -> Iterator[object]:
    if depth > 4:
        return
    if isinstance(obj, dict):
        if key in obj:
            yield obj[key]
        for value in obj.values():
            yield from _iter_key_values(value, key, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            yield from _iter_key_values(value, key, depth + 1)


def _json_tmux_sessions(obj: object) -> set[str]:
    return {
        value
        for value in _iter_key_values(obj, "tmux_session")
        if isinstance(value, str) and value
    }


def _json_pid_suffixes(obj: object) -> set[str]:
    suffixes: set[str] = set()
    for value in _iter_key_values(obj, "pid"):
        if isinstance(value, int):
            suffixes.add(str(value))
        elif isinstance(value, str) and value.isdigit():
            suffixes.add(value)
    return suffixes


def get_active_dirs(
    outputs_dir: str | os.PathLike[str],
    live_sessions: Iterable[str] | None = None,
    *,
    prefix_fallback: bool = True,
) -> set[str]:
    """Return output dir names that still have a live tmux session.

    Metadata is authoritative when present: ``session.json`` and ``state.json``
    must point at a live tmux session. Prefix matching is only a legacy fallback
    for directories with no usable metadata, so a new live task does not protect
    every old directory with the same task prefix.
    """
    outputs_path = Path(outputs_dir)
    if not outputs_path.is_dir():
        return set()

    live = set(live_sessions) if live_sessions is not None else list_live_orch_sessions()
    live = {s for s in live if s.startswith("orch-") and not s.endswith("-web")}
    if not live:
        return set()

    live_suffixes = {
        match.group("suffix")
        for session in live
        if (match := ORCH_SESSION_RE.match(session))
    }
    live_tasks = {
        task
        for session in live
        if (task := orch_session_task(session))
    }

    active: set[str] = set()
    metadata_seen: set[str] = set()
    entries = [entry for entry in outputs_path.iterdir() if entry.is_dir()]

    for entry in entries:
        tmux_sessions: set[str] = set()
        pid_suffixes: set[str] = set()
        for filename in ("session.json", "state.json", "marker.json"):
            data = _read_json(entry / filename)
            if data is None:
                continue
            tmux_sessions.update(_json_tmux_sessions(data))
            pid_suffixes.update(_json_pid_suffixes(data))

        if tmux_sessions or pid_suffixes:
            metadata_seen.add(entry.name)
        if tmux_sessions & live:
            active.add(entry.name)
            continue
        if pid_suffixes & live_suffixes:
            active.add(entry.name)

    if prefix_fallback:
        for entry in entries:
            if entry.name in metadata_seen:
                continue
            task = output_dir_task(entry.name)
            if task and task in live_tasks:
                active.add(entry.name)

    return active


def is_empty_session_dir(path: str | os.PathLike[str], max_log_bytes: int = 512) -> bool:
    """Return True if the run has no logs or all direct log files are tiny."""
    log_dir = Path(path) / "logs"
    if not log_dir.is_dir():
        return True

    logs = [entry for entry in log_dir.iterdir() if entry.is_file()]
    if not logs:
        return True

    for logfile in logs:
        try:
            if logfile.stat().st_size > max_log_bytes:
                return False
        except OSError:
            return False
    return True
