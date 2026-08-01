"""Tmux session monitor: permission auto-accept, idle detection, auto-continue, logging."""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from prompt import COMPLETION_KEYWORDS, get_continuation_prompt
from state import StateManager

log = logging.getLogger(__name__)

PERMISSION_PATTERNS = [
    re.compile(r"Do you want to (proceed|allow)"),
    re.compile(r"Run this command\?"),
    re.compile(r"Write to this file\?"),
    re.compile(r"Create this file\?"),
    re.compile(r"Delete this file\?"),
    re.compile(r"Allow this web search\?"),
    re.compile(r"Allow this web fetch\?"),
]

IDLE_INDICATORS = [
    re.compile(r"[❯>]\s*$", re.MULTILINE),
    re.compile(r"\?\s+for shortcuts", re.MULTILINE),
    re.compile(r"Add a follow-up", re.MULTILINE),
    re.compile(r"Plan, search, build anything", re.MULTILINE),
    # Codex TUI: idle pane shows a "›" arrow with a rotating placeholder
    # like "Improve documentation in @filename" / "Find and fix a bug in
    # @filename". When the agent is busy, the status line changes to
    # "Esc to interrupt" / "Working ..." and the placeholder gets
    # replaced by the user's draft, so the "@filename" suffix is the
    # most reliable "no draft, agent waiting" anchor.
    re.compile(r"›\s+\S.*@filename", re.MULTILINE),
]


def tmux_capture(session: str) -> str:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p", "-S", "-"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return ""


def tmux_send(session: str, text: str, literal: bool = False):
    cmd = ["tmux", "send-keys", "-t", session]
    if literal:
        cmd.append("-l")
    cmd.append(text)
    subprocess.run(cmd, timeout=5)


def tmux_session_alive(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True, timeout=5,
    )
    return result.returncode == 0


def _content_hash(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text.strip())
    return hashlib.md5(cleaned.encode()).hexdigest()


def _check_permission_prompt(text: str) -> Optional[str]:
    for pat in PERMISSION_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


def _check_idle(text: str) -> bool:
    return any(pat.search(text) for pat in IDLE_INDICATORS)


def _check_completion(text: str, baseline: str = "") -> bool:
    """Check for completion keywords only in content that's NEW since baseline."""
    if baseline:
        new_text = text[len(baseline):] if text.startswith(baseline) else text
        bl_lines = set(baseline.strip().splitlines())
        new_lines = [l for l in text.strip().splitlines() if l not in bl_lines]
        new_text = "\n".join(new_lines)
    else:
        new_text = text
    return any(kw in new_text for kw in COMPLETION_KEYWORDS)


class Monitor:
    def __init__(self, task_name: str, tmux_session: str,
                 state_mgr: StateManager, log_dir: str | Path = "logs",
                 completion_criteria: str = "", important_notes: str = ""):
        self.task_name = task_name
        self.tmux_session = tmux_session
        self.state_mgr = state_mgr
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / f"{task_name}.log"
        self.completion_criteria = completion_criteria
        self.important_notes = important_notes

        self._last_hash = ""
        self._idle_since: Optional[float] = None
        self._running = True
        self._prompt_baseline = ""

    def run(self):
        log.info(f"[{self.task_name}] Monitor started, session={self.tmux_session}")

        while self._running:
            state = self.state_mgr.get(self.task_name)
            if not state:
                log.warning(f"[{self.task_name}] Task not found in state, stopping")
                break

            if state.status in ("completed", "failed"):
                log.info(f"[{self.task_name}] Task {state.status}, monitor exiting")
                break

            if not tmux_session_alive(self.tmux_session):
                log.warning(f"[{self.task_name}] Tmux session gone, marking failed")
                self.state_mgr.update(self.task_name, status="failed")
                break

            pane_text = tmux_capture(self.tmux_session)
            if not pane_text:
                time.sleep(state.idle_timeout / 4)
                continue

            self._write_log(pane_text)

            perm = _check_permission_prompt(pane_text)
            if perm:
                log.info(f"[{self.task_name}] Permission prompt detected: {perm}")
                if "Run this command" in perm:
                    tmux_send(self.tmux_session, "y")
                else:
                    tmux_send(self.tmux_session, "Enter")
                time.sleep(1)
                continue

            if _check_completion(pane_text, self._prompt_baseline):
                log.info(f"[{self.task_name}] Completion keyword detected in agent output")
                self.state_mgr.update(
                    self.task_name,
                    status="completed",
                    last_activity=self.state_mgr.now(),
                )
                break

            if state.mode == "paused":
                time.sleep(2)
                continue

            current_hash = _content_hash(pane_text)
            if current_hash != self._last_hash:
                self._last_hash = current_hash
                self._idle_since = None
                self.state_mgr.update(
                    self.task_name,
                    last_activity=self.state_mgr.now(),
                )
            else:
                if self._idle_since is None:
                    self._idle_since = time.time()

            if state.mode == "auto" and self._idle_since:
                idle_seconds = time.time() - self._idle_since
                if idle_seconds >= state.idle_timeout and _check_idle(pane_text):
                    if state.round >= state.max_rounds:
                        log.info(f"[{self.task_name}] Max rounds ({state.max_rounds}) reached")
                        self.state_mgr.update(self.task_name, status="completed")
                        break

                    prompt = get_continuation_prompt(
                        mode="fixed",
                        completion_criteria=self.completion_criteria,
                        important_notes=self.important_notes,
                    )
                    log.info(f"[{self.task_name}] Sending continuation (round {state.round + 1})")

                    self._prompt_baseline = tmux_capture(self.tmux_session)

                    tmux_send(self.tmux_session, prompt, literal=True)
                    time.sleep(2)
                    tmux_send(self.tmux_session, "Enter")
                    self._idle_since = None
                    self._last_hash = ""

                    time.sleep(3)
                    self._prompt_baseline = tmux_capture(self.tmux_session)

                    self.state_mgr.update(
                        self.task_name,
                        round=state.round + 1,
                        last_activity=self.state_mgr.now(),
                    )
                    time.sleep(2)
                    continue

            time.sleep(state.idle_timeout / max(state.idle_timeout, 4))

    def stop(self):
        self._running = False

    def _write_log(self, text: str):
        with open(self.log_file, "w") as f:
            f.write(text)
