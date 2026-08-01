#!/usr/bin/env bash
# Remove the orch-dashboard LaunchAgent and stop the background service.
# After this, start the dashboard manually from iTerm via: orch dashboard
set -euo pipefail

LABEL="com.user.orch-dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
PORT="${PORT:-7860}"

echo "Stopping $LABEL ..."
if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  echo "  ✓ booted out"
else
  echo "  (not loaded)"
fi

if [[ -f "$PLIST" ]]; then
  rm -f "$PLIST"
  echo "  ✓ removed $PLIST"
fi

# Report, but never kill, an unrelated process that happens to use the port.
listener="$(lsof -iTCP:"$PORT" -sTCP:LISTEN -n -P 2>/dev/null | tail -n +2 || true)"
if [[ -n "$listener" ]]; then
  echo "Warning: another process is still listening on :$PORT"
  echo "$listener"
fi

echo
echo "Done. To start the dashboard manually, from iTerm:"
echo "  orch dashboard --https            # or: orch dashboard"
echo
echo "Your deployed mirror is still present. Move it to Trash manually if"
echo "you no longer need it."
