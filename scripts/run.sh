#!/bin/bash
# Lightweight interactive mode: start an agent with auto-permission + logging
# Usage: orch run [options] [claude|cursor|codex] [name]
# Examples:
#   orch run                       # cursor agent, auto-named
#   orch run claude                # claude code
#   orch run codex                 # OpenAI codex CLI (YOLO mode)
#   orch run --fast                # cursor + composer-2-fast
#   orch run --think claude        # claude + sonnet-4-thinking
#   orch run --effort high claude  # claude + explicit effort
#   orch run -m gpt-5.2 cursor    # cursor + explicit model
#
# Model shortcuts: --fast --think --codex --codex-high --max --opus --sonnet --auto
# Claude Code supports --effort low|medium|high|xhigh|max.
# Codex effort is restored through `-c model_reasoning_effort="..."`.
# Default (no -m / shortcuts):
#   cursor/agent → claude-opus-4-7-high
#   claude       → agent default
#   codex        → codex CLI default (no --model passed)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUTS_DIR="${ORCH_OUTPUTS_DIR:-$REPO_DIR/outputs}"

# Keep macOS malloc debug toggles from leaking into Python helper processes.
unset MallocStackLogging MallocStackLoggingNoCompact MallocScribble MallocGuardEdges MallocNanoZone

MODEL=""
EFFORT=""
LABEL=""
RESUME_ID=""
NATIVE_SESSION_ID=""
NATIVE_RESUME_SOURCE=""
RUN_NAME_OVERRIDE=""
TERMINAL_THEME="${ORCH_DEFAULT_TERMINAL_THEME:-soft-dark}"
NO_ATTACH=0
POSITIONAL=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model|-m)   MODEL="$2"; shift 2 ;;
        --effort)     EFFORT="$2"; shift 2 ;;
        --label|-l)   LABEL="$2"; shift 2 ;;
        --resume-id)  RESUME_ID="$2"; shift 2 ;;
        --native-session-id) NATIVE_SESSION_ID="$2"; shift 2 ;;
        --native-resume-source) NATIVE_RESUME_SOURCE="$2"; shift 2 ;;
        --run-name)   RUN_NAME_OVERRIDE="$2"; shift 2 ;;
        --theme|--terminal-theme) TERMINAL_THEME="$2"; shift 2 ;;
        --no-attach)  NO_ATTACH=1; shift ;;   # spawn tmux + watcher, don't attach
        --fast)       MODEL="composer-2-fast"; shift ;;
        --think)      MODEL="claude-opus-4-7-thinking-high"; shift ;;
        --codex)      MODEL="gpt-5.3-codex"; shift ;;
        --codex-high) MODEL="gpt-5.3-codex-high"; shift ;;
        --max)        MODEL="gpt-5.3-codex-xhigh"; shift ;;
        --opus)       MODEL="claude-opus-4-7-high"; shift ;;
        --sonnet)     MODEL="claude-4.6-sonnet-medium"; shift ;;
        --auto)       MODEL="auto"; shift ;;
        *)            POSITIONAL+=("$1"); shift ;;
    esac
done

AGENT_TYPE="${POSITIONAL[0]:-cursor}"
TASK_NAME="${POSITIONAL[1]:-interactive}"
CWD="${POSITIONAL[2]:-$(pwd)}"

# Claude records transient API-error placeholder messages with
# model="<synthetic>". It is not a CLI model name; passing it to an
# unquoted shell command turns `<synthetic>` into input redirection.
if [[ "$MODEL" == \<*\> ]]; then
    MODEL=""
fi

# If no model is specified, cursor/agent uses claude-opus-4-7-high.
# Claude and Codex keep their own CLI defaults.
if [ -z "$MODEL" ] && [ "$AGENT_TYPE" != "claude" ] && [ "$AGENT_TYPE" != "codex" ]; then
    MODEL="claude-opus-4-7-high"
fi

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
RUN_NAME="${RUN_NAME_OVERRIDE:-${TASK_NAME}-${TIMESTAMP}}"
if [[ "$RUN_NAME" == *"/"* || "$RUN_NAME" == "." || "$RUN_NAME" == ".." ]]; then
    echo "Invalid --run-name: $RUN_NAME" >&2
    exit 2
fi
RUN_DIR="$OUTPUTS_DIR/$RUN_NAME"
LOG_DIR="$RUN_DIR/logs"
RUN_ID="${RUN_NAME}::${TASK_NAME}"

mkdir -p "$LOG_DIR"

LOGFILE="$LOG_DIR/${TASK_NAME}.log"
SESSION="orch-${TASK_NAME}-$$"
SESSION_JSON="$RUN_DIR/session.json"

