"""Native agent lifecycle tracking for the dashboard.

Codex writes structured turn events to its rollout JSONL files. Claude Code
offers lifecycle hooks. This module consumes both signals without touching the
terminal renderer, so a quiet tool call is not mistaken for an idle agent.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


_WORKING_STATES = {"working", "background_working"}
_WAITING_STATES = {"waiting_user", "needs_input", "ended"}
_CODEX_BOUNDARY_EVENTS = {
    "task_started": ("working", "Turn started"),
    "turn_started": ("working", "Turn started"),
    "task_complete": ("waiting_user", "Turn complete"),
    "turn_complete": ("waiting_user", "Turn complete"),
    "turn_completed": ("waiting_user", "Turn complete"),
    "turn_aborted": ("waiting_user", "Turn interrupted"),
}
_CLAUDE_PERMISSION_POLICIES = {"observe", "orchestrator"}


def _state_home() -> Path:
    configured = os.environ.get("XDG_STATE_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state"


def activity_database_path() -> Path:
    configured = os.environ.get("ORCH_NATIVE_ACTIVITY_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _state_home() / "agent-orchestrator" / "native-activity.sqlite3"


def _event_time(value: Any, fallback: float | None = None) -> float:
    text = str(value or "").strip()
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return float(fallback if fallback is not None else time.time())


class NativeActivityStore:
    """Small cross-process store written by Claude hooks and read by the UI."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or activity_database_path()).expanduser()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=2000")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS session_activity (
                agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                state TEXT NOT NULL,
                since REAL NOT NULL,
                updated_at REAL NOT NULL,
                event TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (agent, session_id)
            )"""
        )
        return conn

    def record(
        self, *, agent: str, session_id: str, state: str,
        event: str = "", reason: str = "", source: str = "",
        occurred_at: float | None = None,
    ) -> dict[str, Any]:
        agent = str(agent or "").strip().lower()
        session_id = str(session_id or "").strip().lower()
        state = str(state or "").strip().lower()
        if not agent or not session_id or state not in (_WORKING_STATES | _WAITING_STATES):
            raise ValueError("agent, session_id, and a supported state are required")
        now = float(occurred_at if occurred_at is not None else time.time())
        with closing(self._connect()) as conn, conn:
            existing = conn.execute(
                "SELECT state, since FROM session_activity "
                "WHERE agent = ? AND session_id = ?",
                (agent, session_id),
            ).fetchone()
            since = (
                float(existing["since"])
                if existing is not None and existing["state"] == state
                else now
            )
            conn.execute(
                """INSERT INTO session_activity
                   (agent, session_id, state, since, updated_at, event, reason, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(agent, session_id) DO UPDATE SET
                     state = excluded.state,
                     since = excluded.since,
                     updated_at = excluded.updated_at,
                     event = excluded.event,
                     reason = excluded.reason,
                     source = excluded.source""",
                (agent, session_id, state, since, now, event, reason, source),
            )
        return {
            "agent": agent,
            "session_id": session_id,
            "state": state,
            "since": since,
            "updated_at": now,
            "event": event,
            "reason": reason,
            "source": source,
        }

    def read_all(self) -> list[dict[str, Any]]:
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT agent, session_id, state, since, updated_at, "
                    "event, reason, source FROM session_activity"
                ).fetchall()
        except (OSError, sqlite3.Error):
            return []
        return [dict(row) for row in rows]


def _claude_event_state(payload: dict[str, Any]) -> tuple[str, str] | None:
    event = str(payload.get("hook_event_name") or payload.get("event") or "").strip()
    if event == "UserPromptSubmit":
        return "working", "Prompt submitted"
    if event == "Stop":
        return "waiting_user", "Turn complete"
    if event == "StopFailure":
        return "needs_input", "Turn stopped with an error"
    if event == "PermissionRequest":
        if payload.get("_orch_permission_decision") == "allow":
            return "working", "Permission auto-approved"
        return "needs_input", "Permission requested"
    if event == "Notification":
        notification_type = str(payload.get("notification_type") or "").strip()
        if notification_type == "permission_prompt":
            return "needs_input", "Permission requested"
        if notification_type == "idle_prompt":
            return "waiting_user", "Waiting for your input"
        return None
    if event == "SessionEnd":
        return "ended", "Session ended"
    return None


def claude_permission_decision(
    payload: Mapping[str, Any], *, policy: str = "observe",
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return a native Claude permission decision for an opted-in orch run.

    The policy is deliberately scoped by the environment inherited from the
    Orchestrator launcher. Installing the hook therefore does not silently
    change permissions for unrelated Claude sessions started by the user.
    """
    if policy not in _CLAUDE_PERMISSION_POLICIES:
        raise ValueError(f"unsupported Claude permission policy: {policy}")
    if policy == "observe":
        return None
    event = str(
        payload.get("hook_event_name") or payload.get("event") or ""
    ).strip()
    if (
        event != "PermissionRequest"
        or not str(payload.get("tool_name") or "").strip()
    ):
        return None
    env = os.environ if environ is None else environ
    if not str(env.get("ORCH_RUN_ID") or "").strip():
        return None
    if not str(env.get("ORCH_TMUX_SESSION") or "").strip():
        return None
    agent = str(env.get("ORCH_AGENT_TYPE") or "claude").strip().lower()
    if agent != "claude":
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }


