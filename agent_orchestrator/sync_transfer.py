"""Conflict-aware rsync transfer planning without automatic deletions."""

from __future__ import annotations

import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


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

    def remote_active_paths(self) -> list[str]:
        if not self.remote_host:
            return []
        quoted_root = shlex.quote(self.remote_root)
        remote_command = (
            f"root=$(realpath -- {quoted_root} 2>/dev/null || "
            f"printf '%s' {quoted_root}); "
            "printf '__ORCH_ROOT__\\t%s\\n' \"$root\"; "
            "tmux list-panes -a -F "
            "'#{session_name}\\t#{pane_current_path}\\t#{pane_dead}'"
        )
        command = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            self.remote_host, remote_command,
        ]
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=30, check=False,
        )
        if result.returncode:
            detail = result.stderr.strip()
            raise RuntimeError(detail or "could not inspect remote tmux sessions")
        roots = [Path(self.remote_root)]
        paths = set()
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) == 2 and fields[0] == "__ORCH_ROOT__":
                resolved = fields[1].strip()
                if resolved:
                    roots.append(Path(resolved))
                continue
            if len(fields) != 3:
                continue
            session, cwd, dead = fields
            if (not session.startswith("orch-") or session.endswith("-web")
                    or dead == "1"):
                continue
            rel = None
            for root in roots:
                try:
                    rel = Path(cwd).relative_to(root).as_posix()
                    break
                except ValueError:
                    continue
            if rel is None:
                continue
            paths.add("" if rel == "." else rel)
        return sorted(paths)

    def execute(self, plan: TransferPlan) -> dict[str, Any]:
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
            result = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=self.timeout_seconds, check=False,
            )
        if result.returncode:
            detail = result.stderr.strip()
            raise RuntimeError(detail or f"rsync {direction} exited {result.returncode}")
        return [line for line in result.stdout.splitlines() if line.strip()]
