"""Conflict-aware rsync transfer planning without automatic deletions."""

from __future__ import annotations

import shlex
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


class TransferCancelled(RuntimeError):
    """Raised when the user cancels an in-flight workspace transfer."""


@dataclass
class TransferPlan:
    push: list[str] = field(default_factory=list)
    pull: list[str] = field(default_factory=list)
    busy: list[str] = field(default_factory=list)
    deletions: list[str] = field(default_factory=list)
    git_refs: list[str] = field(default_factory=list)
    too_large: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> int:
        return len(self.push) + len(self.pull)

    def summary(self) -> dict[str, Any]:
        return {
            "push": len(self.push),
            "pull": len(self.pull),
            "busy": len(self.busy),
            "deletions": len(self.deletions),
            "git_refs": len(self.git_refs),
            "too_large": len(self.too_large),
            "conflicts": len(self.conflicts),
        }


def _under(path: str, roots: Iterable[str]) -> bool:
    return any(not root or path == root
               or path.startswith(root.rstrip("/") + "/") for root in roots)


def build_transfer_plan(changes: Iterable[dict[str, Any]], *,
                        local_busy: Iterable[str] = (),
                        remote_busy: Iterable[str] = (),
                        max_file_bytes: int = 512 * 1024 * 1024) -> TransferPlan:
    plan = TransferPlan()
    local_busy = tuple(local_busy)
    remote_busy = tuple(remote_busy)
    for item in changes:
        path = str(item.get("path") or "")
        state = item.get("state")
        if not path:
            continue
        if state == "conflict":
            plan.conflicts.append(path)
            continue
        if state not in {"local_only", "remote_only"}:
            continue
        if item.get("kind") == "git-head" or "/.git/" in f"/{path}/":
            plan.git_refs.append(path)
            continue
        source_present = (
            item.get("local_present") if state == "local_only"
            else item.get("remote_present")
        )
        if not source_present:
            plan.deletions.append(path)
            continue
        if int(item.get("size") or 0) > max_file_bytes:
            plan.too_large.append(path)
            continue
        if _under(path, local_busy) or _under(path, remote_busy):
            plan.busy.append(path)
            continue
        target = plan.push if state == "local_only" else plan.pull
        target.append(path)
    return plan


