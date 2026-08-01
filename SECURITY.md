# Security Policy

## Supported version

Security fixes target the latest version on the default branch.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub private
vulnerability reporting when it is available, or contact the maintainers
privately. Include reproduction steps and impact, but remove real tokens,
transcripts, local paths, and other personal data.

## Deployment model

Agent Orchestrator can send input to local tmux sessions and should be treated
as a privileged developer tool.

- The default dashboard bind is localhost-only.
- Non-loopback binds require a token.
- Use HTTPS or a trusted HTTPS tunnel for remote access.
- Do not publish `outputs/`, `projects/`, `.dashboard-certs/`, local
  configuration files, or private task recipes.
- Run the dashboard as an unprivileged user and expose it only to trusted
  networks.
