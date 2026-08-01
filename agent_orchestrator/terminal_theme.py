"""Terminal theme normalization and ttyd HTML patching."""

from __future__ import annotations

import json
import os


_TTYD_SOFT_DARK_THEME = {
    "background": "#1f242c",
    "foreground": "#d6deeb",
    "cursor": "#f0f6fc",
    "cursorAccent": "#1f242c",
    "selectionBackground": "#3b4252",
    "black": "#1f242c",
    "red": "#ff7b72",
    "green": "#7ee787",
    "yellow": "#d29922",
    "blue": "#79c0ff",
    "magenta": "#d2a8ff",
    "cyan": "#56d4dd",
    "white": "#d6deeb",
    "brightBlack": "#8b949e",
    "brightRed": "#ffa198",
    "brightGreen": "#aff5b4",
    "brightYellow": "#e3b341",
    "brightBlue": "#a5d6ff",
    "brightMagenta": "#d8b9ff",
    "brightCyan": "#7ee0e8",
    "brightWhite": "#f0f6fc",
}
_TTYD_SOFT_LIGHT_THEME = {
    "background": "#e8eef6",
    "foreground": "#1f2937",
    "cursor": "#1f2937",
    "cursorAccent": "#e8eef6",
    "selectionBackground": "#cbd5e1",
    "black": "#1f2937",
    "red": "#cf222e",
    "green": "#116329",
    "yellow": "#953800",
    "blue": "#0969da",
    "magenta": "#8250df",
    "cyan": "#1b7c83",
    "white": "#dbe3ee",
    "brightBlack": "#475569",
    "brightRed": "#a40e26",
    "brightGreen": "#1a7f37",
    "brightYellow": "#9a6700",
    "brightBlue": "#218bff",
    "brightMagenta": "#a475f9",
    "brightCyan": "#3192aa",
    "brightWhite": "#f8fafc",
}
_TTYD_SOFT_GREEN_THEME = {
    "background": "#eef7ee",
    "foreground": "#1f2f24",
    "cursor": "#1f2f24",
    "cursorAccent": "#eef7ee",
    "selectionBackground": "#cfe8d0",
    "black": "#1f2f24",
    "red": "#b3261e",
    "green": "#137333",
    "yellow": "#8a5a00",
    "blue": "#1769aa",
    "magenta": "#7a4fb3",
    "cyan": "#1b7f73",
    "white": "#dceadf",
    "brightBlack": "#5f7165",
    "brightRed": "#d93025",
    "brightGreen": "#188038",
    "brightYellow": "#9a6700",
    "brightBlue": "#1a73e8",
    "brightMagenta": "#9334e6",
    "brightCyan": "#129e8f",
    "brightWhite": "#fbfff9",
}
_TTYD_LIGHT_THEME = {
    "background": "#ffffff",
    "foreground": "#111827",
    "cursor": "#111827",
    "cursorAccent": "#ffffff",
    "selectionBackground": "#bfdbfe",
    "black": "#111827",
    "red": "#b91c1c",
    "green": "#15803d",
    "yellow": "#a16207",
    "blue": "#1d4ed8",
    "magenta": "#7e22ce",
    "cyan": "#0e7490",
    "white": "#e5e7eb",
    "brightBlack": "#6b7280",
    "brightRed": "#dc2626",
    "brightGreen": "#16a34a",
    "brightYellow": "#ca8a04",
    "brightBlue": "#2563eb",
    "brightMagenta": "#9333ea",
    "brightCyan": "#0891b2",
    "brightWhite": "#111827",
}
_TTYD_THEME_PALETTES = {
    "soft-dark": _TTYD_SOFT_DARK_THEME,
    "soft-light": _TTYD_SOFT_LIGHT_THEME,
    "soft-green": _TTYD_SOFT_GREEN_THEME,
    "light": _TTYD_LIGHT_THEME,
}
_TTYD_DARK_THEME_JS = (
    'theme:{foreground:"#d2d2d2",background:"#2b2b2b",cursor:"#adadad",'
    'black:"#000000",red:"#d81e00",green:"#5ea702",yellow:"#cfae00",'
    'blue:"#427ab3",magenta:"#89658e",cyan:"#00a7aa",white:"#dbded8",'
    'brightBlack:"#686a66",brightRed:"#f54235",brightGreen:"#99e343",'
    'brightYellow:"#fdeb61",brightBlue:"#84b0d8",brightMagenta:"#bc94b7",'
    'brightCyan:"#37e6e8",brightWhite:"#f1f1f0"}'
)


def normalize_terminal_theme(theme: str) -> str:
    theme = (theme or "").strip().lower().replace("_", "-").replace(" ", "-")
    if theme in {"dark", "default"}:
        return ""
    if theme == "white":
        return "light"
    if theme in _TTYD_THEME_PALETTES:
        return theme
    return ""


def ttyd_theme_client_option(theme: str = "") -> str:
    theme = normalize_terminal_theme(theme) or normalize_terminal_theme(
        os.environ.get("ORCH_TTYD_THEME", "")
    )
    palette = _TTYD_THEME_PALETTES.get(theme)
    if palette:
        return "theme=" + json.dumps(palette, separators=(",", ":"))
    return ""


def _ttyd_theme_js(theme: str) -> str:
    theme = normalize_terminal_theme(theme)
    palette = _TTYD_THEME_PALETTES.get(theme)
    if not palette:
        return _TTYD_DARK_THEME_JS
    pairs = ",".join(
        f'{key}:{json.dumps(value)}' for key, value in palette.items()
    )
    return "theme:{" + pairs + "}"


def patch_ttyd_index_theme(content: bytes, theme: str) -> bytes:
    """Patch ttyd's bundled xterm termOptions theme for themed panes."""
    if not normalize_terminal_theme(theme) or not content:
        return content
    text = content.decode("utf-8", "ignore")
    patched = text.replace(_TTYD_DARK_THEME_JS, _ttyd_theme_js(theme), 1)
    if patched == text:
        return content
    return patched.encode("utf-8")