def handle_claude_hook(
    payload: dict[str, Any], *, permission_policy: str = "observe",
    store: NativeActivityStore | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Record one Claude event and optionally answer a permission request."""
    env = os.environ if environ is None else environ
    decision = claude_permission_decision(
        payload, policy=permission_policy, environ=env,
    )
    recorded_payload = dict(payload)
    if decision is not None:
        recorded_payload["_orch_permission_decision"] = "allow"
    try:
        activity_store = store or NativeActivityStore()
        record_agent_event("claude", recorded_payload, store=activity_store)

        # Claude's background-agent daemon can fork the visible conversation
        # into a new native session id. Hooks then report lifecycle events for
        # that child id while Orchestrator still identifies the pane by the
        # preallocated/resumed id in session.json. Mirror the event to that
        # stable identity so a child Stop/idle event clears the pane's working
        # state instead of leaving its spinner active forever.
        session_path = str(env.get("ORCH_SESSION_JSON") or "").strip()
        if session_path and str(env.get("ORCH_RUN_ID") or "").strip():
            try:
                metadata = json.loads(Path(session_path).read_text())
                resume = (
                    metadata.get("resume")
                    if isinstance(metadata.get("resume"), dict) else {}
                )
                stable_session_id = str(
                    resume.get("id") or metadata.get("resume_id") or ""
                ).strip()
            except (OSError, ValueError, AttributeError):
                stable_session_id = ""
            reported_session_id = str(
                recorded_payload.get("session_id")
                or recorded_payload.get("sessionId") or ""
            ).strip()
            if (
                stable_session_id
                and stable_session_id.lower() != reported_session_id.lower()
            ):
                stable_payload = dict(recorded_payload)
                stable_payload["session_id"] = stable_session_id
                stable_payload.pop("sessionId", None)
                record_agent_event(
                    "claude", stable_payload, store=activity_store,
                )
    except Exception:
        # Telemetry is best-effort. A temporary SQLite failure must not turn
        # an otherwise valid native approval back into a blocking prompt.
        if decision is None:
            raise
    return decision


def record_agent_event(
    agent: str, payload: dict[str, Any], *, store: NativeActivityStore | None = None,
) -> dict[str, Any] | None:
    """Record one lifecycle hook event. Unknown events are harmless no-ops."""
    agent = str(agent or "").strip().lower()
    if agent != "claude":
        return None
    mapped = _claude_event_state(payload)
    if mapped is None:
        return None
    session_id = str(
        payload.get("session_id") or payload.get("sessionId") or ""
    ).strip()
    if not session_id:
        return None
    state, reason = mapped
    event = str(payload.get("hook_event_name") or payload.get("event") or "")
    return (store or NativeActivityStore()).record(
        agent="claude",
        session_id=session_id,
        state=state,
        event=event,
        reason=reason,
        source="claude-hook",
        occurred_at=_event_time(payload.get("timestamp")),
    )


def claude_hook_entries(
    command: str, *, permission_command: str = "",
) -> dict[str, list[dict[str, Any]]]:
    hook = {"type": "command", "command": command, "timeout": 2}
    permission_hook = {
        "type": "command",
        "command": permission_command or command,
        "timeout": 2,
    }
    return {
        event: [{"hooks": [dict(hook)]}]
        for event in (
            "UserPromptSubmit", "Stop", "StopFailure", "SessionEnd",
        )
    } | {
        "PermissionRequest": [{"hooks": [permission_hook]}],
        "Notification": [{
            "matcher": "permission_prompt|idle_prompt",
            "hooks": [dict(hook)],
        }]
    }


def install_claude_hooks(
    settings_path: Path | None = None, *, orch_path: str = "",
    permission_policy: str = "observe",
) -> tuple[Path, bool]:
    """Merge Orchestrator lifecycle hooks into Claude user settings."""
    if permission_policy not in _CLAUDE_PERMISSION_POLICIES:
        raise ValueError(
            f"unsupported Claude permission policy: {permission_policy}"
        )
    path = Path(settings_path or (Path.home() / ".claude" / "settings.json"))
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read Claude settings: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Claude settings root must be a JSON object")
    if orch_path:
        command = f"{shlex.quote(orch_path)} agent-event --agent claude"
    else:
        # Importing the full Orchestrator CLI pulls in dashboard/task modules
        # and adds hundreds of milliseconds to every Claude prompt. The hook
        # path imports only this stdlib-only module and normally returns in
        # tens of milliseconds.
        python = shutil.which("python3") or sys.executable
        project_root = Path(__file__).resolve().parent.parent
        command = (
            f"PYTHONPATH={shlex.quote(str(project_root))} "
            f"{shlex.quote(python)} -m agent_orchestrator.native_activity "
            "--agent claude"
        )
    permission_command = command
    if permission_policy != "observe":
        permission_command += (
            f" --permission-policy {shlex.quote(permission_policy)}"
        )
    desired = claude_hook_entries(
        command, permission_command=permission_command,
    )
    hooks = data.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise ValueError("Claude settings hooks must be a JSON object")

    changed = False
    for event, entries in desired.items():
        current = hooks.get(event)
        if current is None:
            hooks[event] = entries
            changed = True
            continue
        if not isinstance(current, list):
            raise ValueError(f"Claude settings hooks.{event} must be a list")
        desired_command = entries[0]["hooks"][0]["command"]
        if event == "PermissionRequest":
            for group in current:
                if not isinstance(group, dict):
                    continue
                group_hooks = group.get("hooks")
                if not isinstance(group_hooks, list):
                    continue
                for item in group_hooks:
                    if not isinstance(item, dict):
                        continue
                    existing_command = str(item.get("command") or "")
                    is_orch_hook = (
                        "--agent claude" in existing_command
                        and (
                            "agent_orchestrator.native_activity" in existing_command
                            or "agent-event" in existing_command
                        )
                    )
                    if is_orch_hook and existing_command != desired_command:
                        item["command"] = desired_command
                        changed = True
        already_present = any(
            isinstance(group, dict)
            and any(
                isinstance(item, dict)
                and item.get("command") == desired_command
                for item in group.get("hooks", [])
                if isinstance(group.get("hooks"), list)
            )
            for group in current
        )
        if not already_present:
            current.extend(entries)
            changed = True
    if changed:
        data["hooks"] = hooks
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    return path, changed


@dataclass
class _TrackedRun:
    run_id: str
    agent: str
    session_id: str
    transcript_path: str
    alive: bool
    started_at: float


@dataclass
class _TailCursor:
    path: str = ""
    inode: int = 0
    offset: int = 0
    remainder: bytes = b""
    initialized: bool = False


class NativeActivityService:
    """Low-cost Codex JSONL tailer plus Claude hook-state reconciler."""

    def __init__(
        self, *, store: NativeActivityStore | None = None,
        poll_interval_s: float = 0.25, bootstrap_bytes: int = 32 * 1024 * 1024,
    ):
        self.store = store or NativeActivityStore()
        self.poll_interval_s = max(0.1, float(poll_interval_s))
        self.bootstrap_bytes = max(64 * 1024, int(bootstrap_bytes))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._runs: dict[str, _TrackedRun] = {}
        self._states: dict[tuple[str, str], dict[str, Any]] = {}
        self._tails: dict[tuple[str, str], _TailCursor] = {}
        self._store_signature: tuple[int, int, int, int] | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="orch-native-activity",
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread:
            thread.join(timeout=timeout)
        with self._lock:
            self._thread = None

    def register_runs(self, rows: Iterable[dict[str, Any]]) -> None:
        # One native Codex/Claude conversation can have several historical
        # Orchestrator runs after repeated resumes. Lifecycle state belongs to
        # the conversation, but notifications and UI state must belong to one
        # concrete run. Prefer the live run, then the newest historical run.
        # Otherwise one task_complete event gets broadcast to every old alias.
        canonical: dict[tuple[str, str], _TrackedRun] = {}
        for row in rows:
            agent = str(row.get("agent") or "").strip().lower()
            if "codex" in agent:
                agent = "codex"
            elif "claude" in agent:
                agent = "claude"
            else:
                continue
            resume = row.get("resume") if isinstance(row.get("resume"), dict) else {}
            session_id = str(
                resume.get("id") or row.get("resume_id") or ""
            ).strip().lower()
            if not session_id:
                continue
            run_id = str(row.get("run_id") or "")
            if not run_id:
                continue
            tracked = _TrackedRun(
                run_id=run_id,
                agent=agent,
                session_id=session_id,
                transcript_path=str(row.get("_native_transcript_path") or ""),
                alive=bool(row.get("alive")),
                started_at=_event_time(row.get("started_at"), fallback=0.0),
            )
            key = (agent, session_id)
            previous = canonical.get(key)
            if previous is None or (
                tracked.alive, tracked.started_at, tracked.run_id
            ) > (
                previous.alive, previous.started_at, previous.run_id
            ):
                canonical[key] = tracked
        registered = {run.run_id: run for run in canonical.values()}
        with self._lock:
            self._runs = registered
            live_keys = {(run.agent, run.session_id) for run in registered.values()}
            for key in list(self._tails):
                if key not in live_keys:
                    self._tails.pop(key, None)

    def _set_state(
        self, key: tuple[str, str], state: str, *, source: str,
        event: str = "", reason: str = "", at: float | None = None,
    ) -> None:
        now = float(at if at is not None else time.time())
        with self._lock:
            previous = self._states.get(key)
            since = (
                float(previous.get("since") or now)
                if previous and previous.get("state") == state else now
            )
            self._states[key] = {
                "state": state,
                "since": since,
                "updated_at": now,
                "source": source,
                "confidence": "high",
                "event": event,
                "reason": reason,
            }

    @staticmethod
    def _codex_event(obj: dict[str, Any]) -> tuple[str, str, float] | None:
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        event = str(payload.get("type") or obj.get("type") or "")
        mapped = _CODEX_BOUNDARY_EVENTS.get(event)
        if mapped is None:
            return None
        state, reason = mapped
        return state, event, _event_time(obj.get("timestamp"))

    def _consume_codex_bytes(
        self, key: tuple[str, str], cursor: _TailCursor, data: bytes,
    ) -> None:
        blob = cursor.remainder + data
        lines = blob.split(b"\n")
        cursor.remainder = lines.pop() if lines else b""
        latest: tuple[str, str, float] | None = None
        for raw in lines:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(obj, dict):
                event = self._codex_event(obj)
                if event is not None:
                    latest = event
        if latest is not None:
            state, event, at = latest
            reason = _CODEX_BOUNDARY_EVENTS[event][1]
            self._set_state(
                key, state, source="codex-transcript", event=event,
                reason=reason, at=at,
            )

    def _poll_codex(self, run: _TrackedRun) -> None:
        if not run.transcript_path:
            return
        path = Path(run.transcript_path)
        try:
            stat = path.stat()
        except OSError:
            return
        key = (run.agent, run.session_id)
        with self._lock:
            cursor = self._tails.setdefault(key, _TailCursor())
        inode = int(getattr(stat, "st_ino", 0) or 0)
        if cursor.path != str(path) or cursor.inode != inode or stat.st_size < cursor.offset:
            cursor.path = str(path)
            cursor.inode = inode
            cursor.offset = max(0, int(stat.st_size) - self.bootstrap_bytes)
            cursor.remainder = b""
            cursor.initialized = False
        if int(stat.st_size) == cursor.offset and cursor.initialized:
            return
        try:
            with path.open("rb") as handle:
                handle.seek(cursor.offset)
                data = handle.read()
        except OSError:
            return
        if not cursor.initialized and cursor.offset:
            first_newline = data.find(b"\n")
            data = data[first_newline + 1:] if first_newline >= 0 else b""
        cursor.offset = int(stat.st_size)
        cursor.initialized = True
        self._consume_codex_bytes(key, cursor, data)

    def _load_hook_states(self) -> None:
        wal_path = Path(str(self.store.path) + "-wal")

        def signature(path: Path) -> tuple[int, int]:
            try:
                stat = path.stat()
                return int(stat.st_mtime_ns), int(stat.st_size)
            except OSError:
                return 0, 0

        current_signature = (*signature(self.store.path), *signature(wal_path))
        if current_signature == self._store_signature:
            return
        self._store_signature = current_signature
        for row in self.store.read_all():
            key = (str(row.get("agent") or ""), str(row.get("session_id") or ""))
            if not all(key):
                continue
            with self._lock:
                previous = self._states.get(key)
            if previous and float(previous.get("updated_at") or 0) >= float(row.get("updated_at") or 0):
                continue
            self._set_state(
                key, str(row.get("state") or "waiting_user"),
                source=str(row.get("source") or "claude-hook"),
                event=str(row.get("event") or ""),
                reason=str(row.get("reason") or ""),
                at=float(row.get("updated_at") or time.time()),
            )
            with self._lock:
                self._states[key]["since"] = float(row.get("since") or row.get("updated_at") or time.time())

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval_s):
            self._load_hook_states()
            with self._lock:
                runs = list(self._runs.values())
            for run in runs:
                if run.agent == "codex" and run.alive:
                    self._poll_codex(run)

    @staticmethod
    def _mission_state(native_state: str) -> str:
        return {
            "working": "working",
            "background_working": "working",
            "needs_input": "needs_input",
            "waiting_user": "waiting",
            "ended": "waiting",
        }.get(native_state, "waiting")

    def apply(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Overlay current native state onto a dashboard session snapshot."""
        now = time.time()
        with self._lock:
            states = {key: dict(value) for key, value in self._states.items()}
            canonical_run_ids = set(self._runs)
        for row in rows:
            if str(row.get("run_id") or "") not in canonical_run_ids:
                continue
            agent = str(row.get("agent") or "").strip().lower()
            agent = "codex" if "codex" in agent else "claude" if "claude" in agent else agent
            resume = row.get("resume") if isinstance(row.get("resume"), dict) else {}
            session_id = str(resume.get("id") or row.get("resume_id") or "").strip().lower()
            native = states.get((agent, session_id))
            if not native:
                continue
            native["age_s"] = round(max(0.0, now - float(native.get("since") or now)), 3)
            state = str(native.get("state") or "")
            background_active = bool(row.get("background_active"))
            terminal_active = background_active or bool(
                row.get("activity_sustained_active")
            )
            mission = row.get("mission_control")
            progress = (
                mission.get("progress")
                if isinstance(mission, dict)
                and isinstance(mission.get("progress"), dict)
                else {}
            )
            goal_active = str(progress.get("goal_state") or "") == "pursuing"
            # The live Claude input prompt is stronger evidence than a stale
            # UserPromptSubmit hook.  This fixes sessions that spin forever
            # when Claude misses Stop, while explicit goals, background work,
            # and sustained output continue to win.
            if (
                agent == "claude"
                and state in _WORKING_STATES
                and row.get("terminal_prompt_ready") is True
                and not terminal_active
                and not goal_active
            ):
                try:
                    prompt_age = max(
                        0.0, float(row.get("activity_last_change_age_s") or 0.0)
                    )
                except (TypeError, ValueError):
                    prompt_age = 0.0
                prompt_since = now - prompt_age
                native.update({
                    "state": "waiting_user",
                    "since": prompt_since,
                    "age_s": round(prompt_age, 3),
                    "event": "PromptReady",
                    "reason": "Claude is ready for input",
                    "source": "terminal-prompt",
                })
                state = "waiting_user"
            row["native_activity"] = native
            effectively_working = state in _WORKING_STATES or terminal_active
            if effectively_working:
                row["busy"] = True
            elif state in _WAITING_STATES:
                row["busy"] = False
                row["screen_busy"] = False
            if isinstance(mission, dict) and mission:
                original_mission_state = str(mission.get("state") or "")
                priority = str(
                    mission.get("priority") or row.get("panel_state") or ""
                ).lower()
                activity_age = native["age_s"]
                activity_started = float(native.get("since") or now)
                activity_source = str(native.get("source") or "")
                if background_active:
                    activity_age = (
                        row.get("background_active_age_s") or activity_age
                    )
                    activity_started = float(
                        row.get("background_active_started_ts")
                        or activity_started
                    )
                    activity_source = "terminal-background"
                elif terminal_active:
                    activity_age = row.get("activity_streak_age_s") or activity_age
                    activity_source = "terminal-output"
                # Claude can return to its prompt while an explicit goal keeps
                # background monitors/agents alive.  A waiting hook in that
                # state is not an intervention request; keep the Session active
                # until the goal pauses, stalls, completes, or asks for input.
                effectively_working = effectively_working or (
                    goal_active and state not in {"needs_input", "ended"}
                )
                if goal_active and activity_source not in {
                    "terminal-background", "terminal-output"
                }:
                    activity_source = "goal-active"
                row["busy"] = effectively_working
                mission_state = (
                    "blocked" if original_mission_state == "blocked"
                    else "working" if effectively_working
                    else self._mission_state(state)
                )
                mission["state"] = mission_state
                mission["activity_mode"] = "busy" if effectively_working else "idle"
                mission["activity_age_s"] = activity_age
                mission["last_change_at"] = datetime.fromtimestamp(
                    activity_started
                ).astimezone().isoformat(timespec="seconds")
                mission["activity_source"] = activity_source
                if effectively_working:
                    mission["needs_attention"] = False
                    mission["attention_reason"] = ""
                elif mission_state == "blocked":
                    mission["needs_attention"] = True
                    mission["attention_reason"] = "Blocked"
                elif state == "needs_input":
                    mission["needs_attention"] = True
                    mission["attention_reason"] = native.get("reason") or "Waiting for your input"
                elif (
                    mission_state == "waiting"
                    and priority in {"p0", "p1"}
                    and float(activity_age or 0.0) >= 300.0
                ):
                    mission["needs_attention"] = True
                    mission["attention_reason"] = f"{priority.upper()} has been idle"
                else:
                    mission["needs_attention"] = False
                    mission["attention_reason"] = ""
        return rows

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = list(self._runs.values())
            states = {key: dict(value) for key, value in self._states.items()}
        now = time.time()
        result = []
        for run in runs:
            state = states.get((run.agent, run.session_id))
            if not state:
                continue
            state["age_s"] = round(max(0.0, now - float(state.get("since") or now)), 3)
            result.append({"run_id": run.run_id, "native_activity": state})
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Record an agent lifecycle event")
    parser.add_argument("--agent", required=True, choices=("claude",))
    parser.add_argument(
        "--permission-policy", default="observe",
        choices=sorted(_CLAUDE_PERMISSION_POLICIES),
    )
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            response = handle_claude_hook(
                payload, permission_policy=args.permission_policy,
            )
            if response is not None:
                json.dump(response, sys.stdout, separators=(",", ":"))
                sys.stdout.write("\n")
    except Exception as exc:
        # Hook failures must never reject or add stdout context to an agent
        # turn. Claude records stderr in its own debug log when needed.
        print(f"native activity event ignored: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
