# Contributing

Thanks for helping improve Agent Orchestrator.

## Development setup

1. Use Python 3.10 or newer.
2. Install dependencies with `python -m pip install -r requirements.txt`.
3. Install at least one supported agent CLI and `tmux` for manual integration
   testing.

Keep machine-specific shortcuts in `dashboard.local.json`, private task
recipes in `tasks/private/`, and runtime data in `outputs/`. These paths are
ignored by Git. Never include tokens, transcripts, local paths, or private
prompts in an issue or pull request.

## Checks

Run the same lightweight checks used in continuous integration:

```bash
python -m unittest discover -s tests -v
python -m py_compile *.py launchd/*.py
bash -n ./*.sh launchd/*.sh
```

For dashboard changes, also start a local instance, create a background
session, attach a terminal pane, send input, and stop the session cleanly.

## Pull requests

- Keep each change focused and explain its user-visible behavior.
- Add or update tests for logic changes.
- Update both `README.md` and `README_CN.md` when user-facing instructions
  change.
- Preserve the safe defaults: localhost binding without a token, mandatory
  authentication for non-loopback binds, and ignored runtime data.

Maintainers preparing a public snapshot should also follow `RELEASING.md`.
