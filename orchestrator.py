"""Main orchestrator: parallel task scheduling with dependency management."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import notifier
from config import ProjectConfig, load_config
from dashboard_network import (
    build_access_url,
    detect_dashboard_scheme,
    list_local_ipv4,
    pick_best_ip,
)
from local_settings import dashboard_token, require_dashboard_auth
from state import StateManager
from task_runner import TaskRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUTS_DIR = SCRIPT_DIR / "outputs"


def _configured_outputs_root(default: Path | None = None) -> Path:
    """Return the local outputs root, honoring ORCH_OUTPUTS_DIR."""
    configured = os.environ.get("ORCH_OUTPUTS_DIR", "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else (default or DEFAULT_OUTPUTS_DIR)
    )
    return root.resolve()


# False-ish macOS malloc debug env values still trigger noisy runtime messages
# in every subprocess. Drop them before task runners/watchers inherit them.
for _key in (
    "MallocStackLogging",
    "MallocStackLoggingNoCompact",
    "MallocScribble",
    "MallocGuardEdges",
    "MallocNanoZone",
):
    os.environ.pop(_key, None)


class Orchestrator:
    def __init__(self, config: ProjectConfig, project_dir: Path, output_dir: Path):
        self.config = config
        self.project_dir = project_dir
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir = project_dir / "skills"
        self.state_mgr = StateManager(output_dir / "state.json")
        self._task_map = {t.name: t for t in config.tasks}

    def start(self):
        log.info(f"Project: {self.config.project}")
        log.info(f"Tasks: {[t.name for t in self.config.tasks]}")
        log.info(f"Max concurrent agents: {self.config.max_concurrent_agents}")

        for task in self.config.tasks:
            existing = self.state_mgr.get(task.name)
            if not existing or existing.status in ("completed", "failed"):
                self.state_mgr.init_task(task.name, status="pending")

        completed = set()
        running_futures = {}

        with ThreadPoolExecutor(max_workers=self.config.max_concurrent_agents) as pool:
            while True:
                all_states = self.state_mgr.get_all()
                completed = {
                    name for name, s in all_states.items()
                    if s.status in ("completed", "failed")
                }

                pending = [
                    t for t in self.config.tasks
                    if t.name not in completed
                    and t.name not in running_futures
                    and all(dep in completed for dep in t.depends_on)
                ]

                if not self._check_constraints():
                    log.info("Shared constraints not met, waiting...")
                    time.sleep(10)
                    continue

                active_count = len(running_futures)
                slots = self.config.max_concurrent_agents - active_count

                for task in pending[:slots]:
                    log.info(f"Launching task: {task.name}")
                    runner = TaskRunner(task, self.state_mgr, self.project_dir, self.skills_dir, self.output_dir,
                                        project_name=self.config.project)
                    future = pool.submit(runner.run)
                    running_futures[task.name] = future

                done_names = []
                for name, future in running_futures.items():
                    if future.done():
                        done_names.append(name)
                        exc = future.exception()
                        if exc:
                            log.error(f"Task {name} raised exception: {exc}")
                            self.state_mgr.update(name, status="failed")

                for name in done_names:
                    del running_futures[name]

                all_task_names = {t.name for t in self.config.tasks}
                if all_task_names <= completed:
                    log.info("All tasks completed!")
                    break

                if not running_futures and not pending:
                    remaining = all_task_names - completed
                    log.warning(f"No runnable tasks, stuck. Remaining: {remaining}")
                    break

                time.sleep(3)

        self._print_summary()
        self._notify_summary()

    def _check_constraints(self) -> bool:
        max_jobs = self.config.shared_constraints.get("max_slurm_jobs")
        if max_jobs:
            try:
                result = subprocess.run(
                    ["squeue", "-u", subprocess.check_output(["whoami"]).decode().strip(),
                     "-h", "--format=%i"],
                    capture_output=True, text=True, timeout=10,
                )
                current = len(result.stdout.strip().splitlines()) if result.stdout.strip() else 0
                if current >= max_jobs:
                    log.info(f"Slurm jobs: {current}/{max_jobs}, waiting...")
                    return False
            except (subprocess.SubprocessError, FileNotFoundError):
                pass
        return True

    def _print_summary(self):
        print("\n" + "=" * 60)
        print(f"Project: {self.config.project} - Summary")
        print("=" * 60)
        for name, state in self.state_mgr.get_all().items():
            print(f"  {name}: {state.status} (rounds: {state.round}/{state.max_rounds})")
        print("=" * 60)

    def _notify_summary(self):
        summary = {
            name: state.status for name, state in self.state_mgr.get_all().items()
        }
        notifier.notify_all_done(self.config.project, summary)


def cmd_start(args):
    config_path = Path(args.config).resolve()
    config = load_config(config_path)

    if config.slack_webhook_url:
        notifier.configure(config.slack_webhook_url)

    project_dir = config_path.parent.parent  # tasks/ -> project root
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    outputs_root = _configured_outputs_root(project_dir / "outputs")
    output_dir = outputs_root / f"{config_path.stem}-{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Output dir: {output_dir}")
    orch = Orchestrator(config, project_dir, output_dir)
    orch.start()


def _resolve_output_dir(args) -> Path:
    """Find the latest output dir for a given task YAML, or use --dir."""
    if hasattr(args, 'config') and args.config:
        config_path = Path(args.config).resolve()
        outputs_root = _configured_outputs_root(
            config_path.parent.parent / "outputs"
        )
        prefix = config_path.stem
    elif Path(args.dir) != Path("."):
        return Path(args.dir)
    else:
        outputs_root = _configured_outputs_root()
        prefix = args.task if hasattr(args, 'task') else ""

    if not outputs_root.exists():
        return Path(args.dir)

    candidates = sorted(
        [d for d in outputs_root.iterdir() if d.is_dir() and d.name.startswith(prefix)],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    return Path(args.dir)


def cmd_status(args):
    output_dir = _resolve_output_dir(args)
    state_file = output_dir / "state.json"
    if not state_file.exists():
        print(f"No state.json found at {state_file}")
        return
    data = json.loads(state_file.read_text())
    print(f"Output: {output_dir}")
    print(f"{'Task':<25} {'Status':<12} {'Mode':<10} {'Round':<10} {'Last Activity'}")
    print("-" * 80)
    for name, s in data.items():
        print(f"{name:<25} {s.get('status','?'):<12} {s.get('mode','?'):<10} "
              f"{s.get('round',0)}/{s.get('max_rounds','?'):<6} {s.get('last_activity','')}")


def cmd_attach(args):
    output_dir = _resolve_output_dir(args)
    state_file = output_dir / "state.json"
    if not state_file.exists():
        print(f"No state.json found at {state_file}")
        return
    data = json.loads(state_file.read_text())
    task_state = data.get(args.task)
    if not task_state:
        print(f"Task '{args.task}' not found. Available: {list(data.keys())}")
        return
    session = task_state.get("tmux_session")
    if not session:
        print(f"Task '{args.task}' has no tmux session.")
        return
    print(f"Attaching to tmux session: {session}")
    print("(Detach with Ctrl+b d to return)")
    subprocess.run(["tmux", "attach-session", "-t", session])


def cmd_resume(args):
    output_dir = Path(args.output_dir).resolve()
    state_file = output_dir / "state.json"
    if not state_file.exists():
        print(f"No state.json found at {state_file}")
        return

    state_mgr = StateManager(state_file)
    all_states = state_mgr.get_all()

    from monitor import Monitor, tmux_session_alive

    resumed = 0
    for name, state in all_states.items():
        if state.status != "running":
            log.info(f"[{name}] status={state.status}, skipping")
            continue
        if not state.tmux_session or not tmux_session_alive(state.tmux_session):
            log.warning(f"[{name}] tmux session '{state.tmux_session}' not alive, skipping")
            continue

        log.info(f"[{name}] Resuming monitor on session {state.tmux_session}")
        monitor = Monitor(
            task_name=name,
            tmux_session=state.tmux_session,
            state_mgr=state_mgr,
            log_dir=output_dir / "logs",
        )
        monitor.run()
        resumed += 1

    if resumed == 0:
        print("No running tasks with live tmux sessions found.")
    else:
        final_states = state_mgr.get_all()
        print(f"\nResumed {resumed} task(s). Final status:")
        for name, s in final_states.items():
            print(f"  {name}: {s.status} (rounds: {s.round}/{s.max_rounds})")


def cmd_skip(args):
    output_dir = _resolve_output_dir(args)
    state_file = output_dir / "state.json"
    mgr = StateManager(state_file)
    mgr.update(args.task, status="completed")
    print(f"Task '{args.task}' marked as completed.")


def cmd_continue(args):
    script = Path(__file__).resolve().parent / "continue.sh"
    if not script.exists():
        print(f"continue.sh not found at {script}")
        sys.exit(1)
    cmd = ["bash", str(script)]
    if args.session:
        cmd.append(args.session)
    if args.prompt:
        cmd.extend(["--prompt", args.prompt])
    if args.no_attach:
        cmd.append("--no-attach")
    subprocess.run(cmd)


def cmd_organize(args):
    script = Path(__file__).resolve().parent / "organize.sh"
    if not script.exists():
        print(f"organize.sh not found at {script}")
        sys.exit(1)
    cmd = ["bash", str(script)]
    if args.stale:
        cmd.extend(["--stale", args.stale])
    if args.agent:
        cmd.append(args.agent)
    subprocess.run(cmd)


def cmd_clean(args):
    script = Path(__file__).resolve().parent / "clean.sh"
    if not script.exists():
        print(f"clean.sh not found at {script}")
        sys.exit(1)
    cmd = ["bash", str(script)]
    if args.force:
        cmd.append("-f")
    subprocess.run(cmd)


def cmd_prune(args):
    script = Path(__file__).resolve().parent / "prune.sh"
    if not script.exists():
        print(f"prune.sh not found at {script}")
        sys.exit(1)
    cmd = ["bash", str(script)]
    if args.dry_run:
        cmd.append("--dry-run")
    for name in args.protect or []:
        cmd.extend(["--protect", name])
    subprocess.run(cmd)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _projects_root() -> Path:
    configured = os.environ.get("ORCH_PROJECTS_ROOT", "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / "Documents" / "Projects"
    )
    return root.resolve()


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


def _is_allowed_link_path(path: Path) -> bool:
    return bool(_link_path_scope(path))


def _should_write_folder_task_metadata(folder: Path) -> bool:
    return _link_path_scope(folder) == "task"


def _resolve_linked_folder(path: str) -> Path:
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise SystemExit(f"not a directory: {folder}")
    if not _is_allowed_link_path(folder):
        roots = _linked_folder_roots()
        allowed = ", ".join(str(r) for r in roots) or "(none found)"
        raise SystemExit(f"folder must be under one of: {allowed}")
    return folder


def _resolve_linked_file(path: str) -> Path:
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise SystemExit(f"not a file: {file_path}")
    if not _is_allowed_link_path(file_path):
        roots = _linked_folder_roots()
        allowed = ", ".join(str(r) for r in roots) or "(none found)"
        raise SystemExit(f"file must be under one of: {allowed}")
    return file_path


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


def _normalize_linked_folders(raw) -> list[dict]:
    items = raw if isinstance(raw, list) else []
    out: list[dict] = []
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
        rec = {"path": path, "label": label or Path(path).name, "type": item_type}
        if created_at:
            rec["created_at"] = created_at
        out.append(rec)
    return out


def _add_linked_item(container: dict, linked_path: Path,
                     label: str, item_type: str) -> bool:
    path = str(linked_path)
    if item_type == "url":
        path = _coerce_linked_url(path)
        label = label or _default_linked_url_label(path)
    else:
        label = label or linked_path.name
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


def _add_linked_folder(container: dict, folder: Path, label: str) -> bool:
    return _add_linked_item(container, folder, label, "folder")


def _add_linked_file(container: dict, file_path: Path, label: str) -> bool:
    return _add_linked_item(container, file_path, label, "file")


def _add_linked_url(container: dict, url: str, label: str) -> bool:
    return _add_linked_item(container, url, label, "url")


def _link_context(meta_path: Path, kind: str, task: str,
                  container: dict) -> dict[str, str]:
    run_dir = meta_path.parent
    task_name = task or str(container.get("name") or run_dir.name)
    derived_run_id = f"{run_dir.name}::{task_name}"
    return {
        "run_id": str(container.get("run_id") or derived_run_id or os.environ.get("ORCH_RUN_ID") or ""),
        "run_name": run_dir.name,
        "task": task_name,
        "agent": str(container.get("agent") or os.environ.get("ORCH_AGENT_TYPE") or ""),
        "tmux_session": str(container.get("tmux_session") or os.environ.get("ORCH_TMUX_SESSION") or ""),
        "cwd": str(container.get("cwd") or ""),
        "metadata_path": str(meta_path),
        "kind": kind,
    }


def _update_folder_task_metadata(folder: Path, context: dict[str, str],
                                 label: str) -> tuple[Path, str]:
    meta_dir = folder / ".orch"
    meta_path = meta_dir / "task.json"
    existing = _read_json(meta_path) if meta_path.exists() else {}
    data = existing if isinstance(existing, dict) else {}
    now = _iso_now()
    run_id = context.get("run_id", "")
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

    meta_dir.mkdir(parents=True, exist_ok=True)
    _write_json(meta_path, data)

    warning = ""
    owner = str(data.get("created_by_run_id") or "")
    if owner and run_id and owner != run_id:
        warning = (
            f"folder was created by run_id {owner}; "
            f"current link is from run_id {run_id}. "
            "Create a sibling subtask folder unless you are intentionally continuing this one."
        )
    return meta_path, warning


def _current_tmux_session() -> str:
    env_session = os.environ.get("ORCH_TMUX_SESSION", "").strip()
    if env_session:
        return env_session
    if not os.environ.get("TMUX"):
        return ""
    try:
        out = subprocess.run(["tmux", "display-message", "-p", "#S"],
                             capture_output=True, text=True, timeout=2)
        if out.returncode == 0:
            return out.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""
    return ""


def _session_candidates(session: str) -> list[str]:
    session = (session or "").strip()
    if not session:
        return []
    candidates = [session]
    if session.endswith("-web"):
        candidates.append(session[:-4])
    return candidates


def _find_metadata_for_session(outputs_dir: Path, session: str):
    if not session or not outputs_dir.is_dir():
        return None, "", ""
    sessions = set(_session_candidates(session))
    for run_dir in sorted(outputs_dir.iterdir(),
                          key=lambda p: p.stat().st_mtime if p.exists() else 0,
                          reverse=True):
        sess_file = run_dir / "session.json"
        if sess_file.exists():
            data = _read_json(sess_file)
            if data.get("tmux_session") in sessions:
                return sess_file, "run", ""
        state_file = run_dir / "state.json"
        if state_file.exists():
            data = _read_json(state_file)
            for task, st in data.items():
                if isinstance(st, dict) and st.get("tmux_session") in sessions:
                    return state_file, "task", task
    return None, "", ""


def _resolve_link_target(args) -> tuple[Path, str, str]:
    if args.run_dir:
        run_dir = Path(args.run_dir).expanduser().resolve()
        sess_file = run_dir / "session.json"
        if sess_file.exists():
            return sess_file, "run", ""
        state_file = run_dir / "state.json"
        if state_file.exists() and args.task:
            return state_file, "task", args.task
        raise SystemExit(f"no session.json/state.json found in {run_dir}")

    env_json = os.environ.get("ORCH_SESSION_JSON", "").strip()
    if env_json:
        path = Path(env_json).expanduser().resolve()
        if path.exists():
            if path.name == "state.json":
                task = os.environ.get("ORCH_TASK_NAME", "").strip()
                if task:
                    return path, "task", task
            return path, "run", ""

    env_run_dir = os.environ.get("ORCH_RUN_DIR", "").strip()
    if env_run_dir:
        path = Path(env_run_dir).expanduser().resolve() / "session.json"
        if path.exists():
            return path, "run", ""

    session = args.session or _current_tmux_session()
    outputs_dir = Path(args.outputs).expanduser().resolve()
    path, kind, task = _find_metadata_for_session(outputs_dir, session)
    if path is not None:
        return path, kind, task

    tried = ", ".join(_session_candidates(session)) or "(none)"
    raise SystemExit(
        "could not find current orchestrator session; "
        f"tried tmux session(s): {tried}; "
        "pass --run-dir or --session"
    )


def cmd_link_folder(args):
    folder = _resolve_linked_folder(args.folder)
    label = (args.label or folder.name).strip()
    meta_path, kind, task = _resolve_link_target(args)
    data = _read_json(meta_path)
    if kind == "task":
        if task not in data or not isinstance(data[task], dict):
            raise SystemExit(f"task '{task}' not found in {meta_path}")
        changed = _add_linked_folder(data[task], folder, label)
        context = _link_context(meta_path, kind, task, data[task])
    else:
        changed = _add_linked_folder(data, folder, label)
        context = _link_context(meta_path, kind, task, data)
    _write_json(meta_path, data)
    task_meta_path = None
    warning = ""
    if _should_write_folder_task_metadata(folder):
        task_meta_path, warning = _update_folder_task_metadata(folder, context, label)
    status = "linked" if changed else "already linked"
    print(f"{status}: {folder}")
    print(f"metadata: {meta_path}")
    if task_meta_path is not None:
        print(f"task metadata: {task_meta_path}")
    else:
        print("task metadata: skipped for project/worktree folder")
    if warning:
        print(f"warning: {warning}", file=sys.stderr)


def cmd_link_file(args):
    file_path = _resolve_linked_file(args.file)
    label = (args.label or file_path.name).strip()
    meta_path, kind, task = _resolve_link_target(args)
    data = _read_json(meta_path)
    if kind == "task":
        if task not in data or not isinstance(data[task], dict):
            raise SystemExit(f"task '{task}' not found in {meta_path}")
        changed = _add_linked_file(data[task], file_path, label)
    else:
        changed = _add_linked_file(data, file_path, label)
    _write_json(meta_path, data)
    status = "linked" if changed else "already linked"
    print(f"{status}: {file_path}")
    print(f"metadata: {meta_path}")


def cmd_link_url(args):
    try:
        url = _coerce_linked_url(args.url)
    except ValueError as exc:
        raise SystemExit(str(exc))
    label = (args.label or _default_linked_url_label(url)).strip()
    meta_path, kind, task = _resolve_link_target(args)
    data = _read_json(meta_path)
    if kind == "task":
        if task not in data or not isinstance(data[task], dict):
            raise SystemExit(f"task '{task}' not found in {meta_path}")
        changed = _add_linked_url(data[task], url, label)
    else:
        changed = _add_linked_url(data, url, label)
    _write_json(meta_path, data)
    status = "linked" if changed else "already linked"
    print(f"{status}: {url}")
    print(f"metadata: {meta_path}")


def cmd_url(args):
    """Print the current best dashboard URL (prefers VPN/tunnel IPs over home
    LAN) and optionally copy it to the macOS clipboard. Handy when the Mac
    hops between WiFi / VPN and you want to grab the working URL fast."""
    token = args.token or dashboard_token()
    port = args.port
    detected_scheme = ""
    if args.https is None:
        detected_scheme = detect_dashboard_scheme(port)
        scheme = detected_scheme or ("https" if token else "http")
    else:
        scheme = "https" if args.https else "http"

    bind_host = "0.0.0.0" if token else "127.0.0.1"
    best = pick_best_ip(bind_host)
    best_url = build_access_url(best, port, scheme, token) if best else ""
    interfaces = (
        list_local_ipv4()
        if token
        else [("loopback", "127.0.0.1")]
    )

    if args.json:
        payload = {
            "best_url": best_url,
            "best_ip": best,
            "port": port,
            "scheme": scheme,
            "scheme_detected": bool(detected_scheme),
            "candidates": [
                {"iface": iface, "ip": ip,
                 "url": build_access_url(ip, port, scheme, token)}
                for iface, ip in interfaces
            ],
        }
        print(json.dumps(payload, indent=2))
        return

    if not best_url:
        print("(no reachable interface found)")
        sys.exit(1)

    if not args.quiet:
        print(best_url)
        print()
        print("All candidates:")
        for iface, ip in interfaces:
            print(f"  [{iface:10s}] {build_access_url(ip, port, scheme, token)}")

    # Copy to macOS clipboard unless explicitly disabled.
    if not args.no_copy and shutil.which("pbcopy"):
        try:
            proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            proc.communicate(input=best_url.encode("utf-8"))
            if proc.returncode == 0 and not args.quiet:
                print()
                print("✓ copied to clipboard")
        except Exception as exc:
            if not args.quiet:
                print(f"(clipboard copy failed: {exc})")
    elif args.quiet:
        # --quiet means "just print the URL with nothing else, for scripting".
        print(best_url)


def cmd_dashboard(args):
    """Launch the web dashboard (delegates to dashboard.py)."""
    import dashboard as dash_mod

    try:
        require_dashboard_auth(args.host, args.token)
    except ValueError as exc:
        raise SystemExit(str(exc))

    outputs_dir = (
        Path(args.outputs).expanduser().resolve()
        if args.outputs
        else _configured_outputs_root()
    )
    outputs_dir.mkdir(parents=True, exist_ok=True)

    try:
        import uvicorn
    except ImportError:
        print("uvicorn not installed. Run: pip install -r requirements.txt")
        sys.exit(1)

    ssl_kwargs = {}
    use_https = getattr(args, "https", False) or bool(
        getattr(args, "certfile", "") or getattr(args, "keyfile", "")
    )
    cert_path = key_path = None
    if use_https:
        if getattr(args, "certfile", "") and getattr(args, "keyfile", ""):
            cert_path, key_path = Path(args.certfile), Path(args.keyfile)
        else:
            cert_path, key_path = dash_mod._ensure_self_signed_cert(
                outputs_dir.parent / ".dashboard-certs"
            )
        ssl_kwargs = {"ssl_certfile": str(cert_path), "ssl_keyfile": str(key_path)}
    scheme = "https" if use_https else "http"

    ttyd_host = args.ttyd_host or ("127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host)
    publish_icloud = getattr(args, "publish_icloud", False)
    app = dash_mod.create_app(outputs_dir, token=args.token,
                              ttyd_enabled=args.ttyd, ttyd_host=ttyd_host,
                              bind_host=args.host, port=args.port, scheme=scheme,
                              publish_icloud=publish_icloud)

    print(f"Outputs:  {outputs_dir}")
    print(f"Listen:   {scheme}://{args.host}:{args.port}")
    if use_https:
        print(f"TLS:      cert={cert_path}")
        print(f"          key ={key_path}")
        print("          (self-signed — accept the browser warning once)")
    if args.token:
        print(f"Token:    {args.token}  (append ?token=... or send Authorization: Bearer ...)")
    else:
        print("Token:    (none — open access; pass --token or set $ORCH_DASHBOARD_TOKEN to lock)")
    if args.ttyd:
        print("ttyd:     enabled; browsers connect via the dashboard's same-origin proxy")
    best = dash_mod.pick_best_ip(args.host)
    if best:
        print(f"Phone:    {dash_mod.build_access_url(best, args.port, scheme, args.token)}")
    if publish_icloud and getattr(app.state, "icloud_file", None):
        print(f"iCloud:   {app.state.icloud_file}")
    print()

    # Defense in depth against "ttyd orphan" scenarios. uvicorn normally
    # runs stop_all() via the `finally:` below, but:
    #   - SIGKILL skips all cleanup (nothing we can do there — but the next
    #     dashboard startup will sweep PPID=1 ttyd orphans on its own);
    #   - SIGTERM sometimes gets stuck waiting for lingering WebSocket
    #     connections to drain and uvicorn forces exit without running the
    #     `finally:` body. Register both an atexit hook and an explicit
    #     SIGTERM/SIGINT handler so stop_all always gets a chance to run.
    import atexit
    import signal

    def _cleanup(*_args):
        try:
            app.state.ttyd.stop_all()
        except Exception:
            pass

    atexit.register(_cleanup)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_a: (_cleanup(), sys.exit(0)))
        except (ValueError, OSError):
            pass

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info",
                    **ssl_kwargs)
    finally:
        _cleanup()


def main():
    parser = argparse.ArgumentParser(description="Agent Orchestrator")
    sub = parser.add_subparsers(dest="command")

    p_start = sub.add_parser("start", help="Start tasks from a YAML config")
    p_start.add_argument("config", help="Path to task YAML file")
    p_start.set_defaults(func=cmd_start)

    p_resume = sub.add_parser("resume", help="Resume monitoring on an existing output dir")
    p_resume.add_argument("output_dir", help="Path to output directory with state.json")
    p_resume.set_defaults(func=cmd_resume)

    p_status = sub.add_parser("status", help="Show task status")
    p_status.add_argument("config", nargs="?", help="Task YAML (to resolve output dir)")
    p_status.add_argument("--dir", default=".", help="Output directory (alternative)")
    p_status.set_defaults(func=cmd_status)

    p_attach = sub.add_parser("attach", help="Attach to a task's tmux session")
    p_attach.add_argument("task", help="Task name")
    p_attach.add_argument("config", nargs="?", help="Task YAML")
    p_attach.add_argument("--dir", default=".", help="Output directory")
    p_attach.set_defaults(func=cmd_attach)

    p_skip = sub.add_parser("skip", help="Mark a task as completed")
    p_skip.add_argument("task", help="Task name")
    p_skip.add_argument("config", nargs="?", help="Task YAML")
    p_skip.add_argument("--dir", default=".", help="Output directory")
    p_skip.set_defaults(func=cmd_skip)

    p_continue = sub.add_parser("continue", help="Re-attach watcher to a live tmux session")
    p_continue.add_argument("session", nargs="?", default="", help="Tmux session name (or partial match); omit to list/select")
    p_continue.add_argument("--prompt", "-p", default="", help="Send a continuation prompt to the agent")
    p_continue.add_argument("--no-attach", action="store_true", help="Run watcher in background without attaching")
    p_continue.set_defaults(func=cmd_continue)

    p_organize = sub.add_parser("organize", help="Classify unarchived sessions into projects, then prune")
    p_organize.add_argument("agent", nargs="?", default="cursor", help="Agent type: cursor, claude (default: cursor)")
    p_organize.add_argument("--stale", "-s", default="",
                            help="Only organize sessions not updated for this duration "
                                 "(e.g. '3h', '30m', '1d'). Omit to organize all inactive sessions.")
    p_organize.set_defaults(func=cmd_organize)

    p_clean = sub.add_parser(
        "clean", help="Stop Agent Orchestrator tmux sessions"
    )
    p_clean.add_argument(
        "-f", "--force", action="store_true",
        help="retained for compatibility; sessions remain scoped to orch-*",
    )
    p_clean.set_defaults(func=cmd_clean)

    p_prune = sub.add_parser("prune", help="Move empty and already-archived session dirs to Trash")
    p_prune.add_argument("--dry-run", action="store_true", help="Show what would be moved without moving")
    p_prune.add_argument("--protect", action="append", default=[],
                         help="Output directory name to keep even if it looks prunable")
    p_prune.set_defaults(func=cmd_prune)

    p_link = sub.add_parser("link-folder", help="Link a project, worktree, or task folder to the current session")
    p_link.add_argument("folder", help="Project/worktree/task folder to show in the dashboard")
    p_link.add_argument("--label", default="", help="Display label (default: folder name)")
    p_link.add_argument("--run-dir", default="", help="Explicit output run directory")
    p_link.add_argument("--task", default="", help="Task key when --run-dir points at state.json")
    p_link.add_argument("--session", default="", help="Explicit tmux session name")
    p_link.add_argument("--outputs", default=str(_configured_outputs_root()),
                        help="outputs directory to scan when inferring by tmux session "
                             "(default: $ORCH_OUTPUTS_DIR or <orch>/outputs)")
    p_link.set_defaults(func=cmd_link_folder)

    p_link_file = sub.add_parser("link-file", help="Link a file to the current session")
    p_link_file.add_argument("file", help="File to show in the dashboard Files panel")
    p_link_file.add_argument("--label", default="", help="Display label (default: file name)")
    p_link_file.add_argument("--run-dir", default="", help="Explicit output run directory")
    p_link_file.add_argument("--task", default="", help="Task key when --run-dir points at state.json")
    p_link_file.add_argument("--session", default="", help="Explicit tmux session name")
    p_link_file.add_argument("--outputs", default=str(_configured_outputs_root()),
                             help="outputs directory to scan when inferring by tmux session "
                                  "(default: $ORCH_OUTPUTS_DIR or <orch>/outputs)")
    p_link_file.set_defaults(func=cmd_link_file)

    p_link_url = sub.add_parser("link-url", help="Link a URL to the current session")
    p_link_url.add_argument("url", help="HTTP(S) URL to show in the dashboard Files panel")
    p_link_url.add_argument("--label", default="", help="Display label (default: host/path)")
    p_link_url.add_argument("--run-dir", default="", help="Explicit output run directory")
    p_link_url.add_argument("--task", default="", help="Task key when --run-dir points at state.json")
    p_link_url.add_argument("--session", default="", help="Explicit tmux session name")
    p_link_url.add_argument("--outputs", default=str(_configured_outputs_root()),
                            help="outputs directory to scan when inferring by tmux session "
                                 "(default: $ORCH_OUTPUTS_DIR or <orch>/outputs)")
    p_link_url.set_defaults(func=cmd_link_url)

    p_dash = sub.add_parser("dashboard", help="Launch the web dashboard (view/control sessions from browser)")
    p_dash.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1). Non-loopback "
                             "addresses require --token or $ORCH_DASHBOARD_TOKEN.")
    p_dash.add_argument("--port", type=int, default=7860)
    p_dash.add_argument("--outputs", default="",
                        help="path to outputs/ dir "
                             "(default: $ORCH_OUTPUTS_DIR or <orch>/outputs)")
    p_dash.add_argument("--token", default=dashboard_token(),
                        help="shared secret for auth; reads $ORCH_DASHBOARD_TOKEN "
                             "or the local token cache")
    p_dash.add_argument("--https", action="store_true",
                        help="serve over HTTPS with an auto-generated self-signed cert "
                             "(needed for the browser clipboard API over LAN/tunnel)")
    p_dash.add_argument("--certfile", default="",
                        help="path to TLS cert (implies HTTPS; pair with --keyfile)")
    p_dash.add_argument("--keyfile", default="",
                        help="path to TLS key (implies HTTPS; pair with --certfile)")
    p_dash.add_argument("--publish-icloud", action="store_true",
                        help="write current dashboard URL to iCloud Drive "
                             "(~/iCloud Drive/orch-dashboard.txt) so your phone "
                             "can always find the latest address")
    p_dash.add_argument("--ttyd", action="store_true", default=True,
                        help="enable ttyd for full-pty interaction in browser (default: on; "
                             "requires `ttyd` on PATH)")
    p_dash.add_argument("--no-ttyd", dest="ttyd", action="store_false",
                        help="disable ttyd; the dashboard then only shows plain-log views "
                             "(useful if ttyd isn't installed or for CI/headless tests)")
    p_dash.add_argument("--ttyd-host", default="",
                        help="hostname browsers should use to reach ttyd (defaults to --host)")
    p_dash.set_defaults(func=cmd_dashboard)

    p_url = sub.add_parser("url", help="Print current dashboard URL (auto-picks VPN/LAN) and copy to clipboard")
    p_url.add_argument("--port", type=int, default=7860,
                       help="dashboard port (default: 7860)")
    p_url.add_argument("--token", default="",
                       help="token to embed in URL; falls back to the environment "
                            "or local token cache")
    p_url.add_argument("--https", action="store_true", default=None,
                       help="force https:// instead of auto-detecting the live dashboard")
    p_url.add_argument("--no-https", dest="https", action="store_false",
                       help="force http:// instead of auto-detecting the live dashboard")
    p_url.add_argument("--no-copy", action="store_true",
                       help="don't copy to clipboard, just print")
    p_url.add_argument("-q", "--quiet", action="store_true",
                       help="print only the URL (for shell scripting / piping)")
    p_url.add_argument("--json", action="store_true",
                       help="emit JSON with all candidates")
    p_url.set_defaults(func=cmd_url)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
