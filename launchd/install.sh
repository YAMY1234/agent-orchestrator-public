#!/usr/bin/env bash
# Install / reload the orch dashboard LaunchAgent.
#
# Two modes:
#   ./launchd/install.sh          # full install (writes plist, bootstraps)
#   ./launchd/install.sh --fast   # just restart the already-loaded service
#                                 # (picks up new Python code; ~1s instead of ~15s)
#
# Environment:
#   ORCH_DASHBOARD_TOKEN=...      # optional; generated securely when absent
#   ORCH_DASHBOARD_TOKEN_FILE=... # default: ~/.config/agent-orchestrator/dashboard-token
#   ORCH_OUTPUTS_DIR=...          # default: <project>/outputs
#   PORT=...                      # default: 7860
#
# After install:
#   launchctl list | grep orch-dashboard
#   tail -f outputs/_dashboard.err.log
#
# Uninstall:
#   ./launchd/uninstall.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$PROJECT_DIR/launchd/com.user.orch-dashboard.plist.template"
PLIST_DEST="$HOME/Library/LaunchAgents/com.user.orch-dashboard.plist"
LABEL="com.user.orch-dashboard"

TOKEN="${ORCH_DASHBOARD_TOKEN:-}"
TOKEN_FILE="${ORCH_DASHBOARD_TOKEN_FILE:-$HOME/.config/agent-orchestrator/dashboard-token}"
LEGACY_TOKEN_FILE="$PROJECT_DIR/launchd/_token"
OUTPUTS_DIR="${ORCH_OUTPUTS_DIR:-$PROJECT_DIR/outputs}"
PORT="${PORT:-7860}"

FAST=0
if [[ "${1:-}" == "--fast" || "${1:-}" == "-f" ]]; then
  FAST=1
fi

# ----- Fast path: just kick the existing service -----
if [[ $FAST -eq 1 ]]; then
  if ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "error: $LABEL is not loaded; run without --fast first" >&2
    exit 1
  fi
  echo "Kicking $LABEL ..."
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  # Uvicorn needs ~2-3s to bind. Poll up to 8s before giving up.
  for i in 1 2 3 4 5 6 7 8; do
    if lsof -iTCP:"$PORT" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
      echo "✓ restarted; listening on $PORT"
      exit 0
    fi
    sleep 1
  done
  echo "⚠ service restarted but nothing is listening on $PORT after 8s — check logs:"
  echo "    tail -20 $OUTPUTS_DIR/_dashboard.err.log"
  exit 1
fi

# ----- Full install path -----

# Resolve Python: prefer project venv, else system python3.
PYTHON_BIN=""
if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "error: no python3 on PATH and no .venv found in $PROJECT_DIR" >&2
  exit 1
fi

if [[ -z "$TOKEN" && -f "$TOKEN_FILE" ]]; then
  IFS= read -r TOKEN < "$TOKEN_FILE" || true
fi
if [[ -z "$TOKEN" && -f "$LEGACY_TOKEN_FILE" ]]; then
  IFS= read -r TOKEN < "$LEGACY_TOKEN_FILE" || true
fi
if [[ -z "$TOKEN" ]]; then
  TOKEN="$($PYTHON_BIN -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi

mkdir -p "$(dirname "$TOKEN_FILE")"
(
  umask 077
  printf '%s\n' "$TOKEN" > "$TOKEN_FILE"
)
chmod 600 "$TOKEN_FILE"

PATH_VAL="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$(dirname "$PLIST_DEST")"
mkdir -p "$OUTPUTS_DIR"

"$PYTHON_BIN" "$PROJECT_DIR/launchd/render_plist.py" \
  "$TEMPLATE" "$PLIST_DEST" "$PYTHON_BIN" "$PROJECT_DIR" "$PATH_VAL" \
  "$TOKEN_FILE" "$OUTPUTS_DIR" "$HOME" "$PORT"

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)" "$PLIST_DEST" 2>/dev/null || true
fi
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
launchctl enable    "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true

# Wait briefly, then diagnose.
sleep 2

echo
echo "Installed: $PLIST_DEST"
echo "Label:     $LABEL"
echo "Token:     saved to $TOKEN_FILE"
echo "Port:      $PORT"
echo "Outputs:   $OUTPUTS_DIR"
echo "Logs:      $OUTPUTS_DIR/_dashboard.{out,err}.log"
echo

# Health check.
if lsof -iTCP:"$PORT" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  echo "✓ dashboard is listening on $PORT"
  # Show the right URL scheme based on what flags the plist uses.
  if grep -q '<string>--https</string>' "$PLIST_DEST" 2>/dev/null; then
    echo "  Open: https://127.0.0.1:$PORT/"
    echo "        (self-signed cert → accept the warning once per browser)"
  else
    echo "  Open: http://127.0.0.1:$PORT/"
  fi
  echo "  Run 'orch url' to copy an authenticated URL."
else
  echo "⚠ nothing listening on $PORT yet. Recent errors:"
  tail -5 "$OUTPUTS_DIR/_dashboard.err.log" 2>/dev/null | sed 's/^/    /'
  echo
  # Detect the classic TCC ("Operation not permitted") trap.
  if grep -q "Operation not permitted" "$OUTPUTS_DIR/_dashboard.err.log" 2>/dev/null; then
    cat <<'EOF'
────────────────────────────────────────────────────────────────────
This is macOS TCC blocking launchd-spawned processes from reading
files under ~/Documents (/~/Desktop, /~/Downloads also apply).

Two ways to fix:

  (A) [recommended] Move this project out of ~/Documents, e.g.
        mv ~/Documents/Projects/agent-orchestrator ~/projects/
        cd ~/projects/agent-orchestrator
        ./launchd/install.sh

  (B) Grant "Full Disk Access" to the python that LaunchAgent uses:
        System Settings → Privacy & Security → Full Disk Access
        → add this exact binary:
EOF
    echo "            $PYTHON_BIN"
    echo "────────────────────────────────────────────────────────────────────"
  fi
fi

echo
echo "Next time you just want to pick up code changes:"
echo "    ./launchd/install.sh --fast    # ~1s restart"