case "$AGENT_TYPE" in
    claude)
        AGENT_CMD="claude"
        ;;
    cursor|agent)
        AGENT_CMD="agent"
        ;;
    codex)
        # YOLO mode: skip approval prompts + sandbox so the watcher doesn't
        # have to babysit per-command confirmations. Codex's permission
        # model is different from Cursor/Claude (it asks once at startup
        # for sandbox policy, not per-command), so the same auto-press-y
        # loop in perm_gate.py largely doesn't apply — but we still let
        # the watcher run for log capture.
        AGENT_CMD="codex --dangerously-bypass-approvals-and-sandbox"
        ;;
    *)
        AGENT_CMD="$AGENT_TYPE"
        ;;
esac

_append_effort_arg() {
    [ -n "$EFFORT" ] || return 0
    case "$AGENT_TYPE" in
        claude)
            AGENT_CMD="$AGENT_CMD --effort $(printf '%q' "$EFFORT")"
            ;;
        codex)
            local effort_cfg
            effort_cfg=$(printf '%q' "model_reasoning_effort=\"$EFFORT\"")
            AGENT_CMD="$AGENT_CMD -c $effort_cfg"
            ;;
    esac
}

if [ -n "$MODEL" ]; then
    MODEL_Q=$(printf '%q' "$MODEL")
    if [ "$AGENT_TYPE" = "codex" ]; then
        # codex uses `-m <MODEL>` (no --model long form in current CLI).
        AGENT_CMD="$AGENT_CMD -m $MODEL_Q"
    else
        AGENT_CMD="$AGENT_CMD --model $MODEL_Q"
    fi
fi

_append_effort_arg

if [ -n "$NATIVE_SESSION_ID" ] && [ -z "$RESUME_ID" ]; then
    case "$AGENT_TYPE" in
        claude)
            # Claude Code lets callers choose the session id up front.
            AGENT_CMD="$AGENT_CMD --session-id $(printf '%q' "$NATIVE_SESSION_ID")"
            ;;
        cursor|agent)
            # Cursor Agent can pre-create a chat id, then start by resuming it.
            AGENT_CMD="$AGENT_CMD --resume $(printf '%q' "$NATIVE_SESSION_ID")"
            ;;
    esac
fi

if [ -n "$RESUME_ID" ]; then
    case "$AGENT_TYPE" in
        claude)
            AGENT_CMD="$AGENT_CMD --resume $(printf '%q' "$RESUME_ID")"
            ;;
        cursor|agent)
            AGENT_CMD="$AGENT_CMD --resume $(printf '%q' "$RESUME_ID")"
            ;;
        codex)
            # `codex resume` is a subcommand; keep any model/sandbox flags
            # before the session id.
            AGENT_CMD="codex resume --dangerously-bypass-approvals-and-sandbox"
            if [ -n "$MODEL" ]; then
                MODEL_Q=$(printf '%q' "$MODEL")
                AGENT_CMD="$AGENT_CMD -m $MODEL_Q"
            fi
            _append_effort_arg
            AGENT_CMD="$AGENT_CMD $(printf '%q' "$RESUME_ID")"
            ;;
        *)
            AGENT_CMD="$AGENT_CMD --resume $(printf '%q' "$RESUME_ID")"
            ;;
    esac
fi

METADATA_RESUME_ID="${RESUME_ID:-$NATIVE_SESSION_ID}"
METADATA_RESUME_SOURCE="$NATIVE_RESUME_SOURCE"
if [ -n "$METADATA_RESUME_ID" ] && [ -z "$METADATA_RESUME_SOURCE" ]; then
    if [ -n "$RESUME_ID" ]; then
        METADATA_RESUME_SOURCE="run-sh-resume-id"
    else
        METADATA_RESUME_SOURCE="run-sh-native-session-id"
    fi
fi
METADATA_RESUME_CMD=""
if [ -n "$METADATA_RESUME_ID" ]; then
    case "$AGENT_TYPE" in
        claude)
            METADATA_RESUME_CMD="claude --resume $METADATA_RESUME_ID"
            ;;
        cursor|agent)
            METADATA_RESUME_CMD="agent --resume $METADATA_RESUME_ID"
            ;;
        codex)
            METADATA_RESUME_CMD="codex resume --dangerously-bypass-approvals-and-sandbox"
            if [ -n "$MODEL" ]; then
                METADATA_RESUME_CMD="$METADATA_RESUME_CMD -m $MODEL"
            fi
            if [ -n "$EFFORT" ]; then
                METADATA_RESUME_CMD="$METADATA_RESUME_CMD -c model_reasoning_effort=\\\"$EFFORT\\\""
            fi
            METADATA_RESUME_CMD="$METADATA_RESUME_CMD $METADATA_RESUME_ID"
            ;;
        *)
            METADATA_RESUME_CMD="$AGENT_TYPE --resume $METADATA_RESUME_ID"
            ;;
    esac
fi

# Permission-prompt detection (`perm_gate.py`) and the pane-tailing
# log/autoreply loop (`watcher.sh`) are factored out as separate
# scripts; this shell just launches tmux + the watcher and (in the
# attach path) hands the user to `tmux attach`.
WATCHER_SH="$SCRIPT_DIR/watcher.sh"

