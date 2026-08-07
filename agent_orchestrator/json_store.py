"""Atomic, process-safe updates for small JSON state files."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _write_unlocked(path: Path, data: dict) -> None:
    fd, raw_tmp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def edit_json(path: Path, *, create: bool = False) -> Iterator[dict]:
    """Lock, load, mutate, and atomically replace one JSON object."""
    path = Path(path)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.exists():
            data = json.loads(path.read_text())
        elif create:
            data = {}
        else:
            raise FileNotFoundError(path)
        if not isinstance(data, dict):
            raise ValueError(f"JSON metadata must be an object: {path}")
        yield data
        _write_unlocked(path, data)


def write_json(path: Path, data: dict) -> None:
    """Atomically replace a JSON object while excluding concurrent editors."""
    path = Path(path)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _write_unlocked(path, data)
