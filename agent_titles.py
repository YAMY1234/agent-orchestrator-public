"""Auto-extract session titles from Cursor/Claude/Codex transcripts.

Heuristics:
- Cursor:  ~/.cursor/projects/<cwd-encoded>/agent-transcripts/<uuid>/<uuid>.jsonl
           first line is {role: "user", message: {...}} or {content: ...}
- Claude:  ~/.claude/projects/<cwd-encoded>/*.jsonl
           first line with type=user has message.content (string or list)
- Codex:   ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
           First line is {type: "session_meta", payload: {cwd, ...}};
           subsequent lines have {type: "response_item",
           payload: {type: "message", role: "user", content: [...]}}.
           We filter by `payload.cwd` (no per-cwd directory like cursor/claude)
           and skip the synthetic AGENTS.md / environment_context messages
           that codex injects at the top of every session — see _CODEX_NOISE.

We pick the transcript whose birth time is closest to session.started_at,
falling back to the most recently modified transcript in the project dir.
This works well for the typical `orch run` pattern (one agent per cwd at a
time), and gracefully degrades if there's ambiguity.

No guarantees — this is a best-effort display hint, not a source of truth.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _cursor_encode(cwd: str) -> str:
    # Cursor: /Users/alice/projects/demo -> Users-alice-projects-demo
    p = cwd.strip("/")
    return p.replace("/", "-")


def _claude_encode(cwd: str) -> str:
    # Claude: /Users/alice/projects/demo -> -Users-alice-projects-demo
    return "-" + cwd.strip("/").replace("/", "-")


def _parse_iso(ts: str) -> Optional[float]:
    if not ts:
        return None
    # Accept both "2026-04-17T01:19:41" and "2026-04-17T01:19:41Z"
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts, fmt).timestamp()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Content sanitizers
# ---------------------------------------------------------------------------

# The Cursor agent CLI often wraps user prompts in <user_query>..</user_query>.
_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
_SYSTEM_REMINDER_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.DOTALL)


def _clean(text: str) -> str:
    if not text:
        return ""
    m = _USER_QUERY_RE.search(text)
    if m:
        text = m.group(1)
    text = _SYSTEM_REMINDER_RE.sub("", text)
    text = text.strip()
    # Collapse whitespace and drop leading/trailing quotes/brackets.
    text = re.sub(r"\s+", " ", text)
    return text


def _summarize(text: str, max_len: int = 60) -> str:
    text = _clean(text)
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Extract from transcript content (list-of-parts or plain string)
# ---------------------------------------------------------------------------

def _content_to_text(content) -> str:
    """Flatten the various transcript content shapes into plain text.

    - Cursor/Claude: list of {type: "text", text: "..."} or strings
    - Codex:         list of {type: "input_text"|"output_text", text: "..."}
    The fallback `elif "text" in item` catches both `type=text` and
    `type=input_text` without us having to special-case codex.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and "text" in item:
                    parts.append(item["text"])
                elif "text" in item:
                    parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if isinstance(content, dict):
        return content.get("text", "") or ""
    return str(content)


def _first_user_message(jsonl_path: Path) -> Optional[str]:
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Cursor transcripts: top-level {role, message}
                # Claude transcripts: {type, message}
                is_user = (
                    obj.get("role") == "user"
                    or obj.get("type") == "user"
                    or (isinstance(obj.get("message"), dict)
                        and obj["message"].get("role") == "user")
                )
                if not is_user:
                    continue
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
                content = msg.get("content")
                if content is None:
                    # Claude sometimes stores content directly under obj
                    content = obj.get("content")
                text = _content_to_text(content)
                cleaned = _clean(text)
                if cleaned:
                    return cleaned
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Transcript discovery
# ---------------------------------------------------------------------------

def _cursor_transcripts(cwd: str) -> list[Path]:
    root = Path.home() / ".cursor" / "projects" / _cursor_encode(cwd) / "agent-transcripts"
    if not root.exists():
        return []
    out: list[Path] = []
    for sub in root.iterdir():
        if sub.is_dir():
            jsonl = sub / f"{sub.name}.jsonl"
            if jsonl.exists():
                out.append(jsonl)
    return out


def _claude_transcripts(cwd: str) -> list[Path]:
    root = Path.home() / ".claude" / "projects" / _claude_encode(cwd)
    if not root.exists():
        return []
    return list(root.glob("*.jsonl"))


