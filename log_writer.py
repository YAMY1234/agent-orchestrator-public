#!/usr/bin/env python3
"""Smart log writer that deduplicates TUI screen refreshes.

For full-screen TUI apps (like Cursor Agent), each screen redraw pushes
the old screen into tmux scrollback, causing duplicate headers. This script
finds the last occurrence of the agent header and keeps only content from
there onward.
"""

import re
import sys
from pathlib import Path

HEADER_PATTERNS = [
    re.compile(r"Cursor Agent v\d"),
    re.compile(r"Claude Code v\d"),
]


def find_last_header(lines: list[str]) -> int:
    """Find the line index of the last agent header."""
    last = -1
    for i, line in enumerate(lines):
        for pat in HEADER_PATTERNS:
            if pat.search(line):
                last = i
                break
    return last


def dedup_and_write(logfile: Path, raw_content: str):
    if not raw_content.strip():
        return

    lines = raw_content.split("\n")
    last_header = find_last_header(lines)

    if last_header > 0:
        start = max(0, last_header - 1)
        lines = lines[start:]

    logfile.parent.mkdir(parents=True, exist_ok=True)

    content = "\n".join(lines)
    for attempt in range(3):
        try:
            logfile.write_text(content)
            return
        except OSError as e:
            if attempt < 2:
                import time
                time.sleep(0.5 * (attempt + 1))
            else:
                print(f"Failed to write {logfile} after 3 attempts: {e}",
                      file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: log_writer.py <logfile>", file=sys.stderr)
        sys.exit(1)
    logfile = Path(sys.argv[1])
    raw_content = sys.stdin.read()
    dedup_and_write(logfile, raw_content)
