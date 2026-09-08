# Remote Nodes

Remote Nodes let one browser Dashboard control coding-agent sessions that
actually run on other machines. The remote machine owns the agent process,
tmux session, files, and resume metadata; the local Dashboard only federates
its session inventory and proxies terminal input/output.

This feature is independent from workspace sync. You can keep every project
only on its execution machine and still use a single Dashboard.

## Architecture

```text
browser -> local Dashboard -> SSH tunnel -> remote node-only Dashboard -> tmux
```

Only terminal bytes and small session/status payloads cross the tunnel. The
remote tmux session keeps running when the browser or local Dashboard closes.
Use compatible Agent Orchestrator revisions on both machines.

## 1. Start a node-only Dashboard remotely

On the remote machine:

```bash
cd /path/to/agent-orchestrator-public
.venv/bin/python orchestrator.py dashboard \
  --node-only --host 127.0.0.1 --port 7861
```

`--node-only` prevents a remote node from loading its own `remote_nodes`
configuration and creating federation loops. Keep the service bound to
loopback and reach it through SSH.

If you set `ORCH_DASHBOARD_TOKEN` on the remote process, set the matching local
`token_env` configuration below. Store the value in the environment or a
secret manager, never in the tracked JSON file.

## 2. Create the SSH tunnel

For an interactive first test, run locally:

```bash
ssh -N -L 17861:127.0.0.1:7861 dev-server
```

Leave that command running. You should now be able to query the remote node
through `http://127.0.0.1:17861` on the local machine.

Agent Orchestrator can own the tunnel instead by setting `auto_tunnel` to
`true`. Automatic tunnels use non-interactive SSH (`BatchMode=yes`), so the
host must already work through an SSH agent, key, certificate, or another
non-interactive authentication method. Interactive or device-code login can
instead be modeled with the optional reconnect commands, all of which remain
private in `dashboard.local.json`.

## 3. Configure the local control plane

Copy `examples/dashboard.local.json` to the repository root as
`dashboard.local.json`, then enable and edit the node:

```json
{
  "projects_root": "~/Documents/Projects",
  "remote_nodes": [
    {
      "id": "dev",
      "label": "Remote dev",
      "enabled": true,
      "url": "http://127.0.0.1:17861",
      "projects_root": "/home/example/Projects",
      "token_env": "ORCH_REMOTE_DEV_TOKEN",
      "ssh_host": "dev-server",
      "local_port": 17861,
      "remote_port": 7861,
      "auto_tunnel": false,
      "poll_interval_seconds": 3,
      "reconnect": { "enabled": false }
    }
  ]
}
```

The file is ignored by Git. The browser receives only the node label, health,
and project root; tokens and reconnect commands remain server-side.

Restart the local Dashboard after changing its configuration. The sidebar
will group sessions by location, and remote panes support the same TTY,
priority, Mission Control, linked-item, and notification flows as local panes.

## Optional self-service reconnect

Reconnect is deliberately disabled in the example. When enabled, it executes
only the exact command arrays in your private configuration. It can probe
transport, launch or renew credentials, establish a tunnel, start the remote
service, and surface a generic browser verification URL/code.

Command values are installation-specific and should not be committed. Keep
every command narrowly scoped, avoid shell strings, and use JSON arrays such
as `["ssh", "dev-server", "command"]`. Review them with the same care as any
other local automation that can start processes or open SSH connections.

## Security checklist

- Keep both Dashboards on loopback and use an SSH or trusted HTTPS tunnel.
- Use a separate token per node when authentication is enabled.
- Put tokens in environment variables referenced by `token_env`.
- Do not publish `dashboard.local.json`, SSH keys, output directories, or
  native agent transcripts.
- Treat the local control plane as privileged: it can send keystrokes to every
  configured remote session.
- Disable or remove a node configuration before decommissioning its machine.
