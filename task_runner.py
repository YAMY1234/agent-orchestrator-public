"""Single task lifecycle: start tmux session, inject prompt, run monitor."""

import logging
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

from config import TaskConfig, load_skills
from monitor import Monitor, tmux_send, tmux_session_alive
from notifier import notify_task_done
from prompt import build_initial_context
from state import StateManager

log = logging.getLogger(__name__)

DEFAULT_TERMINAL_THEME = "soft-dark"


def _preallocate_native_resume_id(agent: str) -> tuple[str, str, str]:
    if agent == "claude":
        return str(uuid.uuid4()), "claude-session-id-preallocated", ""
    if agent == "cursor":
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
            return "", "", f"agent create-chat failed: {exc}"
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return "", "", f"agent create-chat failed: {err or proc.returncode}"
        chat_id = next((tok for tok in proc.stdout.strip().split() if tok), "")
        if not chat_id:
            return "", "", "agent create-chat returned no chat id"
        return chat_id, "cursor-create-chat", ""
    return "", "", ""


def _resume_cmd_for(agent: str, resume_id: str) -> str:
    quoted = shlex.quote(resume_id)
    if agent == "claude":
        return f"claude --resume {quoted}"
    if agent == "cursor":
        return f"agent --resume {quoted}"
    if agent == "codex":
        return f"codex resume --dangerously-bypass-approvals-and-sandbox {quoted}"
    return f"{shlex.quote(agent or 'agent')} --resume {quoted}"


def _build_agent_command(task: TaskConfig, native_resume_id: str = "") -> str:
    """Compose the shell command that launches the agent inside tmux.

    - claude: `claude`
    - cursor / agent: `agent`
    - codex: `codex --dangerously-bypass-approvals-and-sandbox` (YOLO mode;
       codex's permission model is one-shot at startup, not per-command,
       so we sidestep the per-command perm_gate watcher path).
    Model flag is `--model` for cursor/claude and `-m` for codex.
    """
    if task.agent == "claude":
        cmd = "claude"
    elif task.agent == "cursor":
        cmd = "agent"
    elif task.agent == "codex":
        cmd = "codex --dangerously-bypass-approvals-and-sandbox"
    else:
        cmd = task.agent
    if task.model:
        if task.agent == "codex":
            cmd += f" -m {task.model}"
        else:
            cmd += f" --model {task.model}"
    if task.effort:
        if task.agent == "claude":
            cmd += f" --effort {shlex.quote(task.effort)}"
        elif task.agent == "codex":
            effort_cfg = f'model_reasoning_effort="{task.effort}"'
            cmd += f" -c {shlex.quote(effort_cfg)}"
    if native_resume_id:
        quoted = shlex.quote(native_resume_id)
        if task.agent == "claude":
            cmd += f" --session-id {quoted}"
        elif task.agent == "cursor":
            cmd += f" --resume {quoted}"
    return cmd


def _shell_env_prefix(env_vars: Optional[dict[str, str]]) -> str:
    if not env_vars:
        return ""
    parts = []
    for key, value in env_vars.items():
        if value is None:
            continue
        parts.append(f"{key}={shlex.quote(str(value))}")
    return " ".join(parts)


def _start_tmux_session(session_name: str, cwd: str, agent_cmd: str,
                        cols: int = 200, rows: int = 50,
                        env_vars: Optional[dict[str, str]] = None):
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name,
         "-x", str(cols), "-y", str(rows)],
        cwd=cwd, timeout=10,
    )
    time.sleep(0.5)
    env_prefix = _shell_env_prefix(env_vars)
    command = f"{env_prefix} {agent_cmd}" if env_prefix else agent_cmd
    tmux_send(session_name, command, literal=True)
    tmux_send(session_name, "Enter")


