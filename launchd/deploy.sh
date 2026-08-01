#!/usr/bin/env bash
# Deploy the dev tree (wherever this script lives) to a TCC-safe "live" dir
# that the LaunchAgent runs from, then kick the service.
#
# Why two dirs?
#   macOS TCC blocks LaunchAgent-spawned processes from reading files under
#   ~/Documents, ~/Desktop, ~/Downloads. But you still want to *develop* in
#   ~/Documents/Projects/... (IDE habits, existing workflows). So we keep a
#   shadow copy at $LIVE_DIR (default ~/projects/agent-orchestrator) where
#   launchd can happily read, and sync code into it on each deploy.
#
# Runtime data and machine-local configuration live in the LIVE dir only. The
# sync excludes preserve them while replacing the tracked application tree.
#
# Usage:
#   ./launchd/deploy.sh              # sync code → live dir, fast-restart service
#   ./launchd/deploy.sh --install    # first-time: also register LaunchAgent
#   ./launchd/deploy.sh --dry-run    # show what rsync would change, no writes
#
# Env:
#   LIVE_DIR=~/projects/agent-orchestrator    (destination)
#   ORCH_PYTHON=/path/to/python3.11           (optional bootstrap override)
#   ORCH_DASHBOARD_TOKEN=...                  (optional on --install)
#   ORCH_OUTPUTS_DIR=/absolute/runtime/path   (only used on --install)
#   ORCH_DASHBOARD_HOST=127.0.0.1             (ditto)
#   ORCH_DASHBOARD_PORT=7860                   (PORT also remains compatible)

set -euo pipefail

DEV_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LIVE_DIR="${LIVE_DIR:-$HOME/projects/agent-orchestrator}"

INSTALL=0
DRY=0
for arg in "$@"; do
  case "$arg" in
    --install|-i) INSTALL=1 ;;
    --dry-run|-n) DRY=1 ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; s/^set.*//'; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# Safety: don't deploy the live dir onto itself.
if [[ "$(cd "$DEV_DIR" && pwd -P)" == "$(cd "$LIVE_DIR" 2>/dev/null && pwd -P || echo __not_there__)" ]]; then
  echo "error: DEV_DIR and LIVE_DIR resolve to the same path; nothing to do"
  exit 1
fi

mkdir -p "$LIVE_DIR"

RSYNC_FLAGS=(-a --delete --stats)
if [[ $DRY -eq 1 ]]; then
  RSYNC_FLAGS+=(--dry-run --verbose)
fi

# Exclude things that either (a) belong to the running/live instance and
# should not be clobbered by the dev tree, or (b) are dev-only clutter.
EXCLUDES=(
  --filter='protect docs/'       # keep excluded machine-local docs without delete warnings
  --exclude='.git/'
  --exclude='.venv/'
  --exclude='__pycache__/'
  --exclude='*.pyc'
  --exclude='.DS_Store'
  --exclude='outputs/'            # runtime artifacts live in LIVE_DIR
  --exclude='projects/'           # archived transcripts live in LIVE_DIR
  --exclude='.dashboard-certs/'   # TLS cert cache lives in LIVE_DIR
  --exclude='launchd/_token'      # preserve a legacy local token cache
  --exclude='dashboard.local.json'
  --exclude='tasks/local-test.yaml'
  --exclude='tasks/fix-hang-issue.yaml'
  --exclude='tasks/private/'
  --exclude='docs/dashboard-session-summary.md'
  --exclude='docs/internal/'
  --exclude='.idea/'
  --exclude='.vscode/'
)

echo "deploy:  $DEV_DIR"
echo "    →    $LIVE_DIR"
echo
rsync "${RSYNC_FLAGS[@]}" "${EXCLUDES[@]}" "$DEV_DIR/" "$LIVE_DIR/"

if [[ $DRY -eq 1 ]]; then
  echo "(dry-run — no service action)"
  exit 0
fi

find_bootstrap_python() {
  local candidate resolved
  if [[ -n "${ORCH_PYTHON:-}" ]]; then
    candidate="$ORCH_PYTHON"
    resolved="$(command -v "$candidate" 2>/dev/null || true)"
    if [[ -n "$resolved" ]] && "$resolved" -c \
        'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
      echo "$resolved"
      return 0
    fi
    return 1
  fi

  for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    resolved="$(command -v "$candidate" 2>/dev/null || true)"
    if [[ -n "$resolved" ]] && "$resolved" -c \
        'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
      echo "$resolved"
      return 0
    fi
  done
  return 1
}

VENV_PYTHON="$LIVE_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  BOOTSTRAP_PYTHON="$(find_bootstrap_python || true)"
  if [[ -z "$BOOTSTRAP_PYTHON" ]]; then
    echo "error: Python 3.10+ is required to create the dashboard runtime" >&2
    echo "install a current Python or set ORCH_PYTHON=/path/to/python3" >&2
    exit 1
  fi
  echo "creating dashboard runtime with $BOOTSTRAP_PYTHON ..."
  "$BOOTSTRAP_PYTHON" -m venv "$LIVE_DIR/.venv"
fi
if ! "$VENV_PYTHON" -c \
    'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "error: $LIVE_DIR/.venv uses Python older than 3.10" >&2
  echo "move that venv to Trash and rerun this command" >&2
  exit 1
fi

REQUIREMENTS_FILE="$LIVE_DIR/requirements.txt"
REQUIREMENTS_STAMP="$LIVE_DIR/.venv/.orch-requirements.sha256"
REQUIREMENTS_HASH="$("$VENV_PYTHON" -c \
  'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
  "$REQUIREMENTS_FILE")"
INSTALLED_HASH=""
if [[ -f "$REQUIREMENTS_STAMP" ]]; then
  IFS= read -r INSTALLED_HASH < "$REQUIREMENTS_STAMP" || true
fi

if [[ "$INSTALLED_HASH" != "$REQUIREMENTS_HASH" ]] || \
    ! "$VENV_PYTHON" -c 'import fastapi, httpx, uvicorn, websockets, yaml' \
      >/dev/null 2>&1; then
  echo "installing dashboard dependencies ..."
  "$VENV_PYTHON" -m pip install --disable-pip-version-check \
    -r "$REQUIREMENTS_FILE"
  printf '%s\n' "$REQUIREMENTS_HASH" > "$REQUIREMENTS_STAMP"
fi
"$VENV_PYTHON" -m pip check

# Re-create outputs/ just in case (LaunchAgent needs to write logs there).
mkdir -p "$LIVE_DIR/outputs"

# First-time install vs. fast restart.
if [[ $INSTALL -eq 1 ]]; then
  echo
  echo "running first-time install in $LIVE_DIR ..."
  "$LIVE_DIR/launchd/install.sh"
else
  # If the service isn't loaded yet, fall back to a full install.
  if launchctl print "gui/$(id -u)/com.user.orch-dashboard" >/dev/null 2>&1; then
    echo
    echo "fast-restarting LaunchAgent ..."
    "$LIVE_DIR/launchd/install.sh" --fast
  else
    echo
    echo "LaunchAgent not loaded yet — running full install ..."
    "$LIVE_DIR/launchd/install.sh"
  fi
fi
