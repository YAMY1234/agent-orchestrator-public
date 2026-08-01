#!/bin/bash
# Resume watcher (permission auto-accept + logging) on an existing tmux session, then attach.
# Usage: orch continue [session] [--prompt MSG] [--no-attach]
#
# If session is omitted, lists all live orch-* sessions and prompts for selection.
# The watcher runs in the background, exactly like run.sh's watcher loop.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUTS_DIR="${ORCH_OUTPUTS_DIR:-$SCRIPT_DIR/outputs}"
LOG_WRITER="$SCRIPT_DIR/log_writer.py"

SESSION=""
PROMPT=""
NO_ATTACH=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prompt|-p) PROMPT="$2"; shift 2 ;;
        --no-attach) NO_ATTACH=true; shift ;;
        *)           SESSION="$1"; shift ;;
    esac
done

# ── Find / select the tmux session ──────────────────────────────────────────

_list_orch_sessions() {
    tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^orch-' | sort
}

if [ -z "$SESSION" ]; then
    SESSIONS=($(_list_orch_sessions))
    if [ ${#SESSIONS[@]} -eq 0 ]; then
        echo "No live orch-* tmux sessions found."
        exit 1
    fi
    if [ ${#SESSIONS[@]} -eq 1 ]; then
        SESSION="${SESSIONS[0]}"
        echo "Auto-selected: $SESSION"
    else
        echo "Live orch-* sessions:"
        for i in "${!SESSIONS[@]}"; do
            echo "  [$((i+1))] ${SESSIONS[$i]}"
        done
        printf "Select [1-%d]: " "${#SESSIONS[@]}"
        read -r choice
        idx=$((choice - 1))
        if [ "$idx" -lt 0 ] || [ "$idx" -ge "${#SESSIONS[@]}" ]; then
            echo "Invalid selection."
            exit 1
        fi
        SESSION="${SESSIONS[$idx]}"
    fi
fi

# Allow partial match: if user passes e.g. "interactive" try "orch-interactive-*"
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    MATCH=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "orch-${SESSION}" | head -1)
    if [ -n "$MATCH" ]; then
        echo "Matched: $MATCH"
        SESSION="$MATCH"
    else
        echo "Session '$SESSION' not found."
        echo "Live sessions: $(_list_orch_sessions | tr '\n' ' ')"
        exit 1
    fi
fi

# ── Resolve log file ────────────────────────────────────────────────────────

# Try to find the most recent output dir for this session's task
TASK_NAME=$(echo "$SESSION" | sed -E 's/^orch-(.+)-[0-9]+$/\1/')
LOGFILE=""

LATEST_DIR=$(ls -1td "$OUTPUTS_DIR/${TASK_NAME}-"* 2>/dev/null | head -1)
if [ -n "$LATEST_DIR" ]; then
    RUN_DIR="$LATEST_DIR"
    LOGFILE="$RUN_DIR/logs/${TASK_NAME}.log"
else
    # Fallback: create a continue-specific output dir
    TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
    RUN_DIR="$OUTPUTS_DIR/${TASK_NAME}-continue-${TIMESTAMP}"
    mkdir -p "$RUN_DIR/logs"
    LOGFILE="$RUN_DIR/logs/${TASK_NAME}.log"
fi
mkdir -p "$(dirname "$LOGFILE")"

# See watcher.sh / perm_gate.py for the auto-approve logic.
WATCHER_SH="$SCRIPT_DIR/watcher.sh"

echo ""
echo "Session: $SESSION"
echo "Task:    $TASK_NAME"
echo "Log:     $LOGFILE"

# ── Optionally send a continuation prompt ────────────────────────────────────

if [ -n "$PROMPT" ]; then
    echo "Sending prompt: ${PROMPT:0:80}..."
    tmux send-keys -t "$SESSION" -l "$PROMPT"
    sleep 1
    tmux send-keys -t "$SESSION" Enter
fi

# ── Start watcher as a daemonized process ────────────────────────────────────

# Replace any existing watcher for this RUN_DIR. We wait for it to
# actually die before continuing, otherwise its own cleanup handler
# can race with the new watcher and delete the fresh .watcher.pid
# that we're about to write. See run.sh for the full rationale.
_kill_stale_watcher() {
    [ -f "$RUN_DIR/.watcher.pid" ] || return 0
    local old_pid
    old_pid=$(cat "$RUN_DIR/.watcher.pid" 2>/dev/null || true)
    if [ -z "$old_pid" ] || ! kill -0 "$old_pid" 2>/dev/null; then
        rm -f "$RUN_DIR/.watcher.pid"
        return 0
    fi
    echo "Killing stale watcher pid=$old_pid"
    kill "$old_pid" 2>/dev/null || true
    for _ in $(seq 1 25); do
        kill -0 "$old_pid" 2>/dev/null || break
        sleep 0.2
    done
    if kill -0 "$old_pid" 2>/dev/null; then
        kill -9 "$old_pid" 2>/dev/null || true
        sleep 0.2
    fi
    rm -f "$RUN_DIR/.watcher.pid"
}
_kill_stale_watcher

echo "Starting watcher (permission auto-accept + logging)..."
nohup bash "$WATCHER_SH" "$SESSION" "$LOGFILE" "$RUN_DIR" \
    >> "$RUN_DIR/.watcher.log" 2>&1 </dev/null &
WATCHER_PID=$!
disown "$WATCHER_PID" 2>/dev/null || true

echo "Watcher PID: $WATCHER_PID (survives detach; exits when session ends)"
echo ""

if [ "$NO_ATTACH" = true ]; then
    echo "Watcher running in background (--no-attach)."
    echo "Stop it with: kill $WATCHER_PID  (or: rm $RUN_DIR/.watcher.pid && kill \$(cat $RUN_DIR/.watcher.pid))"
    exit 0
fi

echo "(Detach with Ctrl+b d to return)"
tmux attach-session -t "$SESSION"
