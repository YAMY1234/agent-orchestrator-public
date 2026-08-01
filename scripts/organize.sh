#!/bin/bash
# Organize past sessions into project folders using an AI agent, then prune.
# Usage: orch organize [--stale DURATION] [claude|cursor|codex]
# Examples:
#   orch organize                  # organize all inactive sessions
#   orch organize 3h               # only sessions not updated in 3 hours
#   orch organize --stale 1d       # only sessions not updated in 1 day
#   orch organize 30m claude       # stale 30min, use claude agent
#   orch organize codex            # use OpenAI codex CLI (YOLO mode)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUTS_DIR="${ORCH_OUTPUTS_DIR:-$REPO_DIR/outputs}"
PROJECTS_DIR="${ORCH_PROJECTS_DIR:-$(dirname "$OUTPUTS_DIR")/projects}"

# Keep macOS malloc debug toggles from leaking into Python helper processes.
unset MallocStackLogging MallocStackLoggingNoCompact MallocScribble MallocGuardEdges MallocNanoZone

STALE=""
NO_ATTACH=0
POSITIONAL=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stale)     STALE="$2"; shift 2 ;;
        # `--no-attach` skips the final `tmux attach-session` call so the
        # script can be started from a non-TTY parent (the web dashboard's
        # background Popen). Without this, the attach fails instantly,
        # the script exits, and its EXIT trap kills the watcher — leaving
        # the organize tmux session running with nobody to auto-answer
        # permission prompts.
        --no-attach) NO_ATTACH=1; shift ;;
        *)           POSITIONAL+=("$1"); shift ;;
    esac
done

# Auto-detect: if stdin is not a tty (e.g. launched by the dashboard
# via Popen with stdin=DEVNULL), behave as if --no-attach was passed.
if [[ $NO_ATTACH -eq 0 ]] && ! [ -t 0 ]; then
    NO_ATTACH=1
fi

# First positional can be a duration (digits+unit) or agent type
if [[ ${#POSITIONAL[@]} -ge 1 ]]; then
    first="${POSITIONAL[0]}"
    if [[ "$first" =~ ^[0-9]+[smhd]?$ && -z "$STALE" ]]; then
        STALE="$first"
        AGENT_TYPE="${POSITIONAL[1]:-cursor}"
    else
        AGENT_TYPE="$first"
    fi
else
    AGENT_TYPE="cursor"
fi
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
RUN_NAME="organize-${TIMESTAMP}"
RUN_DIR="$OUTPUTS_DIR/$RUN_NAME"
LOG_DIR="$RUN_DIR/logs"

mkdir -p "$LOG_DIR"
mkdir -p "$PROJECTS_DIR"

LOGFILE="$LOG_DIR/organize.log"
SESSION="orch-organize-$$"

case "$AGENT_TYPE" in
    claude)
        AGENT_CMD="claude"
        ;;
    cursor|agent)
        AGENT_CMD="agent"
        ;;
    codex)
        AGENT_CMD="codex --dangerously-bypass-approvals-and-sandbox"
        ;;
    *)
        AGENT_CMD="$AGENT_TYPE"
        ;;
esac

# See watcher.sh / perm_gate.py for the auto-approve logic.
WATCHER_SH="$SCRIPT_DIR/watcher.sh"

STALE_ARGS=""
if [[ -n "$STALE" ]]; then
    STALE_ARGS="--stale $STALE"
fi

# Pre-check: anything to organize?
PENDING_CHECK=$(python3 "$SCRIPT_DIR/organize_prompt.py" \
    --outputs-dir "$OUTPUTS_DIR" \
    --projects-dir "$PROJECTS_DIR" \
    --self-session "$RUN_NAME" $STALE_ARGS 2>&1 | head -1)

if echo "$PENDING_CHECK" | grep -q "没有需要整理的"; then
    echo "$PENDING_CHECK"
    echo ""
    echo "Running safe prune to move empty/archived sessions to Trash..."
    bash "$SCRIPT_DIR/prune.sh" --protect "$RUN_NAME"
    rmdir "$LOG_DIR" "$RUN_DIR" 2>/dev/null
    exit 0
fi

