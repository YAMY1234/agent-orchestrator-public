#!/bin/bash
# Daemonized permission-watcher + pane-logger for an orch tmux session.
#
# Usage:
#   watcher.sh <tmux_session> <log_file> <run_dir>
#
# Why this exists as a standalone script:
#   Earlier versions ran the watcher as a subshell `( ... ) &` inside
#   run.sh / continue.sh / organize.sh. Because the subshell was a child
#   of the parent orch-* shell, the watcher died as soon as:
#     - the user detached from tmux (`Ctrl+b d` returns the parent,
#       which then hits its EXIT trap and kill'd the watcher),
#     - the terminal window hosting `orch run` was closed (SIGHUP),
#     - macOS logged out / the parent terminal crashed.
#   The tmux session kept running (it was created detached), so the
#   agent was still alive but nobody was around to auto-press `y` on
#   permission prompts.
#
# Running this as a standalone `nohup` + `disown` + `trap '' HUP`
# process decouples its lifetime from the orch-* shell that spawned
# it. The loop exits on its own when `tmux has-session` goes false,
# i.e. when the agent session actually dies.
#
# PID file: <run_dir>/.watcher.pid  — used by launchers to detect and
# replace a stale watcher instead of stacking a second one.

set -u

# macOS treats MallocStackLogging=0 / MallocStackLoggingNoCompact=0 as a
# request to toggle malloc stack logging and prints noisy diagnostics for every
# short-lived Python helper. Unset instead of setting false-ish values.
unset MallocStackLogging MallocStackLoggingNoCompact MallocScribble MallocGuardEdges MallocNanoZone

SESSION="${1:?missing session}"
LOGFILE="${2:?missing logfile}"
RUN_DIR="${3:?missing run_dir}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_WRITER="$SCRIPT_DIR/log_writer.py"
PERM_GATE="$SCRIPT_DIR/perm_gate.py"
PID_FILE="$RUN_DIR/.watcher.pid"
PERM_STREAM_LOG="$RUN_DIR/.perm_gate_stream.log"

# Ignore HUP so that closing the parent terminal doesn't take us out.
trap '' HUP

mkdir -p "$(dirname "$LOGFILE")"
echo $$ > "$PID_FILE"

start_perm_stream() {
    local session_q perm_gate_q stream_log_q
    session_q=$(printf "%q" "$SESSION")
    perm_gate_q=$(printf "%q" "$PERM_GATE")
    stream_log_q=$(printf "%q" "$PERM_STREAM_LOG")
    # Replace any stale pipe from a prior watcher, then stream raw terminal
    # output into perm_gate. This sees alternate-screen prompts at emission
    # time, before tmux capture-pane can lose them.
    tmux pipe-pane -t "$SESSION" 2>/dev/null || true
    tmux pipe-pane -t "$SESSION" \
        "python3 $perm_gate_q --watch $session_q >> $stream_log_q 2>&1" \
        2>/dev/null || true
}

ensure_perm_stream() {
    local pane_pipe
    pane_pipe=$(tmux display-message -p -t "$SESSION" "#{pane_pipe}" 2>/dev/null || echo 0)
    if [ "$pane_pipe" != "1" ]; then
        start_perm_stream
    fi
}

cleanup() {
    tmux pipe-pane -t "$SESSION" 2>/dev/null || true
    # Final log snapshot — do this regardless of how we exited, so
    # the last bit of pane output isn't lost if tmux session just
    # vanished between iterations.
    tmux capture-pane -t "$SESSION" -p -S - 2>/dev/null \
        | python3 "$LOG_WRITER" "$LOGFILE" 2>/dev/null || true
    # Only delete the pid file if it still belongs to us. Otherwise
    # we'd race with a replacement watcher started by `orch continue`
    # and unlink its freshly-written pid file. See run.sh's
    # _kill_stale_watcher() for the paired guard.
    if [ -f "$PID_FILE" ] && [ "$(cat "$PID_FILE" 2>/dev/null)" = "$$" ]; then
        rm -f "$PID_FILE"
    fi
}
trap cleanup EXIT TERM INT

start_perm_stream

LAST_LOG_TIME=0
LOG_INTERVAL=5
LAST_STREAM_CHECK=0
STREAM_CHECK_INTERVAL=5

while tmux has-session -t "$SESSION" 2>/dev/null; do
    pane_text=$(tmux capture-pane -t "$SESSION" -p -S - 2>/dev/null)

    NOW=$(date +%s)
    if [ $((NOW - LAST_STREAM_CHECK)) -ge $STREAM_CHECK_INTERVAL ]; then
        ensure_perm_stream
        LAST_STREAM_CHECK=$NOW
    fi

    if [ -n "$pane_text" ] && [ $((NOW - LAST_LOG_TIME)) -ge $LOG_INTERVAL ]; then
        echo "$pane_text" | python3 "$LOG_WRITER" "$LOGFILE"
        LAST_LOG_TIME=$NOW
    fi

    # Permission-prompt detection is in perm_gate.py (shared with all
    # launchers). It prints "y" / "enter" / "" to stdout.
    #
    # We pull ~500 rows of scrollback (not just the visible pane)
    # because Cursor prints the to-be-run command verbatim above
    # the arrow, and a long `python3 -c '...'` pipeline can wrap
    # into 100+ visual rows — that pushes the question line off
    # the top of the visible pane into scrollback. perm_gate's
    # own logic still only treats the bottom ~15 rows as "where
    # a live arrow may appear", so we don't accept on a stale
    # historical prompt from deep in scrollback.
    key=$(tmux capture-pane -t "$SESSION" -p -S -500 2>/dev/null | python3 "$PERM_GATE")
    case "$key" in
        y)     tmux send-keys -t "$SESSION" "y";     sleep 1 ;;
        enter) tmux send-keys -t "$SESSION" Enter;   sleep 1 ;;
    esac
    sleep 0.5
done