# ---------------------------------------------------------------------------
# Codex transcripts.
#
# Codex stores everything under `~/.codex/sessions/YYYY/MM/DD/`, NOT under a
# per-cwd directory like cursor/claude. We can't compute the file path from
# `cwd`; we have to scan recent date folders and read each file's first line
# (a `session_meta` event) to learn the cwd it was launched in.
#
# Cost containment: we only scan today + yesterday by default. With one
# session per ~minute that's at most ~1500 files; reading just the first
# line each is cheap (<50ms total even on a slow disk). For older sessions
# the user's cwd-based heuristic loses anyway — they would have started a
# new session if they wanted to keep working there.
# ---------------------------------------------------------------------------

_CODEX_ROOT = Path.home() / ".codex" / "sessions"


def _codex_transcripts(cwd: str, started_at_ts: Optional[float] = None,
                       scan_days: int = 2) -> list[Path]:
    """Return codex rollout files whose session_meta cwd == `cwd`.

    Limits the scan to the last `scan_days` daily folders to stay cheap.
    If `started_at_ts` is given and falls outside that window, also scan
    that day so the lookup still works for old sessions referenced by
    long-lived run dirs."""
    if not _CODEX_ROOT.is_dir():
        return []
    days_to_scan: set[Path] = set()
    now = time.time()
    for d in range(scan_days):
        ts = now - d * 86400
        dt = datetime.fromtimestamp(ts)
        days_to_scan.add(_CODEX_ROOT / dt.strftime("%Y") /
                         dt.strftime("%m") / dt.strftime("%d"))
    if started_at_ts:
        dt = datetime.fromtimestamp(started_at_ts)
        days_to_scan.add(_CODEX_ROOT / dt.strftime("%Y") /
                         dt.strftime("%m") / dt.strftime("%d"))
    out: list[Path] = []
    cwd_norm = (cwd or "").rstrip("/")
    for day_dir in days_to_scan:
        if not day_dir.is_dir():
            continue
        for fp in day_dir.glob("rollout-*.jsonl"):
            file_cwd = _codex_session_cwd(fp)
            if file_cwd and file_cwd.rstrip("/") == cwd_norm:
                out.append(fp)
    return out


def _codex_session_cwd(p: Path) -> str:
    """Read the first line of a codex rollout file and return its cwd.
    Empty string on any failure (file unreadable, JSON malformed, etc.).
    Cached per-process via a tiny LRU because many transcripts get
    consulted repeatedly on a polling dashboard."""
    cached = _CODEX_CWD_CACHE.get(p)
    if cached is not None:
        return cached
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            first = f.readline()
        obj = json.loads(first) if first else {}
        if obj.get("type") == "session_meta":
            cwd = obj.get("payload", {}).get("cwd", "") or ""
        else:
            cwd = ""
    except (OSError, json.JSONDecodeError):
        cwd = ""
    # Bounded cache: drop oldest entries if it gets too big. We never
    # invalidate (codex transcripts are append-only; cwd never changes).
    if len(_CODEX_CWD_CACHE) > 4096:
        _CODEX_CWD_CACHE.clear()
    _CODEX_CWD_CACHE[p] = cwd
    return cwd


_CODEX_CWD_CACHE: dict[Path, str] = {}


# Codex automatically injects a few synthetic "user" messages at the very
# start of every session — these are NOT what the human typed and would
# turn every sidebar title into "# AGENTS.md instructions for ..." or
# "<environment_context>...". We detect them by their distinctive
# leading content and skip them when picking the first real prompt.
_CODEX_NOISE = (
    "# AGENTS.md",
    "<environment_context",
    "<INSTRUCTIONS>",
    "<user_instructions",
    "<permissions instructions",
)


def _first_codex_user_message(jsonl_path: Path) -> Optional[str]:
    """Codex-specific first-user-message extractor that skips the auto-
    injected AGENTS.md / environment_context / instructions noise."""
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "response_item":
                    continue
                payload = obj.get("payload", {})
                if (payload.get("type") != "message"
                        or payload.get("role") != "user"):
                    continue
                content = payload.get("content")
                text = _content_to_text(content)
                stripped = text.lstrip()
                if any(stripped.startswith(noise) for noise in _CODEX_NOISE):
                    continue
                cleaned = _clean(text)
                if cleaned:
                    return cleaned
    except OSError:
        return None
    return None


# Max skew between `started_at` (recorded when we launched the agent)
# and the transcript directory's creation time. Cursor writes the
# transcript dir within a second or two of the process starting, so a
# ~5 min window is hugely generous but still tight enough that two
# sessions launched in the same cwd on the same day can't collide
# (they're always several minutes apart in practice).
_MATCH_WINDOW_SECONDS = 300.0


