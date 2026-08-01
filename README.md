# Agent Orchestrator

[Chinese documentation](README_CN.md)

Agent Orchestrator is a local dashboard for running and supervising multiple
CLI coding agents. It starts agents in tmux sessions, captures their output,
lets you arrange them in a browser grid, and supports stop/resume workflows when
the underlying agent CLI exposes enough session metadata.

The currently supported workflow is dashboard-first:

1. Start the dashboard.
2. Create sessions from the browser.
3. Prefer background sessions to avoid opening many terminal windows.
4. Use the dashboard to watch, send input, stop, and resume agents.

The older YAML recipe runner still exists, but it is not the primary supported
path. Treat it as an advanced or legacy mode.

## What It Supports

- Cursor Agent CLI, Claude Code, and OpenAI Codex CLI.
- Background session start, with optional terminal launch.
- Multi-pane dashboard layouts: `1`, `2`, `3`, `2x2`, `3x2`, `3x3`, `4x3`,
  and `5x3`.
- Drag sessions from the sidebar into panes.
- Per-pane input, copy, escape, close, and mode controls.
- Plain text log streaming and optional ttyd full-terminal mode.
- Stop and save resume metadata when possible.
- Resume ended sessions from recovered CLI resume commands.
- Local or remote browser access with token-based authentication.

## Requirements

- macOS or Linux with `tmux`
- Python 3.10+
- One or more supported agent CLIs:
  - Cursor Agent CLI as `agent`
  - Claude Code as `claude`
  - OpenAI Codex CLI as `codex`
- Python dependencies from `requirements.txt`
- Optional: `ttyd` for full browser terminal interaction

Install dependencies:

```bash
pip install -r requirements.txt
```

Examples below assume `orch` points to `orchestrator.py` in this repository.
If you do not have that alias or wrapper, run `python3 orchestrator.py ...`
from the repo root.

## Quick Start

Start the dashboard:

```bash
orch dashboard
```

The default bind address is `127.0.0.1`, so this command is local-only. A
cached token is reused when present; otherwise local-only mode runs without
authentication.

Open:

```text
http://127.0.0.1:7860
```

For LAN, VPN, or tunnel access, use a token:

```bash
ORCH_DASHBOARD_TOKEN=mysecret orch dashboard --host 0.0.0.0 --https
```

In the dashboard, use the new-session control to choose an agent and start it.
The recommended default is **Start in Background**. Use **Open Terminal and
Start** only when you explicitly want a local terminal window.

### Local Dashboard Configuration

Copy `dashboard.local.example.json` to `dashboard.local.json` to configure
machine-specific shortcuts and paths without committing them. The local file
is ignored by Git. Supported fields are `notes_url`, `projects_browser_url`,
`git_status_url`, and `projects_root`.

The environment variables `ORCH_NOTES_URL`, `ORCH_PROJECTS_BROWSER_URL`,
`ORCH_GIT_STATUS_URL`, and `ORCH_PROJECTS_ROOT` override the corresponding
file values. Set `ORCH_DASHBOARD_CONFIG` to load the JSON file from a different
location.

### Local Runtime Directories

Runtime data defaults to `outputs/` beside the source tree. Set
`ORCH_OUTPUTS_DIR` to an absolute path to keep session metadata and logs
elsewhere; the dashboard, session launcher, continuation commands, link
commands, and YAML runner all use the same override. `orch dashboard
--outputs PATH` still takes precedence for that process.

`orch organize` stores archived session material beside the output directory
in `projects/`. Set `ORCH_PROJECTS_DIR` when that archive needs a separate
location.

## Dashboard Workflow

### Start Sessions

From the browser you can create sessions for Cursor, Claude, or Codex. A
session gets a tmux session, an output directory, and a captured log file.

Background start is usually the better default because it avoids creating many
terminal windows. The dashboard can still interact with the tmux session.

### Arrange Panes

Use the layout selector to switch between compact and large grids. Drag a
session from the sidebar into any pane, or use modifier-click from the sidebar
to pin multiple sessions.

### Send Input

Each pane has its own input box. Press Enter to send, and Shift+Enter for a
newline. The dashboard sends text through tmux to the underlying agent.

### Stop And Resume

The Stop control tries to terminate the agent cleanly and save resume metadata.
Resume support depends on each CLI:

- Codex: `codex resume <session-id>`
- Claude Code: `claude --resume <session-id>`
- Cursor Agent: `agent --resume <chat-id>`

If a resume command is found, the session appears in the resume UI when you
create a new session.

