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

_TTYD_INTERACTION_MARKER = "orch-ttyd-interactions-v1"
_TTYD_INTERACTION_SCRIPT = r"""<script id="orch-ttyd-interactions-v1">
(() => {
  const install = () => {
    const terminal = window.term;
    const core = terminal && terminal._core;
    const screen = document.querySelector(".xterm-screen");
    const mouse = core && core.coreMouseService;
    const selection = core && core._selectionService;
    if (!terminal || !screen || !mouse || !selection) return false;
    if (mouse.__orchInteractionPatch) return true;

    let mode = "";
    let pendingUrl = "";
    let startX = 0;
    let startY = 0;
    const originalTriggerMouseEvent = mouse.triggerMouseEvent.bind(mouse);
    mouse.triggerMouseEvent = (event) => (
      mode ? false : originalTriggerMouseEvent(event)
    );
    mouse.__orchInteractionPatch = true;

    const cleanHttpUrl = (value) => {
      let url = String(value || "");
      // Terminal linkifiers do not consistently recognize CJK punctuation
      // as a URL boundary. In prose such as `https://example.test（details）`,
      // trim the annotation before opening the target instead of letting the
      // browser percent-encode it as part of the path.
      const cjkBoundary = url.search(/[（【《〈「『〔［｛，。；：！？、]/u);
      if (cjkBoundary >= 0) url = url.slice(0, cjkBoundary);
      url = url.replace(/[.,;:!?]+$/, "");
      // Markdown commonly leaves its closing delimiter next to a literal
      // URL. Drop only unmatched trailing delimiters so valid targets such
      // as `Function_(mathematics)` remain intact.
      for (const [opening, closing] of [["(", ")"], ["[", "]"], ["{", "}"]]) {
        let balance = 0;
        for (const character of url) {
          if (character === opening) balance += 1;
          else if (character === closing) balance -= 1;
        }
        while (balance < 0 && url.endsWith(closing)) {
          url = url.slice(0, -1);
          balance += 1;
        }
      }
      url = url.replace(/[.,;:!?]+$/, "");
      return /^https?:\/\//i.test(url) ? url : "";
    };

    const coordsForEvent = (event) => {
      try {
        const coords = selection._getMouseBufferCoords(event);
        if (coords && coords.length === 2) return coords;
      } catch (_) {}
      const rect = screen.getBoundingClientRect();
      if (!rect.width || !rect.height) return null;
      const col = Math.max(0, Math.min(
        terminal.cols - 1,
        Math.floor((event.clientX - rect.left) * terminal.cols / rect.width),
      ));
      const row = Math.max(0, Math.min(
        terminal.rows - 1,
        Math.floor((event.clientY - rect.top) * terminal.rows / rect.height),
      ));
      return [col, terminal.buffer.active.viewportY + row];
    };

    const urlForEvent = (event) => {
      const coords = coordsForEvent(event);
      if (!coords) return "";
      const [col, row] = coords;
      const buffer = terminal.buffer.active;
      const line = row >= 0 && row < buffer.length
        ? buffer.getLine(row)
        : null;
      if (!line) return "";

      // Claude and Codex render Markdown links as OSC 8 hyperlinks: the
      // visible label may be `owner/repo#123` while the URL is stored in the
      // xterm cell metadata. Check that metadata before falling back to
      // searching for a literal https:// string on the screen.
      try {
        const cell = line.getCell(col);
        const extended = cell && cell.extended;
        const urlId = Number(
          extended && (extended.urlId || extended._urlId || 0),
        );
        const service = core && core._oscLinkService;
        if (urlId && service && typeof service.getLinkData === "function") {
          const linkData = service.getLinkData(urlId);
          const oscUrl = typeof linkData === "string"
            ? linkData
            : String((linkData && (linkData.uri || linkData.url)) || "");
          const cleanOscUrl = cleanHttpUrl(oscUrl);
          if (cleanOscUrl) return cleanOscUrl;
        }
      } catch (_) {}

      let first = row;
      while (first > 0 && buffer.getLine(first)?.isWrapped) first -= 1;
      let last = row;
      while (last + 1 < buffer.length && buffer.getLine(last + 1)?.isWrapped) {
        last += 1;
      }
      let text = "";
      for (let index = first; index <= last; index += 1) {
        text += buffer.getLine(index)?.translateToString(false) || "";
      }
      const offset = (row - first) * terminal.cols + col;
      const pattern = /https?:\/\/[^\s<>"'`]+/g;
      for (const match of text.matchAll(pattern)) {
        const raw = match[0];
        const url = cleanHttpUrl(raw);
        const begin = match.index || 0;
        if (offset >= begin && offset < begin + url.length) return url;
      }
      return "";
    };

    const clearMode = () => {
      mode = "";
      pendingUrl = "";
    };

    screen.addEventListener("mousedown", (event) => {
      if (event.button !== 0) return;
      startX = event.clientX;
      startY = event.clientY;
      if (event.altKey) {
        // Older xterm.js releases still emit a mouse-release report after an
        // Option-drag selection. tmux redraws on that report and erases the
        // selection. Keep mouse reporting muted through the matching mouseup.
        mode = "selection";
        return;
      }
      pendingUrl = urlForEvent(event);
      if (!pendingUrl) return;
      mode = "link";
    }, true);

    screen.addEventListener("mousemove", (event) => {
      if (event.buttons) return;
      const layer = screen.querySelector(".xterm-link-layer");
      if (layer) layer.style.cursor = urlForEvent(event) ? "pointer" : "default";
    }, true);

    document.addEventListener("mouseup", (event) => {
      if (mode === "selection") {
        // Let xterm finalize the selection first; keep the mouse report muted
        // until every listener for this mouseup event has run.
        setTimeout(clearMode, 0);
        return;
      }
      if (mode !== "link") return;
      const moved = Math.hypot(
        event.clientX - startX,
        event.clientY - startY,
      ) > 6;
      // ttyd's built-in xterm handler displays a confirmation dialog for
      // every OSC 8 link. Handle the validated http(s) URL here, before that
      // mouseup handler runs, so one click opens one tab without a prompt.
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!moved) {
        window.open(pendingUrl, "_blank", "noopener,noreferrer");
      }
      clearMode();
    }, true);

    window.addEventListener("blur", clearMode);
    return true;
  };

  if (install()) return;
  let attempts = 0;
  const timer = setInterval(() => {
    attempts += 1;
    if (install() || attempts >= 100) clearInterval(timer);
  }, 50);
})();
</script>"""


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