echo "=== Organize Mode ==="
echo "Agent:   $AGENT_TYPE"
echo "Stale:   ${STALE:-all inactive}"
echo "Session: $SESSION"
echo "Output:  $RUN_DIR"
echo ""

COLS=$(tput cols 2>/dev/null || echo 200)
ROWS=$(tput lines 2>/dev/null || echo 50)

tmux new-session -d -s "$SESSION" -x "$COLS" -y "$ROWS" \
    "cd $REPO_DIR && $AGENT_CMD; echo '--- Agent exited ---'; read"

# Wait for agent to be ready
echo "Waiting for agent to initialize..."
READY=false
for i in $(seq 1 30); do
    pane=$(tmux capture-pane -t "$SESSION" -p -S - 2>/dev/null)
    # Idle markers, by agent:
    #   cursor: "Plan, search, build anything"
    #   claude: trailing `>`/`❯` prompt or "? for shortcuts"
    #   codex:  the box-drawn input ruler shows "›" followed by a
    #           rotating placeholder ending in "@filename" once ready.
    if echo "$pane" | grep -qE "Plan, search, build anything|[❯>]\s*$|\?\s+for shortcuts|› +.*@filename"; then
        READY=true
        break
    fi
    if echo "$pane" | grep -qE "Yes, I trust this folder|Is this a project you created"; then
        tmux send-keys -t "$SESSION" Enter
        sleep 3
        continue
    fi
    sleep 2
done

if ! $READY; then
    echo "WARNING: Agent may not be ready, sending prompt anyway..."
fi

# Generate prompt, load into tmux buffer, and paste
PROMPT_FILE=$(mktemp)
python3 "$SCRIPT_DIR/organize_prompt.py" \
    --outputs-dir "$OUTPUTS_DIR" \
    --projects-dir "$PROJECTS_DIR" \
    --self-session "$RUN_NAME" $STALE_ARGS > "$PROMPT_FILE"

sleep 1
tmux load-buffer "$PROMPT_FILE"
tmux paste-buffer -t "$SESSION" -d
sleep 1
tmux send-keys -t "$SESSION" Enter
rm -f "$PROMPT_FILE"

echo "Prompt sent! Attaching to session..."
echo ""

# Daemonized watcher (survives detach / parent shell exit). See run.sh.
# Wait for any stale watcher to actually die before replacing, so its
# cleanup handler doesn't race with ours and unlink the new pid file.
_kill_stale_watcher() {
    [ -f "$RUN_DIR/.watcher.pid" ] || return 0
    local old_pid
    old_pid=$(cat "$RUN_DIR/.watcher.pid" 2>/dev/null || true)
    if [ -z "$old_pid" ] || ! kill -0 "$old_pid" 2>/dev/null; then
        rm -f "$RUN_DIR/.watcher.pid"
        return 0
    fi
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

nohup bash "$WATCHER_SH" "$SESSION" "$LOGFILE" "$RUN_DIR" \
    >> "$RUN_DIR/.watcher.log" 2>&1 </dev/null &
WATCHER_PID=$!
disown "$WATCHER_PID" 2>/dev/null || true

cleanup() {
    # Watcher keeps running in the background; don't kill it here.
    # Prune must only fire after the tmux session actually ends,
    # otherwise it would try to archive the still-running organize
    # session itself. The wait logic below handles that.
    :
}
trap cleanup EXIT INT TERM

# Wait until the tmux session is gone; this is what "organize finished"
# really means (the agent closed). For the attach path, `tmux attach`
# returns on detach too, so we still need the polling wait afterwards.
wait_for_session_end() {
    while tmux has-session -t "$SESSION" 2>/dev/null; do
        sleep 2
    done
}

if [[ $NO_ATTACH -eq 1 ]]; then
    echo "Running detached; watcher PID=$WATCHER_PID, session=$SESSION"
    wait_for_session_end
else
    tmux attach-session -t "$SESSION"
    wait_for_session_end
fi

echo ""
echo "Log saved to: $LOGFILE"
echo ""
echo "=== Running safe prune to move archived sessions to Trash ==="
bash "$SCRIPT_DIR/prune.sh" --protect "$RUN_NAME"
