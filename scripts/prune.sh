#!/bin/bash
# Move empty and already-archived session directories from outputs/ to Trash.
# Usage: orch prune [--dry-run]
#   --dry-run  show what would be moved without moving anything

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/prune_sessions.py" "$@"
