"""Read-only, conflict-aware status for a pair of workspace trees.

The service intentionally does not copy, delete, or overwrite project files.
It maintains a persistent comparison baseline and uses filesystem events to
refresh only paths that changed locally.  A low-frequency full scan catches
missed events and refreshes the remote side over SSH.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Optional


DEFAULT_EXCLUDES = (
    ".DS_Store",
    ".rsync-partial/",
    ".git/lfs/objects/",
    "*.sock",
    "*.lock",
    "__pycache__/",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state_home() -> Path:
    configured = os.environ.get("XDG_STATE_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state"


def _clean_relative(value: str) -> str:
    value = value.strip().replace("\\", "/").strip("/")
    path = PurePosixPath(value or ".")
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"sync path must be relative: {value!r}")
    return "" if str(path) == "." else str(path)


def _read_list_file(path: Path) -> list[str]:
    try:
        lines = path.expanduser().read_text().splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines
            if line.strip() and not line.lstrip().startswith("#")]


@dataclass(frozen=True)
class SyncSettings:
    enabled: bool = False
    local_root: str = ""
    remote_host: str = ""
    remote_root: str = ""
    label: str = "Remote"
    paths: tuple[str, ...] = ()
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES
    scan_interval_seconds: float = 21600.0
    debounce_seconds: float = 30.0
    ssh_timeout_seconds: float = 120.0
    remote_python: str = "python3"
    remote_code_root: str = ""
    state_db: str = ""

    @property
    def local_path(self) -> Path:
        return Path(os.path.expandvars(self.local_root)).expanduser().resolve()

    @property
    def database_path(self) -> Path:
        if self.state_db:
            return Path(os.path.expandvars(self.state_db)).expanduser()
        return _state_home() / "agent-orchestrator" / "sync-status.sqlite3"


def settings_from_dict(data: dict[str, Any], *, config_dir: Path) -> SyncSettings:
    raw = data.get("sync_status")
    if not isinstance(raw, dict):
        return SyncSettings()

    def config_path(value: Any) -> Optional[Path]:
        text = str(value or "").strip()
        if not text:
            return None
        path = Path(os.path.expandvars(text)).expanduser()
        return path if path.is_absolute() else config_dir / path

    paths: list[str] = []
    for value in raw.get("paths", []):
        paths.append(_clean_relative(str(value)))
    for value in raw.get("manifest_files", []):
        path = config_path(value)
        if path:
            if not path.is_file():
                raise ValueError(f"sync manifest does not exist: {path}")
            paths.extend(_clean_relative(item) for item in _read_list_file(path))

    excludes = list(DEFAULT_EXCLUDES)
    excludes.extend(str(value).strip() for value in raw.get("excludes", [])
                    if str(value).strip())
    for value in raw.get("exclude_files", []):
        path = config_path(value)
        if path:
            if not path.is_file():
                raise ValueError(f"sync exclude file does not exist: {path}")
            excludes.extend(_read_list_file(path))

    enabled = bool(raw.get("enabled", False))
    local_root = str(raw.get("local_root", "")).strip()
    remote_root = str(raw.get("remote_root", "")).strip()
    if enabled and (not local_root or not remote_root):
        raise ValueError(
            "sync_status requires local_root and remote_root when enabled"
        )
    return SyncSettings(
        enabled=enabled,
        local_root=local_root,
        remote_host=str(raw.get("remote_host", "")).strip(),
        remote_root=remote_root,
        label=str(raw.get("label", "Remote")).strip() or "Remote",
        paths=tuple(dict.fromkeys(paths)),
        excludes=tuple(dict.fromkeys(excludes)),
        scan_interval_seconds=max(
            300.0, float(raw.get("scan_interval_seconds", 21600.0))
        ),
        debounce_seconds=max(1.0, float(raw.get("debounce_seconds", 30.0))),
        ssh_timeout_seconds=max(
            10.0, float(raw.get("ssh_timeout_seconds", 120.0))
        ),
        remote_python=str(raw.get("remote_python", "python3")).strip()
        or "python3",
        remote_code_root=str(raw.get("remote_code_root", "")).strip(),
        state_db=str(raw.get("state_db", "")).strip(),
    )


def load_settings(config_path: Path) -> SyncSettings:
    try:
        data = json.loads(config_path.expanduser().read_text())
    except (OSError, json.JSONDecodeError):
        return SyncSettings()
    if not isinstance(data, dict):
        return SyncSettings()
    return settings_from_dict(data, config_dir=config_path.expanduser().parent)


@dataclass(frozen=True)
class FileRecord:
    path: str
    kind: str
    size: int
    mtime_ns: int
    digest: str = ""


def _matches_exclude(rel: str, is_dir: bool, patterns: Iterable[str]) -> bool:
    if PurePosixPath(rel).name == ".env.example":
        return False
    rel = rel.strip("/")
    name = PurePosixPath(rel).name
    parts = PurePosixPath(rel).parts
    for raw in patterns:
        pattern = raw.strip().replace("\\", "/")
        if not pattern or pattern.startswith("#"):
            continue
        rooted = pattern.startswith("/")
        directory = pattern.endswith("/")
        pattern = pattern.strip("/")
        if not pattern:
            continue
        if directory:
            if rooted:
                if (rel == pattern or rel.startswith(pattern + "/")
                        or f"/{pattern}/" in f"/{rel}/"):
                    return True
            elif any(fnmatch.fnmatchcase(part, pattern) for part in parts):
                return True
            continue
        candidate = rel if rooted or "/" in pattern else name
        if fnmatch.fnmatchcase(candidate, pattern):
            return True
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_dir(worktree: Path) -> Optional[Path]:
    marker = worktree / ".git"
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    try:
        value = marker.read_text().strip()
    except OSError:
        return None
    if not value.startswith("gitdir:"):
        return None
    path = Path(value.split(":", 1)[1].strip())
    return (worktree / path).resolve() if not path.is_absolute() else path


def _git_head(git_dir: Path) -> str:
    try:
        head = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return ""
    if not head.startswith("ref:"):
        return head
    ref_name = head.split(":", 1)[1].strip()
    try:
        return (git_dir / ref_name).read_text().strip()
    except OSError:
        pass
    try:
        for line in (git_dir / "packed-refs").read_text().splitlines():
            if line and not line.startswith(("#", "^")):
                commit, name = line.split(" ", 1)
                if name == ref_name:
                    return commit
    except (OSError, ValueError):
        pass
    return head


def _git_records(worktree: Path, rel: str) -> Iterator[FileRecord]:
    git_dir = _git_dir(worktree)
    if not git_dir:
        return
    head = _git_head(git_dir)
    if head:
        yield FileRecord(
            path=f"{rel}/.git/HEAD".strip("/"), kind="git-head",
            size=len(head), mtime_ns=0,
            digest=hashlib.sha256(head.encode()).hexdigest(),
        )


def scan_paths(root: Path, paths: Iterable[str],
               excludes: Iterable[str], *,
               digest_files: bool = False) -> Iterator[FileRecord]:
    root = root.expanduser().resolve()
    selected = tuple(dict.fromkeys(_clean_relative(path) for path in paths))
    if not selected:
        selected = ("",)
    seen: set[str] = set()
    for selected_rel in selected:
        start = root / selected_rel
        if not start.exists() and not start.is_symlink():
            continue
        if start.is_dir() and not start.is_symlink():
            for current, dir_names, file_names in os.walk(start):
                current_path = Path(current)
                current_rel = current_path.relative_to(root).as_posix()
                for record in _git_records(current_path, current_rel):
                    if record.path not in seen:
                        seen.add(record.path)
                        yield record
                kept_dirs = []
                for name in dir_names:
                    child_rel = (PurePosixPath(current_rel) / name).as_posix()
                    if name == ".git" or _matches_exclude(
                            child_rel, True, excludes):
                        continue
                    kept_dirs.append(name)
                dir_names[:] = kept_dirs
                for name in file_names:
                    rel = (PurePosixPath(current_rel) / name).as_posix()
                    if (name == ".git" or rel in seen
                            or _matches_exclude(rel, False, excludes)):
                        continue
                    path = current_path / name
                    record = _record_for_path(
                        root, path, rel, digest_file=digest_files
                    )
                    if record:
                        seen.add(rel)
                        yield record
        else:
            rel = selected_rel or start.name
            if rel in seen or _matches_exclude(rel, False, excludes):
                continue
            record = _record_for_path(
                root, start, rel, digest_file=digest_files
            )
            if record:
                seen.add(rel)
                yield record


def _record_for_path(root: Path, path: Path, rel: str, *,
                     digest_file: bool = False) -> Optional[FileRecord]:
    try:
        st = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            return None
        return FileRecord(
            path=rel, kind="symlink", size=len(target),
            mtime_ns=int(st.st_mtime_ns),
            digest=hashlib.sha256(target.encode()).hexdigest(),
        )
    if not stat.S_ISREG(st.st_mode):
        return None
    return FileRecord(
        path=rel, kind="file", size=int(st.st_size),
        mtime_ns=int(st.st_mtime_ns), digest=_sha256(path) if digest_file else "",
    )


class SyncStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS entries (
                side TEXT NOT NULL,
                path TEXT NOT NULL,
                kind TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                digest TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (side, path)
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def replace(self, side: str, records: Iterable[FileRecord]) -> int:
        rows = [(side, record.path, record.kind, record.size,
                 record.mtime_ns, record.digest) for record in records]
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM entries WHERE side = ?", (side,))
            self._conn.executemany(
                "INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?)", rows
            )
        return len(rows)

    def replace_prefix(self, side: str, prefix: str,
                       records: Iterable[FileRecord]) -> int:
        rows = [(side, record.path, record.kind, record.size,
                 record.mtime_ns, record.digest) for record in records]
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM entries WHERE side = ? AND "
                "(path = ? OR path LIKE ? ESCAPE '\\')",
                (side, prefix, escaped + "/%"),
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def copy_side(self, source: str, target: str) -> int:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM entries WHERE side = ?", (target,))
            self._conn.execute(
                """INSERT INTO entries
                   SELECT ?, path, kind, size, mtime_ns, digest
                   FROM entries WHERE side = ?""",
                (target, source),
            )
            count = int(self._conn.execute(
                "SELECT COUNT(*) FROM entries WHERE side = ?", (target,)
            ).fetchone()[0])
        return count

    def merge(self, side: str, records: Iterable[FileRecord]) -> int:
        rows = [(side, record.path, record.kind, record.size,
                 record.mtime_ns, record.digest) for record in records]
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def records(self, side: str) -> dict[str, FileRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, kind, size, mtime_ns, digest "
                "FROM entries WHERE side = ?", (side,)
            ).fetchall()
        return {row[0]: FileRecord(*row) for row in rows}

    def set_meta(self, key: str, value: Any) -> None:
        encoded = json.dumps(value, separators=(",", ":"))
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?, ?)",
                (key, encoded),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default


def _records_equal(left: Optional[FileRecord],
                   right: Optional[FileRecord]) -> bool:
    if left is None or right is None:
        return left is right
    if left.kind != right.kind or left.size != right.size:
        return False
    if left.digest and right.digest:
        return left.digest == right.digest
    # rsync 2.6.x (still shipped by macOS) preserves file times only to whole
    # seconds. Comparing nanoseconds would report every migrated file as
    # changed when the destination filesystem zeroes the fractional part.
    return left.mtime_ns // 1_000_000_000 == right.mtime_ns // 1_000_000_000


def classify_records(local: dict[str, FileRecord],
                     remote: dict[str, FileRecord],
                     baseline: dict[str, FileRecord]) -> dict[str, Any]:
    counts = {
        "unchanged": 0,
        "local_only": 0,
        "remote_only": 0,
        "same_change": 0,
        "conflict": 0,
    }
    changes = []
    for path in sorted(set(local) | set(remote) | set(baseline)):
        local_record = local.get(path)
        remote_record = remote.get(path)
        baseline_record = baseline.get(path)
        local_same = _records_equal(local_record, baseline_record)
        remote_same = _records_equal(remote_record, baseline_record)
        if local_same and remote_same:
            state = "unchanged"
        elif not local_same and remote_same:
            state = "local_only"
        elif local_same and not remote_same:
            state = "remote_only"
        elif _records_equal(local_record, remote_record):
            state = "same_change"
        else:
            state = "conflict"
        counts[state] += 1
        if state != "unchanged":
            record = local_record or remote_record or baseline_record
            changes.append({
                "path": path,
                "state": state,
                "kind": record.kind if record else "file",
                "size": record.size if record else 0,
                "local_present": local_record is not None,
                "remote_present": remote_record is not None,
            })
    return {"counts": counts, "changes": changes}


class _EventHandler:
    def __init__(self, service: "SyncStatusService"):
        self.service = service

    def dispatch(self, event: Any) -> None:
        if getattr(event, "is_directory", False) and event.event_type == "modified":
            return
        self.service.note_path(getattr(event, "src_path", ""))
        self.service.note_path(getattr(event, "dest_path", ""))


class SyncStatusService:
    def __init__(self, settings: SyncSettings):
        self.settings = settings
        self.store: Optional[SyncStore] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._dirty: set[str] = set()
        self._force_full = True
        self._thread: Optional[threading.Thread] = None
        self._observer: Any = None
        self._active_proc: Optional[subprocess.Popen] = None
        self._phase = "disabled" if not settings.enabled else "starting"
        self._last_error = ""
        self._scanning = False
        self._watcher = "disabled" if not settings.enabled else "pending"
        self._cached_status: Optional[dict[str, Any]] = None

    def start(self) -> None:
        if not self.settings.enabled or self._thread is not None:
            return
        self.store = SyncStore(self.settings.database_path)
        self._start_watcher()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="orch-sync-status"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        observer = self._observer
        if observer is not None:
            observer.stop()
            observer.join(timeout=2)
        with self._lock:
            proc = self._active_proc
        if proc is not None and proc.poll() is None:
            proc.kill()
        if self._thread:
            self._thread.join(timeout=5)
        if self._thread and self._thread.is_alive():
            return
        if self.store:
            self.store.close()
        self._thread = None
        self.store = None

    def _start_watcher(self) -> None:
        try:
            from watchdog.observers import Observer
        except ImportError:
            self._watcher = "polling-fallback"
            return
        root = self.settings.local_path
        if not root.is_dir():
            self._watcher = "root-missing"
            return
        observer = Observer()
        watch_paths = []
        for rel in self.settings.paths or ("",):
            candidate = root / rel
            target = candidate if candidate.is_dir() else candidate.parent
            if target.is_dir():
                watch_paths.append(target.resolve())
        collapsed = []
        for target in sorted(set(watch_paths), key=lambda path: len(path.parts)):
            if any(target == parent or parent in target.parents
                   for parent in collapsed):
                continue
            collapsed.append(target)
        for target in collapsed:
            observer.schedule(_EventHandler(self), str(target), recursive=True)
        observer.start()
        self._observer = observer
        self._watcher = "watching"

    def _selected(self, rel: str) -> bool:
        if not self.settings.paths:
            return True
        return any(
            rel == selected or rel.startswith(selected + "/")
            or selected.startswith(rel + "/")
            for selected in self.settings.paths
        )

    def note_path(self, raw_path: str) -> None:
        if not raw_path:
            return
        try:
            rel = Path(raw_path).resolve(strict=False).relative_to(
                self.settings.local_path
            ).as_posix()
        except (OSError, ValueError):
            return
        if not self._selected(rel):
            return
        if "/.git/" in f"/{rel}/":
            worktree = rel.split("/.git/", 1)[0]
            rel = f"{worktree}/.git".strip("/")
        if _matches_exclude(rel, False, self.settings.excludes):
            return
        with self._lock:
            self._dirty.add(rel)
        self._wake.set()

    def request_refresh(self) -> None:
        with self._lock:
            self._force_full = True
        self._wake.set()

    def initialize_baseline(self, source: str = "remote") -> int:
        if source not in {"local", "remote"}:
            raise ValueError("baseline source must be local or remote")
        if not self.store:
            raise RuntimeError("sync status is not running")
        count = self.store.copy_side(source, "baseline")
        if count <= 0:
            raise RuntimeError(f"cannot initialize baseline from empty {source} scan")
        self.store.set_meta("baseline_initialized_at", _utc_now())
        self.store.set_meta("baseline_source", source)
        self._cached_status = self._build_status()
        return count

    def refresh_now(self, *, full: bool = True) -> None:
        if not self.store:
            self.store = SyncStore(self.settings.database_path)
        self._refresh(full=full)

    def _run(self) -> None:
        next_full = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                force_full = self._force_full
                has_dirty = bool(self._dirty)
            if force_full or now >= next_full:
                self._refresh(full=True)
                next_full = time.monotonic() + self.settings.scan_interval_seconds
                continue
            if has_dirty:
                if self._stop.wait(self.settings.debounce_seconds):
                    break
                self._refresh(full=False)
                continue
            timeout = max(1.0, min(60.0, next_full - now))
            self._wake.wait(timeout)
            self._wake.clear()

    def _refresh(self, *, full: bool) -> None:
        if not self.store:
            return
        with self._lock:
            if self._scanning:
                return
            self._scanning = True
            dirty = set(self._dirty)
            self._dirty.clear()
            if full:
                self._force_full = False
        started = time.monotonic()
        try:
            if full:
                local = scan_paths(
                    self.settings.local_path,
                    self.settings.paths,
                    self.settings.excludes,
                )
                self.store.replace("local", local)
                self.store.replace("remote", self._scan_remote())
            else:
                for rel in self._collapse_paths(dirty):
                    if rel == ".git" or rel.endswith("/.git"):
                        worktree_rel = rel[:-5].strip("/")
                        worktree = self.settings.local_path / worktree_rel
                        records = _git_records(worktree, worktree_rel)
                    else:
                        records = scan_paths(
                            self.settings.local_path, (rel,),
                            self.settings.excludes,
                        )
                    self.store.replace_prefix("local", rel, records)
            self._resolve_content_matches()
            self.store.set_meta("last_scan_at", _utc_now())
            self.store.set_meta("last_scan_seconds", round(time.monotonic() - started, 3))
            self._last_error = ""
            self._phase = "ready"
        except Exception as exc:
            self._last_error = str(exc)
            self._phase = "degraded"
            self.store.set_meta("last_error_at", _utc_now())
        finally:
            with self._lock:
                self._scanning = False
            self._cached_status = self._build_status()

    @staticmethod
    def _collapse_paths(paths: Iterable[str]) -> list[str]:
        result = []
        for path in sorted(set(paths), key=lambda item: (item.count("/"), item)):
            if any(path == parent or path.startswith(parent + "/")
                   for parent in result):
                continue
            result.append(path)
        return result

    def _scan_remote(self) -> Iterator[FileRecord]:
        yield from self._scan_remote_paths(self.settings.paths)

    def _scan_remote_paths(self, paths: Iterable[str], *,
                           digest_files: bool = False) -> Iterator[FileRecord]:
        if not self.settings.remote_host:
            yield from scan_paths(
                Path(self.settings.remote_root), paths,
                self.settings.excludes, digest_files=digest_files,
            )
            return
        payload = base64.urlsafe_b64encode(json.dumps({
            "paths": tuple(paths),
            "excludes": self.settings.excludes,
            "digest_files": digest_files,
        }, separators=(",", ":")).encode()).decode()
        scan_command = shlex.join([
            self.settings.remote_python,
            "-m", "agent_orchestrator.sync_status", "scan",
            "--root", self.settings.remote_root,
            "--config-b64", payload,
        ])
        remote_command = scan_command
        if self.settings.remote_code_root:
            remote_command = (
                f"cd {shlex.quote(self.settings.remote_code_root)} && "
                f"{scan_command}"
            )
        command = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            self.settings.remote_host, remote_command,
        ]
        with tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=stderr, text=True,
            )
            with self._lock:
                self._active_proc = proc
            timed_out = threading.Event()

            def terminate() -> None:
                timed_out.set()
                proc.kill()

            timer = threading.Timer(self.settings.ssh_timeout_seconds, terminate)
            timer.start()
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    yield FileRecord(**json.loads(line))
                return_code = proc.wait()
            finally:
                timer.cancel()
                if proc.stdout is not None:
                    proc.stdout.close()
                with self._lock:
                    if self._active_proc is proc:
                        self._active_proc = None
            if return_code:
                stderr.seek(0)
                detail = stderr.read().decode(errors="replace").strip()
                if timed_out.is_set():
                    detail = "remote scan timed out"
                raise RuntimeError(detail or f"remote scan exited {return_code}")

    def _resolve_content_matches(self) -> None:
        """Hash only dual-sided candidates whose metadata cannot decide.

        Most changes are one-sided, so the normal event path never reads file
        contents.  Hashing is reserved for same-size files that otherwise look
        like conflicts because their mtimes differ between hosts.
        """
        if not self.store or not self.store.get_meta("baseline_initialized_at", ""):
            return
        local = self.store.records("local")
        remote = self.store.records("remote")
        baseline = self.store.records("baseline")
        provisional = classify_records(local, remote, baseline)["changes"]
        candidates = []
        for item in provisional:
            if item["state"] not in {"conflict", "same_change"}:
                continue
            left = local.get(item["path"])
            right = remote.get(item["path"])
            if (left and right and left.kind == right.kind == "file"
                    and left.size == right.size):
                candidates.append(item["path"])
        if not candidates:
            return
        local_hashed = scan_paths(
            self.settings.local_path, candidates, self.settings.excludes,
            digest_files=True,
        )
        remote_hashed = self._scan_remote_paths(candidates, digest_files=True)
        self.store.merge("local", local_hashed)
        self.store.merge("remote", remote_hashed)

    def health_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.settings.enabled,
                "phase": self._phase,
                "watcher": self._watcher,
                "scanning": self._scanning,
                "pending_events": len(self._dirty),
                "read_only": True,
            }

    def status(self) -> dict[str, Any]:
        if not self.settings.enabled:
            return {"enabled": False, "phase": "disabled"}
        if not self.store:
            return {
                "enabled": True, "phase": self._phase,
                "watcher": self._watcher, "scanning": False,
                "label": self.settings.label,
            }
        if self._cached_status is None:
            self._cached_status = self._build_status()
        result = dict(self._cached_status)
        with self._lock:
            result["pending_events"] = len(self._dirty)
            result["scanning"] = self._scanning
        return result

    def _build_status(self) -> dict[str, Any]:
        assert self.store is not None
        local = self.store.records("local")
        remote = self.store.records("remote")
        baseline = self.store.records("baseline")
        initialized_at = self.store.get_meta("baseline_initialized_at", "")
        comparison = classify_records(local, remote, baseline) if initialized_at else {
            "counts": {
                "unchanged": 0, "local_only": 0, "remote_only": 0,
                "same_change": 0, "conflict": 0,
            },
            "changes": [],
        }
        priority = {"conflict": 0, "remote_only": 1, "local_only": 2, "same_change": 3}
        changes = sorted(
            comparison["changes"],
            key=lambda item: (priority.get(item["state"], 9), item["path"]),
        )
        return {
            "enabled": True,
            "phase": self._phase,
            "watcher": self._watcher,
            "scanning": self._scanning,
            "pending_events": len(self._dirty),
            "label": self.settings.label,
            "baseline_initialized": bool(initialized_at),
            "baseline_initialized_at": initialized_at,
            "baseline_source": self.store.get_meta("baseline_source", ""),
            "last_scan_at": self.store.get_meta("last_scan_at", ""),
            "last_scan_seconds": self.store.get_meta("last_scan_seconds", 0),
            "last_error": self._last_error,
            "files": {"local": len(local), "remote": len(remote), "baseline": len(baseline)},
            "counts": comparison["counts"],
            "changes": changes[:200],
            "changes_omitted": max(0, len(changes) - 200),
            "read_only": True,
        }


def _scan_command(args: argparse.Namespace) -> int:
    try:
        config = json.loads(base64.urlsafe_b64decode(args.config_b64).decode())
        records = scan_paths(
            Path(args.root), config.get("paths", []),
            config.get("excludes", DEFAULT_EXCLUDES),
            digest_files=bool(config.get("digest_files", False)),
        )
        for record in records:
            print(json.dumps(asdict(record), separators=(",", ":")), flush=True)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Orchestrator sync status helper")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan")
    scan.add_argument("--root", required=True)
    scan.add_argument("--config-b64", required=True)
    args = parser.parse_args(argv)
    if args.command == "scan":
        return _scan_command(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