echo "Agent:   $AGENT_TYPE"
echo "Model:   ${MODEL:-default}"
if [ -n "$EFFORT" ]; then
    echo "Effort:  $EFFORT"
fi
echo "Session: $SESSION"
echo "CWD:     $CWD"
echo "Output:  $RUN_DIR"
echo "Log:     $LOGFILE"
echo ""

COLS=$(tput cols 2>/dev/null || echo 200)
ROWS=$(tput lines 2>/dev/null || echo 50)
CWD_Q=$(printf "%q" "$CWD")
RUN_DIR_Q=$(printf "%q" "$RUN_DIR")
SESSION_Q=$(printf "%q" "$SESSION")
SESSION_JSON_Q=$(printf "%q" "$SESSION_JSON")
RUN_ID_Q=$(printf "%q" "$RUN_ID")
TASK_NAME_Q=$(printf "%q" "$TASK_NAME")
AGENT_TYPE_Q=$(printf "%q" "$AGENT_TYPE")

tmux new-session -d -s "$SESSION" -x "$COLS" -y "$ROWS" \
    "cd $CWD_Q && ORCH_RUN_ID=$RUN_ID_Q ORCH_RUN_DIR=$RUN_DIR_Q ORCH_TMUX_SESSION=$SESSION_Q ORCH_SESSION_JSON=$SESSION_JSON_Q ORCH_TASK_NAME=$TASK_NAME_Q ORCH_AGENT_TYPE=$AGENT_TYPE_Q $AGENT_CMD; echo '--- Agent exited ---'; read"

cat > "$SESSION_JSON" <<EOF
{
  "kind": "run",
  "run_id": "$RUN_ID",
  "name": "$TASK_NAME",
  "agent": "$AGENT_TYPE",
  "model": "${MODEL:-default}",
  "effort": "$EFFORT",
  "cwd": "$CWD",
  "tmux_session": "$SESSION",
  "log_file": "logs/${TASK_NAME}.log",
  "started_at": "$(date +%Y-%m-%dT%H:%M:%S)",
  "resume_agent": "$AGENT_TYPE",
  "resume_id": "$METADATA_RESUME_ID",
  "resume_cmd": "$METADATA_RESUME_CMD",
  "resume_source": "$METADATA_RESUME_SOURCE",
  "resume_recorded_at": "$(date +%Y-%m-%dT%H:%M:%S)",
  "terminal_theme": "$TERMINAL_THEME",
  "linked_folders": [],
  "label": "${LABEL//\"/\\\"}"
}
EOF

# Kill any pre-existing watcher for this RUN_DIR (re-run after crash,
# or an `orch continue` replacing a daemon). We MUST wait for the old
# watcher to actually die before starting the new one, because the
# old watcher's cleanup() unlinks $RUN_DIR/.watcher.pid as its last
# step — if it runs late, it would happily delete the new watcher's
# pid file and you'd end up with a running-but-orphaned new daemon.
_kill_stale_watcher() {
    [ -f "$RUN_DIR/.watcher.pid" ] || return 0
    local old_pid
    old_pid=$(cat "$RUN_DIR/.watcher.pid" 2>/dev/null || true)
    if [ -z "$old_pid" ] || ! kill -0 "$old_pid" 2>/dev/null; then
        rm -f "$RUN_DIR/.watcher.pid"
        return 0
    fi
    kill "$old_pid" 2>/dev/null || true
    # Wait up to ~5s for graceful exit, then SIGKILL.
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

# Launch the watcher as a daemonized process. `nohup` + `disown` +
# the HUP trap inside watcher.sh together make it survive:
#   - SIGHUP when the parent terminal window is closed,
#   - this shell exiting after `tmux attach` returns (detach),
#   - user logout (modulo launchd decisions).
# The watcher naturally exits when `tmux has-session` goes false.
nohup bash "$WATCHER_SH" "$SESSION" "$LOGFILE" "$RUN_DIR" \
    >> "$RUN_DIR/.watcher.log" 2>&1 </dev/null &
WATCHER_PID=$!
disown "$WATCHER_PID" 2>/dev/null || true

cleanup() {
    # Note: we intentionally do NOT kill the watcher here. Detaching
    # from the tmux session must not take the watcher down — that was
    # the bug this script was reworked to fix. The watcher self-exits
    # when the tmux session dies.
    echo ""
    echo "Log saved to: $LOGFILE"
    echo "Watcher PID:  $WATCHER_PID (keeps running until session ends)"

    if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
        python3 -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from notifier import notify_task_done
notify_task_done('interactive', '$TASK_NAME', 'finished')
"
    fi
}
trap cleanup EXIT INT TERM

if [ "$NO_ATTACH" = "1" ]; then
    echo "tmux session $SESSION started in background."
    echo "Attach with: tmux attach -t $SESSION"
    # Don't run the cleanup echo either — keep stdout minimal for Popen.
    trap - EXIT INT TERM
    exit 0
fi

tmux attach-session -t "$SESSION"
