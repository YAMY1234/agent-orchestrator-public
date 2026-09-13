# Agent-to-agent delegation

Agent Orchestrator can expose its tmux sessions as a small, scriptable control
surface. An agent can create a child session, inspect a bounded part of another
session's terminal history, and send it a follow-up without taking over the
browser.

## Create a child task

Run this inside an Orchestrator-launched session:

```bash
orch delegate \
  --agent codex \
  --model gpt-5.6-sol \
  --effort high \
  --label dependency-audit \
  --priority p1 \
  --idempotency-key dependency-audit-v1 \
  --prompt "Audit the dependency update, run targeted tests, and report the exact evidence."
```

The current `ORCH_RUN_ID` becomes the parent automatically. Unless overridden,
the child inherits the parent's working directory, terminal theme, and Linked
Items. The child runs in its own background tmux session and appears in the
same Dashboard like any other task.

Claude Code uses the same interface; model and effort remain separate fields:

```bash
orch delegate \
  --agent claude \
  --model opus \
  --effort xhigh \
  --label api-review \
  --prompt-file ./review-prompt.md
```

An idempotency key is optional but recommended for automation. Repeating the
same request with the same parent and key returns the existing child; reusing
that key with different parameters is rejected.

## Discover and inspect sessions

```bash
orch session list --alive
orch session list --alive --json
orch session status <run-id>
orch session status <run-id> --json

orch session read "$ORCH_RUN_ID" --head -n 80
orch session read <run-id> -n 200
orch session read <run-id> -n 200 --json
```

`read` returns joined plain text from tmux while the session is alive and falls
back to the persisted log after it exits. Reads are explicitly bounded to
1–5,000 lines, so an agent does not need to ingest an entire transcript just
to understand recent state.

## Send a follow-up

```bash
orch session send <run-id> "Please report the failing test name and stop."
orch session send <run-id> --file ./follow-up.md
```

The default submits the text with Enter. Add `--no-enter` to stage text in the
target input without submitting it.

Codex goals use the same message path, for example:

```bash
orch session send <run-id> "/goal Investigate the regression and preserve evidence."
orch session send <run-id> "/goal edit"
orch session send <run-id> "/goal clear"
```

## Change model or effort in place

When a Codex or Claude Code session is waiting at its normal input prompt, its
runtime selection can be changed without recreating the session:

```bash
orch session configure <run-id> --model gpt-5.6-sol --effort high
orch session configure <run-id> --model fable --effort xhigh
```

The command drives the agent's native model picker and rejects unavailable
choices instead of silently substituting another model or effort. Claude Code
changes apply only to the selected session and do not alter its default for
future sessions. Configuration is rejected while the agent is working.

## Local and remote nodes

New sessions receive `ORCH_DASHBOARD_URL`, pointing at the Dashboard on their
own machine. Older sessions fall back to local discovery on ports 7861 and
7860. Therefore an agent running on a remote execution node creates and
controls remote tmux sessions directly; the local aggregating Dashboard sees
the result through normal Remote Nodes federation.

From the local machine, delegation can also target a configured node:

```bash
orch delegate --node devbox --agent codex --prompt "Run the remote smoke test."
```

## API surface

The CLI is a thin client for these authenticated Dashboard routes:

- `POST /api/delegate`
- `GET /api/sessions`
- `GET /api/sessions/{run_id}`
- `GET /api/sessions/{run_id}/read?lines=200&position=tail`
- `POST /api/sessions/{run_id}/send`
- `POST /api/sessions/{run_id}/runtime-config`

`POST /api/delegate` accepts `parent_run_id`, `agent`, `model`, `effort`,
`label`, `cwd`, `priority`, `prompt`, `inherit_linked_items`, and an optional
`idempotency_key`.

This is a powerful local-user capability: anyone who can authenticate to the
Dashboard API can read and interact with its sessions. Keep the Dashboard on
loopback or behind its token and an SSH/VPN tunnel; do not expose it directly
to an untrusted network.