class TtydOutputThemeMapper:
    """Map a TUI's explicit true-color black background to the pane theme.

    Recent Codex TUIs paint every cell with ``ESC[48;2;0;0;0m``.  That is an
    explicit RGB color, so xterm's configured background and ANSI ``black``
    palette cannot override it.  ttyd prefixes terminal-output WebSocket
    messages with the byte ``0``; this mapper rewrites only that exact
    background sequence and preserves every other color and protocol message.
    """

    _BLACK_BACKGROUND = b"\x1b[48;2;0;0;0m"

    def __init__(self, theme: str):
        palette = _TTYD_THEME_PALETTES.get(normalize_terminal_theme(theme))
        background = palette.get("background", "") if palette else ""
        self._replacement = b""
        if len(background) == 7 and background.startswith("#"):
            try:
                red = int(background[1:3], 16)
                green = int(background[3:5], 16)
                blue = int(background[5:7], 16)
                self._replacement = (
                    f"\x1b[48;2;{red};{green};{blue}m".encode("ascii")
                )
            except ValueError:
                pass
        self._pending = b""

    def transform(self, message):
        if not self._replacement or not isinstance(message, bytes):
            return [message]
        if not message.startswith(b"0"):
            output = []
            if self._pending:
                output.append(b"0" + self._pending)
                self._pending = b""
            output.append(message)
            return output

        data = self._pending + message[1:]
        keep = 0
        maximum = min(len(data), len(self._BLACK_BACKGROUND) - 1)
        for size in range(maximum, 0, -1):
            if data.endswith(self._BLACK_BACKGROUND[:size]):
                keep = size
                break
        body = data[:-keep] if keep else data
        self._pending = data[-keep:] if keep else b""
        body = body.replace(self._BLACK_BACKGROUND, self._replacement)
        return [b"0" + body] if body else []

    def finish(self):
        if not self._pending:
            return None
        message = b"0" + self._pending
        self._pending = b""
        return message


def patch_ttyd_index_interactions(content: bytes) -> bytes:
    """Add reliable link and Option-drag behavior to ttyd's xterm page.

    ttyd 1.7.x bundles an xterm.js release that can emit a final tmux mouse
    report after an Option-drag selection. The resulting redraw immediately
    clears the selection. The same mouse-reporting path consumes ordinary URL
    clicks. Inject a small, idempotent compatibility layer into the HTML page;
    it leaves keyboard input and normal tmux mouse behavior unchanged.
    """
    if not content or _TTYD_INTERACTION_MARKER.encode() in content:
        return content
    text = content.decode("utf-8", "ignore")
    if "</body>" not in text:
        return content
    return text.replace(
        "</body>", _TTYD_INTERACTION_SCRIPT + "</body>", 1,
    ).encode("utf-8")