def _transcript_creation_ts(p: Path) -> float:
    """Return when this transcript was *created*, not last modified.

    Cursor: path is <uuid>/<uuid>.jsonl. The uuid directory's
            st_birthtime is exactly `agent` process start time.
    Claude: path is <file>.jsonl directly; use its own birthtime.
    We fall back to ctime then mtime if birthtime is unavailable
    (some filesystems / non-macOS).
    """
    try:
        # For Cursor, the parent dir's birth time is the truest signal;
        # the jsonl inside gets appended to continuously.
        target = p.parent if p.parent.name == p.stem else p
        st = target.stat()
    except OSError:
        return 0.0
    return getattr(st, "st_birthtime", None) or st.st_ctime or st.st_mtime


def _best_transcript(candidates: list[Path],
                     started_at_ts: Optional[float]) -> Optional[Path]:
    """Pick the transcript that belongs to a session with the given
    started_at. If we have no started_at we fall back to "most recent",
    which is fine for single-session-per-cwd setups but NOT safe when
    several sessions share a cwd (that's the sidebar-title-duplication
    bug we hit on 2026-04-19).

    With a started_at, we require the transcript's birthtime to fall
    within +/- _MATCH_WINDOW_SECONDS. If nothing matches we return
    None *instead* of falling back to "newest" — a wrong match is
    worse than no title at all, because the sidebar then shows an
    unrelated session's first message.
    """
    if not candidates:
        return None
    if started_at_ts is None:
        # No session start recorded: cache-key-level distinctness has
        # already been compromised upstream, so the best we can do is
        # surface the newest transcript. Keep old behavior here.
        return max(candidates, key=lambda p: _transcript_creation_ts(p) or 0.0)

    def delta(p: Path) -> float:
        t = _transcript_creation_ts(p)
        if not t:
            return float("inf")
        return abs(t - started_at_ts)

    best = min(candidates, key=delta)
    if delta(best) > _MATCH_WINDOW_SECONDS:
        return None
    return best


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_title(agent: str, cwd: str, started_at: str = "",
                  max_len: int = 60) -> Optional[str]:
    """Return a short title for the session, or None if not derivable.

    agent: "cursor" | "claude" | other (best-effort)
    cwd:   absolute path the agent was launched in
    started_at: ISO timestamp; used to disambiguate among multiple transcripts
    """
    if not cwd:
        return None
    started_ts = _parse_iso(started_at) if started_at else None

    def pick(cands: list[Path], extractor=_first_user_message) -> Optional[str]:
        best = _best_transcript(cands, started_ts)
        if not best:
            return None
        text = extractor(best)
        return _summarize(text, max_len=max_len) if text else None

    def pick_codex() -> Optional[str]:
        # Codex needs the dedicated extractor that skips AGENTS.md noise.
        return pick(_codex_transcripts(cwd, started_ts),
                    extractor=_first_codex_user_message)

    agent_l = (agent or "").lower()

    if agent_l.startswith("cursor") or agent_l == "agent":
        t = pick(_cursor_transcripts(cwd))
        if t:
            return t
        return pick(_claude_transcripts(cwd)) or pick_codex()

    if agent_l.startswith("claude"):
        t = pick(_claude_transcripts(cwd))
        if t:
            return t
        return pick(_cursor_transcripts(cwd)) or pick_codex()

    if agent_l.startswith("codex"):
        t = pick_codex()
        if t:
            return t
        return pick(_cursor_transcripts(cwd)) or pick(_claude_transcripts(cwd))

    # Unknown agent: try all three.
    for getter in (
        lambda: pick(_cursor_transcripts(cwd)),
        lambda: pick(_claude_transcripts(cwd)),
        pick_codex,
    ):
        t = getter()
        if t:
            return t
    return None


# ---------------------------------------------------------------------------
# Simple on-disk cache to avoid re-parsing transcripts every /api/sessions hit.
# Key: (cwd, agent, started_at) -> (title, cached_at).
# ---------------------------------------------------------------------------

class TitleCache:
    def __init__(self, ttl_seconds: float = 30.0):
        self._data: dict[tuple, tuple[Optional[str], float]] = {}
        self.ttl = ttl_seconds

    def get(self, agent: str, cwd: str, started_at: str) -> Optional[str]:
        key = (agent, cwd, started_at)
        hit = self._data.get(key)
        now = time.time()
        if hit and (now - hit[1]) < self.ttl:
            return hit[0]
        title = extract_title(agent, cwd, started_at)
        self._data[key] = (title, now)
        return title

    def invalidate(self, agent: str = "", cwd: str = "", started_at: str = ""):
        if not agent and not cwd:
            self._data.clear()
            return
        key = (agent, cwd, started_at)
        self._data.pop(key, None)