class WorkspaceTransfer:
    def __init__(self, *, local_root: Path, remote_root: str,
                 remote_host: str, excludes: Iterable[str],
                 timeout_seconds: float = 3600.0):
        self.local_root = local_root
        self.remote_root = remote_root
        self.remote_host = remote_host
        self.excludes = tuple(excludes)
        self.timeout_seconds = timeout_seconds
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._active_proc: Optional[subprocess.Popen] = None

    def reset_cancel(self) -> None:
        self._cancelled.clear()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            proc = self._active_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def remote_active_paths(self) -> list[str]:
        if not self.remote_host:
            return []
        quoted_root = shlex.quote(self.remote_root)
        remote_command = (
            f"root=$(realpath -- {quoted_root} 2>/dev/null || "
            f"printf '%s' {quoted_root}); "
            "printf '__ORCH_ROOT__\\t%s\\n' \"$root\"; "
            "tmux list-panes -a -F "
            "'#{session_name}\\t#{pane_current_path}\\t#{pane_dead}' | "
            "while IFS=\"$(printf '\\t')\" read -r session cwd dead; do "
            "git_root=$(git -C \"$cwd\" rev-parse --show-toplevel "
            "2>/dev/null || true); "
            "printf '%s\\t%s\\t%s\\t%s\\n' "
            "\"$session\" \"$cwd\" \"$dead\" \"$git_root\"; done"
        )
        command = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            self.remote_host, remote_command,
        ]
        if self._cancelled.is_set():
            raise TransferCancelled("sync cancelled")
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        with self._lock:
            self._active_proc = proc
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            raise RuntimeError("could not inspect remote tmux sessions: timed out")
        finally:
            with self._lock:
                if self._active_proc is proc:
                    self._active_proc = None
        if self._cancelled.is_set():
            raise TransferCancelled("sync cancelled")
        if proc.returncode:
            detail = stderr.strip()
            raise RuntimeError(detail or "could not inspect remote tmux sessions")
        roots = [Path(self.remote_root)]
        paths = set()
        for line in stdout.splitlines():
            fields = line.split("\t")
            if len(fields) == 2 and fields[0] == "__ORCH_ROOT__":
                resolved = fields[1].strip()
                if resolved:
                    roots.append(Path(resolved))
                continue
            if len(fields) != 4:
                continue
            session, cwd, dead, git_root = fields
            if (not session.startswith("orch-") or session.endswith("-web")
                    or dead == "1"):
                continue
            rel = None
            for candidate in (git_root, cwd):
                if not candidate:
                    continue
                for root in roots:
                    try:
                        rel = Path(candidate).relative_to(root).as_posix()
                        break
                    except ValueError:
                        continue
                if rel is not None:
                    break
            if rel is None:
                continue
            # A generic Projects-root cwd does not mean every project is
            # being modified. Scoped sync only blocks on a concrete remote
            # project cwd; remote Linked Items are not available here.
            if rel != ".":
                paths.add(rel)
        return sorted(paths)

    def execute(self, plan: TransferPlan) -> dict[str, Any]:
        if self._cancelled.is_set():
            raise TransferCancelled("sync cancelled")
        dry_run = []
        transferred = []
        if plan.push:
            dry_run.extend(self._run("push", plan.push, dry_run=True))
            transferred.extend(self._run("push", plan.push, dry_run=False))
        if plan.pull:
            dry_run.extend(self._run("pull", plan.pull, dry_run=True))
            transferred.extend(self._run("pull", plan.pull, dry_run=False))
        return {
            "ok": True,
            "plan": plan.summary(),
            "dry_run_items": len(dry_run),
            "transferred_items": len(transferred),
            "dry_run_preview": dry_run[:100],
            "transfer_preview": transferred[:100],
        }

    def _run(self, direction: str, paths: Iterable[str], *,
             dry_run: bool) -> list[str]:
        selected = sorted(set(paths))
        if not selected:
            return []
        if self._cancelled.is_set():
            raise TransferCancelled("sync cancelled")
        with tempfile.NamedTemporaryFile() as manifest:
            manifest.write(b"\0".join(path.encode() for path in selected) + b"\0")
            manifest.flush()
            command = [
                "rsync", "-azR", "--from0", f"--files-from={manifest.name}",
                "--partial", "--partial-dir=.rsync-partial",
                "--itemize-changes",
            ]
            if dry_run:
                command.append("--dry-run")
            for pattern in self.excludes:
                command.append(f"--exclude={pattern}")
            if self.remote_host:
                command.extend([
                    "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10 "
                    "-o ServerAliveInterval=15 -o ServerAliveCountMax=6",
                ])
            local = str(self.local_root) + "/"
            remote = (
                f"{self.remote_host}:{self.remote_root.rstrip('/')}/"
                if self.remote_host else str(Path(self.remote_root)) + "/"
            )
            command.extend([local, remote] if direction == "push" else [remote, local])
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            with self._lock:
                self._active_proc = proc
            try:
                stdout, stderr = proc.communicate(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                raise RuntimeError(
                    f"rsync {direction} timed out after "
                    f"{self.timeout_seconds:g}s"
                )
            finally:
                with self._lock:
                    if self._active_proc is proc:
                        self._active_proc = None
        if self._cancelled.is_set():
            raise TransferCancelled("sync cancelled")
        result_returncode = proc.returncode
        if result_returncode:
            detail = stderr.strip()
            raise RuntimeError(detail or f"rsync {direction} exited {result_returncode}")
        return [line for line in stdout.splitlines() if line.strip()]
