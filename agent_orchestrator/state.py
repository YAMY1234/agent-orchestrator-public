"""Unified state.json control panel.

Users can edit state.json at any time to control agent behavior:
- mode: manual/auto/paused
- max_rounds, idle_timeout: adjustable mid-run
- status: set to 'completed' to manually finish a task
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Optional

from .json_store import edit_json


@dataclass
class TaskState:
    mode: str = "manual"
    status: str = "pending"
    current_step: int = 0
    round: int = 0
    max_rounds: int = 10
    idle_timeout: int = 20
    tmux_session: str = ""
    started_at: str = ""
    last_activity: str = ""
    log_file: str = ""
    label: str = ""
    cwd: str = ""
    agent: str = ""
    model: str = ""
    effort: str = ""
    resume_agent: str = ""
    resume_id: str = ""
    resume_cmd: str = ""
    resume_source: str = ""
    resume_source_path: str = ""
    resume_recorded_at: str = ""
    resume_confidence: str = ""
    resume_capture_status: str = ""
    resume_capture_error: str = ""
    terminal_theme: str = ""
    stopped_at: str = ""


class StateManager:
    def __init__(self, state_file: str | Path = "state.json"):
        self.state_file = Path(state_file)
        self._lock = Lock()
        self._cache: dict[str, TaskState] = {}

    def init_task(self, name: str, **overrides) -> TaskState:
        with self._lock:
            state = TaskState(**overrides)
            self._cache[name] = state
            with edit_json(self.state_file, create=True) as data:
                data[name] = asdict(state)
            return state

    def get(self, name: str) -> Optional[TaskState]:
        """Read from file each time to pick up user edits."""
        self._load_from_file()
        return self._cache.get(name)

    def update(self, name: str, **kwargs) -> TaskState:
        with self._lock:
            with edit_json(self.state_file) as data:
                raw = data.get(name)
                if not isinstance(raw, dict):
                    raise KeyError(f"Task '{name}' not found in state")
                state = TaskState(**{
                    key: value for key, value in raw.items()
                    if hasattr(TaskState, key)
                })
                for key, value in kwargs.items():
                    if hasattr(state, key):
                        setattr(state, key, value)
                merged = dict(raw)
                merged.update(asdict(state))
                data[name] = merged
                self._cache[name] = state
            return state

    def get_all(self) -> dict[str, TaskState]:
        self._load_from_file()
        return dict(self._cache)

    def _load_from_file(self):
        with self._lock:
            self._load_from_file_locked()

    def _load_from_file_locked(self):
        if not self.state_file.exists():
            return
        try:
            raw = json.loads(self.state_file.read_text())
            for name, data in raw.items():
                if name in self._cache:
                    for k, v in data.items():
                        if hasattr(self._cache[name], k):
                            setattr(self._cache[name], k, v)
                else:
                    self._cache[name] = TaskState(**{
                        k: v for k, v in data.items()
                        if hasattr(TaskState, k)
                    })
        except (json.JSONDecodeError, TypeError):
            pass

    def now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S")
