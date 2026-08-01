#!/usr/bin/env python3
"""Permission-prompt gate for the agent watchers.

Reads the full visible-pane capture from stdin and decides whether the
agent is currently showing an interactive permission prompt that we
should auto-accept.

Gate (all must hold):
  1. The pane ends with a "live" prompt block — the arrow line
     (e.g. "→ Run (once) (y)" or "❯ Yes") must be in the BOTTOM
     few lines of the pane. We scan the last `MAX_TAIL_LINES` lines
     from the bottom up for the first arrow hit.
  2. A question line (PERM_PATTERNS) must exist ABOVE that arrow,
     within `MAX_BLOCK_LINES` lines. Cursor sometimes prints the
     to-be-run command verbatim between the question and the arrow,
     and if the command is long it wraps over many lines — that's
     exactly why a fixed-size `tail -N` window was unreliable.
The fixed `tail -8` approach we used before would miss long-command
prompts entirely (the arrow falls outside the window when Cursor
prints the full command above it) — that's the bug this replaces.

Outputs on stdout one of:
  y       -> send "y" (Run/Write/Create/Delete/WebSearch/WebFetch)
  enter   -> send Enter (generic "Do you want to proceed/allow" yes)
  (empty) -> no prompt detected

`--watch <tmux-session>` reads a live `tmux pipe-pane` stream and sends the
approval key itself. This catches prompts printed inside alternate-screen TUIs
after they have already scrolled out of `tmux capture-pane`.

Exit code:
  0 if a key was emitted, 1 otherwise.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time

# Questions that indicate an interactive permission prompt.
PERM_PATTERNS = re.compile(
    r"Do you want to (proceed|allow)"
    r"|Run this command\?"
    r"|Write to this file\?"
    r"|Create this file\?"
    r"|Delete this file\?"
    r"|Allow this web search\?"
    r"|Allow this web fetch\?"
    r"|Run a dynamic workflow\?"
    r"|Switch to Agent mode\?"
)

# The selection-arrow line that ONLY appears on a live prompt.
# Matches "→ Run (once) (y)", "❯ Yes", "→ Allow", "→ Fetch (y)",
# "→ Search", "→ Proceed (y)", etc. The verb list must cover every
# action Cursor uses on the primary (first) option of a permission
# prompt:
#   Run     — `Run this command?`         (older builds)
#   Yes     — `Do you want to proceed?`
#   Allow   — generic "Allow this ...?" fallback
#   Write / Create / Delete — file-mutation prompts (older builds)
#   Fetch   — `Allow this web fetch?`     (→ Fetch (y))
#   Search  — `Allow this web search?`    (→ Search (y))
#   Proceed — newer cursor-agent builds label the primary option
#             as "Proceed (y)" for Write/Run/etc. instead of echoing
#             the verb from the question. Missing this meant every
#             Write-to-file prompt in those builds fell back to
#             manual approval.
#   Approve — Cursor mode-switch prompt: `Switch to Agent mode?`
#             followed by `Approve mode switch (y)`.
# Claude Code v2.1+ may render the primary option as a numbered menu item,
# e.g. "❯ 1. Yes" instead of "❯ Yes"; allow that optional prefix.
# Missing a verb here means the watcher silently skips that prompt
# and the user has to click the button manually.
PERM_ARROW = re.compile(
    r"^\s*(?:[│|]\s*)?[→❯]\s*(?:\d+\.\s*)?"
    r"(Run|Yes|Allow|Write|Create|Delete|Fetch|Search|Proceed|Approve)\b"
)

# Questions that should be answered with "y" (rather than Enter).
Y_QUESTIONS = re.compile(
    r"Run this command\?"
    r"|Write to this file\?"
    r"|Create this file\?"
    r"|Delete this file\?"
    r"|Allow this web search\?"
    r"|Allow this web fetch\?"
    r"|Switch to Agent mode\?"
)

WORKFLOW_MENU_VISIBLE = re.compile(
    r"[→❯]\s*(?:1\.\s*)?Yes,\s*run it\b.*?"
    r"(?:2\.\s*)?View raw script\b.*?"
    r"(?:3\.\s*)?No\b.*?"
    r"(?:Esc to cancel|Tab to amend|ctrl\+g to edit script)",
    re.IGNORECASE | re.DOTALL,
)

ANSI_RE = re.compile(
    r"\x1b\][^\x07]*(?:\x07|\x1b\\)"      # OSC
    r"|\x1b\[[0-?]*[ -/]*[@-~]"            # CSI
    r"|\x1b[@-Z\\-_]"                      # 2-byte ESC
)

# Stream-mode prompts are read from raw terminal output as it is emitted,
# before alternate-screen redraws can push them out of capture-pane. Keep
# these patterns deliberately narrow so regular prose in agent output does
# not get approved.
STREAM_PROCEED_MENU = re.compile(
    r"(?:Permission rule\s+\S+\s+requires confirmation.*?)?"
    r"Do you want to proceed\?.{0,400}?"
    r"(?:[→❯]\s*)?(?:1\.\s*)?Yes\b.{0,160}?"
    r"(?:2\.\s*)?No\b.{0,300}?"
    r"(?:Esc to cancel|Tab to amend|ctrl\+e to explain|ctrl\\u002Be to explain)",
    re.IGNORECASE | re.DOTALL,
)
STREAM_RUN_MENU = re.compile(
    r"(Run this command\?|Write to this file\?|Create this file\?|"
    r"Delete this file\?|Allow this web search\?|Allow this web fetch\?).*?"
    r"[→❯]\s*(?:\d+\.\s*)?"
    r"(Run|Write|Create|Delete|Fetch|Search|Proceed)\b",
    re.IGNORECASE | re.DOTALL,
)
STREAM_WORKFLOW_MENU = re.compile(
    r"Run a dynamic workflow\?.{0,1200}?"
    r"[→❯]\s*(?:1\.\s*)?Yes,\s*run it\b.{0,500}?"
    r"(?:Esc to cancel|Tab to amend|ctrl\+g to edit script)",
    re.IGNORECASE | re.DOTALL,
)
STREAM_MODE_SWITCH_MENU = re.compile(
    r"Switch to Agent mode\?.{0,800}?"
    r"[→❯]\s*Approve\s+mode\s+switch(?:\s*\(y\))?.{0,300}?"
    r"Reject\s*\(n\s+or\s+esc\)",
    re.IGNORECASE | re.DOTALL,
)

# How far up from the LAST NON-EMPTY LINE the arrow can appear and
# still be considered "live". An actively-displayed prompt ends near
# the bottom of the visible pane, but Cursor sometimes pads it with
# several rows of hint text ("ctrl+r to review edits", a progress
# spinner, status bar, blank separators) before the absolute bottom.
#
# Why we anchor on the last non-empty line (instead of the absolute
# pane bottom): with a tall tmux window (e.g. 75-row dashboard pane)
# Cursor leaves the prompt block near the visual middle of the pane
# and fills the rest with trailing blanks. Concretely a 3-option
# prompt like
#     → Fetch (y)
#       Always allow github.com (tab)
#       Skip (esc or n)
#                                       ctrl+r to review changed files
#                                       <~25 blank rows>
# put the arrow ~29 rows above the absolute bottom — which used to
# silently miss the 15-row window. Anchoring on last-non-empty fixes
# this while still rejecting genuinely stale prompts: a stale prompt
# in scrollback would have *real* content (later command output)
# below it, pushing the last-non-empty anchor way past the arrow.
MAX_TAIL_LINES = 15

# How many lines above the arrow to scan for the matching question.
# Cursor prints the to-be-run command verbatim between the question
# and the arrow; long `python3 -c "..."` blocks can wrap into 60+
# visible rows. We therefore allow a very generous window — "is
# there a question anywhere in scrollback above this live arrow?"
# On its own that sounds too loose, but remember the arrow check is
# already a hard gate (only live prompts show the arrow), and the
# question regex is specific enough that unrelated chatter about
# "Run this command?" won't match. Bumped from 60 after seeing
# real long-command prompts miss the window.
MAX_BLOCK_LINES = 500


def strip_terminal_controls(text: str) -> str:
    text = ANSI_RE.sub("", text)
    # Convert carriage-return based redraws into searchable separators.
    text = text.replace("\r", "\n")
    while "\b" in text:
        next_text = re.sub(r".\b", "", text, count=1)
        if next_text == text:
            break
        text = next_text
    return text


def decide(pane: str) -> str:
    lines = pane.splitlines()
    if not lines:
        return ""

    # 1. Find arrow near the bottom. We anchor the search on the last
    #    NON-EMPTY line, not the absolute pane bottom — see the
    #    MAX_TAIL_LINES docstring above for rationale.
    last_nonempty = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            last_nonempty = i
            break
    if last_nonempty < 0:
        return ""
    arrow_idx = -1
    start = max(0, last_nonempty - MAX_TAIL_LINES + 1)
    for i in range(last_nonempty, start - 1, -1):
        if PERM_ARROW.search(lines[i]):
            arrow_idx = i
            break
    if arrow_idx < 0:
        return ""

    # 2. Walk up from the arrow to find the matching question.
    top = max(0, arrow_idx - MAX_BLOCK_LINES)
    question_idx = -1
    for i in range(arrow_idx - 1, top - 1, -1):
        if PERM_PATTERNS.search(lines[i]):
            question_idx = i
            break
    if question_idx < 0:
        local_block = "\n".join(lines[max(0, arrow_idx - 4):arrow_idx + 8])
        if WORKFLOW_MENU_VISIBLE.search(local_block):
            return "enter"
        return ""

    # 3. Determine which key to send, based on the question text.
    question_line = lines[question_idx]
    if Y_QUESTIONS.search(question_line):
        return "y"
    return "enter"


def decide_stream(raw: str) -> str:
    text = strip_terminal_controls(raw)
    tail = text[-12000:]
    if STREAM_PROCEED_MENU.search(tail):
        return "enter"
    if STREAM_WORKFLOW_MENU.search(tail):
        return "enter"
    if STREAM_MODE_SWITCH_MENU.search(tail):
        return "y"
    match = STREAM_RUN_MENU.search(tail)
    if match:
        question = match.group(1)
        if Y_QUESTIONS.search(question):
            return "y"
        return "enter"
    return ""


def send_key(session: str, key: str) -> None:
    if key == "y":
        subprocess.run(["tmux", "send-keys", "-t", session, "y"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5)
    elif key == "enter":
        subprocess.run(["tmux", "send-keys", "-t", session, "Enter"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5)


def watch_stream(session: str) -> int:
    buf = ""
    last_sent = 0.0
    while True:
        chunk = sys.stdin.buffer.read1(4096)
        if not chunk:
            return 0
        buf += chunk.decode("utf-8", errors="ignore")
        if len(buf) > 24000:
            buf = buf[-12000:]
        key = decide_stream(buf)
        now = time.time()
        if key and now - last_sent > 1.5:
            send_key(session, key)
            print(f"auto-approved {key}", flush=True)
            buf = ""
            last_sent = now


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--stream":
        key = decide_stream(sys.stdin.read())
        if not key:
            return 1
        sys.stdout.write(key)
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "--watch":
        return watch_stream(sys.argv[2])
    pane = sys.stdin.read()
    key = decide(pane)
    if not key:
        return 1
    sys.stdout.write(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
