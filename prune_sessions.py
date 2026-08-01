#!/usr/bin/env python3
"""Safely move prunable output session directories to macOS Trash."""

from __future__ import annotations

import argparse
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from session_safety import (
    get_active_dirs,
    get_archived_timestamps,
    is_empty_session_dir,
    output_dir_timestamp,
    probe_live_orch_sessions,
)


def _unique_trash_target(trash_dir: Path, name: str) -> Path:
    target = trash_dir / name
    if not target.exists():
        return target

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for idx in range(1, 1000):
        candidate = trash_dir / f"{name}-trashed-{stamp}-{idx}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not allocate unique Trash target for {name}")


def _move_to_trash(path: Path, trash_dir: Path) -> Path:
    trash_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_trash_target(trash_dir, path.name)
    shutil.move(str(path), str(target))
    return target


def _classify(path: Path, archived_timestamps: set[str]) -> str | None:
    dirname = path.name
    if dirname.startswith("organize-"):
        return "organize"
    if is_empty_session_dir(path):
        return "empty"
    ts = output_dir_timestamp(dirname)
    if ts and ts in archived_timestamps:
        return "archived"
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be moved to Trash.")
    parser.add_argument("--outputs-dir", default="",
                        help="Override outputs directory. Defaults to ./outputs next to this script.")
    parser.add_argument("--projects-dir", default="",
                        help="Override projects directory. Defaults to ./projects next to this script.")
    parser.add_argument("--trash-dir", default=str(Path.home() / ".Trash"),
                        help="Directory used as Trash target. Defaults to ~/.Trash.")
    parser.add_argument("--protect", action="append", default=[],
                        help="Output directory name to always keep. May be repeated.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    outputs_dir = Path(args.outputs_dir).resolve() if args.outputs_dir else script_dir / "outputs"
    projects_dir = Path(args.projects_dir).resolve() if args.projects_dir else script_dir / "projects"
    trash_dir = Path(args.trash_dir).expanduser().resolve()
    protected = set(args.protect or [])

    live_sessions, tmux_ok = probe_live_orch_sessions()
    if not tmux_ok:
        print("ERROR: could not reliably query tmux sessions; refusing to prune.")
        return 2

    archived_timestamps = get_archived_timestamps(projects_dir)
    active_dirs = get_active_dirs(outputs_dir, live_sessions=live_sessions)

    print(f"Found {len(archived_timestamps)} archived timestamps in projects/.")
    print(f"Found {len(active_dirs)} active output dir(s).")
    if active_dirs:
        print("Active protected dirs:")
        for name in sorted(active_dirs):
            print(f"  - {name}")
    if protected:
        print("Extra protected dirs:")
        for name in sorted(protected):
            print(f"  - {name}")
    print("")

    counts: Counter[str] = Counter()
    kept = 0
    skipped_active = 0
    skipped_protected = 0

    if not outputs_dir.is_dir():
        print(f"No outputs directory found: {outputs_dir}")
        return 0

    for path in sorted(outputs_dir.iterdir(), key=lambda p: p.name):
        if not path.is_dir():
            continue

        dirname = path.name
        if dirname in protected:
            skipped_protected += 1
            kept += 1
            continue
        if dirname in active_dirs:
            skipped_active += 1
            kept += 1
            continue

        reason = _classify(path, archived_timestamps)
        if not reason:
            kept += 1
            continue

        live_now, tmux_ok = probe_live_orch_sessions()
        if not tmux_ok:
            print("ERROR: tmux probe failed during per-directory safety check; aborting.")
            return 2
        active_now = get_active_dirs(outputs_dir, live_sessions=live_now)
        if dirname in active_now:
            skipped_active += 1
            kept += 1
            print(f"  [SKIP active]   {dirname}")
            continue

        counts[reason] += 1
        if args.dry_run:
            print(f"  [TRASH {reason:<8}] {dirname}")
        else:
            target = _move_to_trash(path, trash_dir)
            print(f"  Trashed {reason:<8}: {dirname} -> {target}")

    total = sum(counts.values())
    print("")
    if args.dry_run:
        print(
            "Would move to Trash: "
            f"{counts['empty']} empty, {counts['archived']} archived, "
            f"{counts['organize']} organize."
        )
        print(f"Would keep: {kept} session dir(s).")
        print("(dry-run mode, nothing was moved)")
    else:
        print(
            "Moved to Trash: "
            f"{counts['empty']} empty, {counts['archived']} archived, "
            f"{counts['organize']} organize."
        )
        print(f"Kept: {kept} session dir(s).")
    if skipped_active or skipped_protected:
        print(f"Skipped: {skipped_active} active, {skipped_protected} explicitly protected.")
    print(f"Total candidates: {total}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