class TaskRunner:
    def __init__(self, task: TaskConfig, state_mgr: StateManager,
                 project_dir: Path, skills_dir: Path, output_dir: Path,
                 project_name: str = ""):
        self.task = task
        self.state_mgr = state_mgr
        self.project_dir = project_dir
        self.skills_dir = skills_dir
        self.output_dir = output_dir
        self.project_name = project_name or task.name
        self.session_name = f"orch-{task.name}-{int(time.time()) % 100000}"

    def run(self):
        task = self.task
        log.info(f"[{task.name}] Starting task (agent={task.agent}, cwd={task.cwd})")

        cwd_abs = task.cwd if Path(task.cwd).is_absolute() else str(self.project_dir / task.cwd)
        native_resume_id, native_resume_source, native_resume_error = (
            _preallocate_native_resume_id(task.agent)
        )

        self.state_mgr.init_task(
            task.name,
            mode=task.mode,
            status="running",
            max_rounds=task.max_rounds,
            idle_timeout=task.idle_timeout,
            tmux_session=self.session_name,
            started_at=self.state_mgr.now(),
            log_file=f"logs/{task.name}.log",
            cwd=cwd_abs,
            agent=task.agent,
            model=task.model,
            effort=task.effort,
            resume_agent=task.agent if native_resume_id else "",
            resume_id=native_resume_id,
            resume_cmd=(
                _resume_cmd_for(task.agent, native_resume_id)
                if native_resume_id else ""
            ),
            resume_source=native_resume_source,
            resume_recorded_at=(
                self.state_mgr.now() if native_resume_id else ""
            ),
            resume_confidence="exact" if native_resume_id else "",
            resume_capture_status=(
                "captured" if native_resume_id
                else ("missing" if native_resume_error else "pending")
            ),
            resume_capture_error=native_resume_error,
            terminal_theme=os.environ.get(
                "ORCH_DEFAULT_TERMINAL_THEME",
                DEFAULT_TERMINAL_THEME,
            ),
        )

        cwd = cwd_abs
        agent_cmd = _build_agent_command(task, native_resume_id)
        run_id = f"{self.output_dir.name}::{task.name}"
        _start_tmux_session(
            self.session_name,
            cwd,
            agent_cmd,
            env_vars={
                "ORCH_RUN_ID": run_id,
                "ORCH_RUN_DIR": str(self.output_dir),
                "ORCH_TMUX_SESSION": self.session_name,
                "ORCH_TASK_NAME": task.name,
                "ORCH_AGENT_TYPE": task.agent,
            },
        )

        log.info(f"[{task.name}] Waiting for agent to initialize...")
        if not self._wait_for_agent_ready(timeout=60):
            log.error(f"[{task.name}] Agent did not become ready in time")
            self.state_mgr.update(task.name, status="failed")
            return

        skills_text = load_skills(task.skills, self.skills_dir)
        steps = task.steps if task.steps else []
        step_idx = 0

        prompt = self._build_prompt(task.initial_prompt, skills_text, step_idx, steps)
        self._send_prompt(prompt)

        log_dir = self.output_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        monitor = Monitor(
            task_name=task.name,
            tmux_session=self.session_name,
            state_mgr=self.state_mgr,
            log_dir=log_dir,
            completion_criteria=task.completion_criteria,
            important_notes=task.important_notes,
        )

        try:
            while True:
                monitor.run()

                state = self.state_mgr.get(task.name)
                if not state or state.status in ("completed", "failed"):
                    break

                if steps and step_idx < len(steps) - 1:
                    step_idx += 1
                    log.info(f"[{task.name}] Advancing to step {step_idx + 1}/{len(steps)}")
                    self.state_mgr.update(
                        task.name,
                        current_step=step_idx,
                        round=0,
                        max_rounds=steps[step_idx].max_rounds,
                        status="running",
                    )
                    next_prompt = steps[step_idx].prompt
                    self._send_prompt(next_prompt)
                    monitor._running = True
                    monitor._idle_since = None
                    monitor._last_hash = ""
                else:
                    break
        finally:
            monitor.stop()

        final = self.state_mgr.get(task.name)
        status = final.status if final else "unknown"
        rounds = f"{final.round}/{final.max_rounds}" if final else ""
        log.info(f"[{task.name}] Task finished with status: {status}")
        notify_task_done(self.project_name, task.name, status, rounds=rounds)

    def _wait_for_agent_ready(self, timeout: int = 60) -> bool:
        """Wait until the agent's input prompt is visible."""
        import re
        from monitor import tmux_capture, tmux_send, tmux_session_alive

        ready_patterns = [
            re.compile(r"Plan, search, build anything"),  # Cursor Agent
            re.compile(r"[❯>]\s*$", re.MULTILINE),        # Claude Code
            re.compile(r"\?\s+for shortcuts"),              # Claude Code
            # Codex TUI shows a box-drawn input ruler with a "›" arrow
            # followed by a rotating placeholder ("Improve documentation
            # in @filename", "Find and fix a bug in @filename", "Explain
            # ...", etc.) once the TUI is ready. The "@filename" suffix
            # is stable across all the placeholder variants we've seen,
            # and is unlikely to appear in real user input verbatim, so
            # we anchor on it instead of the placeholder verb.
            re.compile(r"›\s+\S.*@filename"),             # Codex CLI
        ]

        trust_patterns = [
            re.compile(r"Yes, I trust this folder"),
            re.compile(r"Is this a project you created"),
        ]

        start = time.time()
        while time.time() - start < timeout:
            if not tmux_session_alive(self.session_name):
                return False
            pane = tmux_capture(self.session_name)

            if any(p.search(pane) for p in trust_patterns):
                log.info(f"[{self.task.name}] Trust folder prompt detected, confirming...")
                tmux_send(self.session_name, "Enter")
                time.sleep(3)
                continue

            if any(p.search(pane) for p in ready_patterns):
                log.info(f"[{self.task.name}] Agent ready (waited {time.time() - start:.1f}s)")
                time.sleep(1)
                return True
            time.sleep(2)
        return False

    def _build_prompt(self, initial_prompt: str, skills_text: str,
                      step_idx: int, steps: list) -> str:
        task = self.task
        if steps and step_idx < len(steps):
            prompt_text = f"[步骤 {step_idx + 1}/{len(steps)}]\n{steps[step_idx].prompt}"
        else:
            prompt_text = initial_prompt

        return build_initial_context(
            initial_prompt=prompt_text,
            skills_text=skills_text,
            completion_criteria=task.completion_criteria,
            important_notes=task.important_notes,
        )

    def _send_prompt(self, prompt: str):
        log.info(f"[{self.task.name}] Sending prompt ({len(prompt)} chars)")
        tmux_send(self.session_name, prompt, literal=True)
        time.sleep(2)
        tmux_send(self.session_name, "Enter")