## CLI Session Mode

You can also start one session directly from the terminal:

```bash
# Cursor Agent, the default
orch run

# Claude Code
orch run claude

# OpenAI Codex CLI
orch run codex

# Custom label and working directory
orch run cursor fix-bug /path/to/project
orch run claude review-pr /path/to/project
orch run codex investigate-bug /path/to/project
```

Supported shortcuts include `--model`, `--effort` (Claude Code only),
`--fast`, `--think`, `--opus`, `--sonnet`, `--codex`, and `--codex-high`.

## Remote Access

Authentication is mandatory whenever the dashboard binds beyond localhost.
The CLI refuses a non-loopback `--host` unless a token is configured.

```bash
# LAN or Tailscale
ORCH_DASHBOARD_TOKEN=mysecret orch dashboard --host 0.0.0.0 --https

# Cloudflare Tunnel
ORCH_DASHBOARD_TOKEN=mysecret orch dashboard --host 127.0.0.1
cloudflared tunnel --url http://localhost:7860

# ngrok
ORCH_DASHBOARD_TOKEN=mysecret orch dashboard --host 127.0.0.1
ngrok http 7860
```

Browser clipboard APIs require a secure context for non-localhost origins. Use
`--https` on LAN or VPN, or use a tunnel that provides HTTPS.

## URL Helper

```bash
orch url            # Auto-detect HTTP/HTTPS, print and copy the best URL
orch url -q         # Print only the URL
orch url --json     # Print all candidate interfaces
orch url --no-copy  # Do not copy to the clipboard
```

The helper reads `ORCH_DASHBOARD_TOKEN` first, then the local token cache at
`~/.config/agent-orchestrator/dashboard-token`. Without a token it returns
only a localhost URL. Set `ORCH_DASHBOARD_TOKEN_FILE` to use a different cache
path. Use `--https` or `--no-https` only when the dashboard is not currently
running and you need to override the fallback scheme.

With `--publish-icloud`, the dashboard writes the current URL to:

```text
~/iCloud Drive/orch-dashboard.txt
```

## macOS LaunchAgent

The `launchd/` scripts can install the dashboard as a user LaunchAgent:

```bash
./launchd/deploy.sh --install
./launchd/deploy.sh
./launchd/deploy.sh --dry-run
```

The first install generates a strong random dashboard token and stores it in
the local token cache with user-only permissions. Set
`ORCH_DASHBOARD_TOKEN` during installation if you prefer an explicit token.

The deploy script syncs the repo to a launchd-friendly live directory such as
`~/projects/agent-orchestrator/`. This avoids macOS TCC issues where background
LaunchAgent processes may be blocked from reading `~/Documents`, `~/Desktop`,
or `~/Downloads`.

## Advanced: YAML Recipe Runner

The repository still includes an older YAML-based orchestration mode:

```bash
orch start tasks/example.yaml
orch resume outputs/example-20260515-120000
orch status
```

This mode can run multi-task recipes with dependencies, but it is not where most
recent dashboard work has focused. For new usage, prefer dashboard-managed
background sessions.

`tasks/example.yaml` is intentionally machine-independent. Keep recipes that
contain local paths, private prompts, or log references in the ignored
`tasks/private/` directory rather than committing them.

## Architecture

- `dashboard.py`: FastAPI backend, tmux integration, session discovery, resume
  metadata recovery, and ttyd proxying.
- `static/index.html`: single-file browser UI.
- `run.sh`: lightweight session launcher used by dashboard-created sessions.
- `outputs/`: runtime logs, session metadata, and dashboard state.
- `tmux`: process supervision and terminal capture layer.

## Security Notes

- A remotely reachable dashboard can send input to local tmux sessions.
- Non-loopback binds require `--token` or `ORCH_DASHBOARD_TOKEN`.
- Runtime directories can contain transcripts, prompts, logs, and local paths.
  Do not publish `outputs/`, `projects/`, or `.dashboard-certs/`.

See [SECURITY.md](SECURITY.md) for vulnerability reporting and deployment
guidance.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, checks, and pull
request guidelines.

## License

Agent Orchestrator is available under the [MIT License](LICENSE).

## Known Limitations

- Resume support is best-effort and depends on the agent CLI.
- ttyd rendering can still be affected by terminal UI behavior from individual
  agent CLIs.
- YAML orchestration is legacy and less polished than the dashboard workflow.
- The project is currently optimized for local developer machines rather than
  hosted multi-user deployments.
