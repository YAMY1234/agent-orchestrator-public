#!/usr/bin/env python3
"""Render the LaunchAgent plist without shell interpolation."""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path
from typing import Any


PLACEHOLDERS = (
    "__PYTHON__",
    "__PROJECT_DIR__",
    "__PATH__",
    "__TOKEN_FILE__",
    "__OUTPUTS_DIR__",
    "__HOME__",
    "__PORT__",
)


def _render(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for placeholder, replacement in replacements.items():
            value = value.replace(placeholder, replacement)
        return value
    if isinstance(value, list):
        return [_render(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, replacements) for key, item in value.items()}
    return value


def render_plist(template: Path, destination: Path,
                 values: list[str]) -> None:
    if len(values) != len(PLACEHOLDERS):
        raise ValueError(f"expected {len(PLACEHOLDERS)} replacement values")
    replacements = dict(zip(PLACEHOLDERS, values))
    with template.open("rb") as source:
        data = _render(plistlib.load(source), replacements)
    with destination.open("wb") as target:
        plistlib.dump(data, target, sort_keys=False)
    os.chmod(destination, 0o600)


def main() -> int:
    if len(sys.argv) != len(PLACEHOLDERS) + 3:
        print(
            f"usage: {Path(sys.argv[0]).name} TEMPLATE DESTINATION "
            + " ".join(PLACEHOLDERS),
            file=sys.stderr,
        )
        return 2
    render_plist(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
