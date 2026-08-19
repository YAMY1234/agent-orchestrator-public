"""Incremental, content-free conversation statistics for native transcripts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent_titles import _CODEX_NOISE, _clean, _content_to_text


_SYSTEM_BLOCK_RE = re.compile(
    r"<system[-_]reminder>.*?</system[-_]reminder>", re.I | re.S,
)
_CLAUDE_SYNTHETIC_PREFIXES = (
    "<task-notification",
    "<local-command-",
    "<command-name",
    "This session is being continued from",
)
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _estimated_request_tokens(text: str) -> int:
    """Return a cheap, model-neutral estimate without retaining content.

    Native Codex/Claude transcripts do not attach token counts to individual
    user messages. UTF-8 bytes / 4 is a useful cross-language approximation:
    about four ASCII characters per token while CJK characters naturally
    contribute more bytes. The Dashboard labels this value as estimated.
    """
    if not text:
        return 0
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _event_timestamp(obj: dict[str, Any]) -> float:
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    return _timestamp(
        obj.get("timestamp") or message.get("timestamp") or payload.get("timestamp")
    )


def _user_text(agent: str, obj: dict[str, Any]) -> str | None:
    agent = (agent or "").lower()
    if agent == "codex":
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if not (
            obj.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            return None
        text = _content_to_text(payload.get("content"))
        if any(text.lstrip().startswith(noise) for noise in _CODEX_NOISE):
            return ""
    else:
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        is_user = (
            obj.get("role") == "user"
            or obj.get("type") == "user"
            or message.get("role") == "user"
        )
        if not is_user:
            return None
        content = message.get("content") if message else obj.get("content")
        text = _content_to_text(content)
        if (
            obj.get("isMeta")
            or obj.get("isCompactSummary")
            or obj.get("isSidechain")
            or text.lstrip().startswith(_CLAUDE_SYNTHETIC_PREFIXES)
        ):
            return ""
    text = _SYSTEM_BLOCK_RE.sub("", text)
    return _clean(text)


def _event_id(obj: dict[str, Any]) -> str:
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    return str(
        obj.get("uuid") or obj.get("id") or message.get("id") or payload.get("id") or ""
    )


def _numeric_usage(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for field_name in _TOKEN_FIELDS:
        try:
            value = int(raw.get(field_name) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            out[field_name] = value
    if "total_tokens" not in out:
        total = sum(
            out.get(name, 0)
            for name in (
                "input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "output_tokens",
            )
        )
        if total:
            out["total_tokens"] = total
    return out


@dataclass
class _TranscriptState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    device: int = 0
    inode: int = 0
    offset: int = 0
    requests: list[dict[str, Any]] = field(default_factory=list)
    token_events: list[dict[str, Any]] = field(default_factory=list)
    seen_request_ids: set[str] = field(default_factory=set)
    seen_usage_ids: set[str] = field(default_factory=set)
    codex_total_usage: dict[str, int] = field(default_factory=dict)
    initialized: bool = False
    persisted_offset: int = 0
    last_persist_at: float = 0.0

    def reset(self) -> None:
        self.offset = 0
        self.requests.clear()
        self.token_events.clear()
        self.seen_request_ids.clear()
        self.seen_usage_ids.clear()
        self.codex_total_usage.clear()
        self.initialized = False
        self.persisted_offset = 0
        self.last_persist_at = 0.0


class TranscriptMetricsCache:
    """Parse only newly appended JSONL records and retain numeric events.

    When ``cache_dir`` is configured, the content-free event index is also
    persisted. Dashboard restarts can then resume from the last byte offset
    instead of reparsing multi-gigabyte native transcripts.
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._states: dict[tuple[str, str], _TranscriptState] = {}
        self._cache_dir = Path(cache_dir).expanduser() if cache_dir else None

    def _cache_path(self, path: Path, agent: str) -> Path | None:
        if self._cache_dir is None:
            return None
        key = hashlib.sha256(
            f"{path}\0{(agent or '').lower()}".encode("utf-8")
        ).hexdigest()
        return self._cache_dir / f"{key}.json"

    def _restore(self, path: Path, agent: str, state: _TranscriptState) -> None:
        cache_path = self._cache_path(path, agent)
        if cache_path is None:
            return
        try:
            data = json.loads(cache_path.read_text())
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        try:
            version = int(data.get("version") or 0)
        except (TypeError, ValueError):
            return
        if version != 1:
            return
        if data.get("source") != str(path) or data.get("agent") != agent:
            return
        try:
            state.device = int(data.get("device") or 0)
            state.inode = int(data.get("inode") or 0)
            state.offset = max(0, int(data.get("offset") or 0))
        except (TypeError, ValueError):
            return
        requests = data.get("requests")
        token_events = data.get("token_events")
        state.requests = (
            [value for value in requests if isinstance(value, dict)]
            if isinstance(requests, list) else []
        )
        state.token_events = (
            [value for value in token_events if isinstance(value, dict)]
            if isinstance(token_events, list) else []
        )
        request_ids = data.get("seen_request_ids")
        usage_ids = data.get("seen_usage_ids")
        state.seen_request_ids = {
            str(value) for value in request_ids if value
        } if isinstance(request_ids, list) else set()
        state.seen_usage_ids = {
            str(value) for value in usage_ids if value
        } if isinstance(usage_ids, list) else set()
        usage = data.get("codex_total_usage")
        state.codex_total_usage = {
            str(key): int(value)
            for key, value in usage.items()
            if isinstance(value, (int, float))
        } if isinstance(usage, dict) else {}
        state.initialized = bool(data.get("initialized"))
        state.persisted_offset = state.offset
        state.last_persist_at = time.monotonic()

    def _persist(self, path: Path, agent: str, state: _TranscriptState) -> None:
        cache_path = self._cache_path(path, agent)
        if cache_path is None or not state.initialized:
            return
        now = time.monotonic()
        if state.offset == state.persisted_offset:
            return
        # Rewriting the compact numeric index on every five-second poll would
        # create needless I/O. A crash can at worst require reparsing the last
        # 30 seconds of appended transcript data.
        if state.persisted_offset and now - state.last_persist_at < 30.0:
            return
        data = {
            "version": 1,
            "source": str(path),
            "agent": agent,
            "device": state.device,
            "inode": state.inode,
            "offset": state.offset,
            "requests": state.requests,
            "token_events": state.token_events,
            "seen_request_ids": sorted(state.seen_request_ids),
            "seen_usage_ids": sorted(state.seen_usage_ids),
            "codex_total_usage": state.codex_total_usage,
            "initialized": True,
        }
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = cache_path.with_name(
                f".{cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temp_path.write_text(json.dumps(data, separators=(",", ":")))
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, cache_path)
        except OSError:
            return
        state.persisted_offset = state.offset
        state.last_persist_at = now

    def _state(self, path: Path, agent: str) -> _TranscriptState:
        path = Path(path)
        agent = (agent or "").lower()
        key = (str(path), agent)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = _TranscriptState()
                self._restore(path, agent, state)
                self._states[key] = state
            return state

    def _consume(self, state: _TranscriptState, agent: str, obj: dict[str, Any]) -> None:
        text = _user_text(agent, obj)
        if text is not None:
            request_id = _event_id(obj)
            if text and (not request_id or request_id not in state.seen_request_ids):
                state.requests.append({
                    "timestamp": _event_timestamp(obj),
                    "characters": len(text),
                    "tokens": _estimated_request_tokens(text),
                })
                if request_id:
                    state.seen_request_ids.add(request_id)

        agent = (agent or "").lower()
        if agent == "codex":
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            if obj.get("type") != "event_msg" or payload.get("type") != "token_count":
                return
            current = _numeric_usage(info.get("total_token_usage"))
            if not current:
                return
            delta = {
                name: max(0, value - state.codex_total_usage.get(name, 0))
                for name, value in current.items()
            }
            state.codex_total_usage = current
            if delta.get("total_tokens", 0) > 0:
                state.token_events.append({
                    "timestamp": _event_timestamp(obj),
                    **delta,
                })
            return

        if obj.get("type") != "assistant":
            return
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        usage = _numeric_usage(message.get("usage"))
        if not usage:
            return
        usage_id = str(message.get("id") or obj.get("uuid") or "")
        if usage_id and usage_id in state.seen_usage_ids:
            return
        if usage_id:
            state.seen_usage_ids.add(usage_id)
        state.token_events.append({
            "timestamp": _event_timestamp(obj),
            **usage,
        })

    def _update(self, path: Path, agent: str, state: _TranscriptState) -> None:
        stat = path.stat()
        if (
            state.device != stat.st_dev
            or state.inode != stat.st_ino
            or stat.st_size < state.offset
        ):
            state.reset()
            state.device = stat.st_dev
            state.inode = stat.st_ino
        if stat.st_size == state.offset:
            state.initialized = True
            return
        with path.open("rb") as handle:
            handle.seek(state.offset)
            while True:
                position = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    # The writer is still appending this JSON record. Parse it
                    # on the next request instead of counting a partial line.
                    handle.seek(position)
                    break
                state.offset = handle.tell()
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(obj, dict):
                    self._consume(state, agent, obj)
        state.initialized = True

    def is_ready(self, path: Path, agent: str) -> bool:
        state = self._state(Path(path), agent)
        # A background worker can hold this lock for minutes while it indexes
        # a very large transcript. Mission Control must return its activity
        # timeline immediately instead of waiting behind that cold parse.
        if not state.lock.acquire(blocking=False):
            return False
        try:
            return state.initialized
        finally:
            state.lock.release()

    @staticmethod
    def _sum_tokens(events: list[dict[str, Any]]) -> dict[str, int]:
        out = {name: 0 for name in _TOKEN_FIELDS}
        for event in events:
            for name in _TOKEN_FIELDS:
                out[name] += int(event.get(name) or 0)
        return out

    def snapshot(
        self, path: Path, agent: str, *, window_start: float, window_end: float,
    ) -> dict[str, Any]:
        path = Path(path)
        state = self._state(path, agent)
        with state.lock:
            try:
                self._update(path, agent, state)
            except OSError:
                return {"available": False}
            self._persist(path, (agent or "").lower(), state)
            requests = [
                event for event in state.requests
                if window_start <= float(event.get("timestamp") or 0.0) <= window_end
            ]
            token_events = [
                event for event in state.token_events
                if window_start <= float(event.get("timestamp") or 0.0) <= window_end
            ]
            window_tokens = self._sum_tokens(token_events)
            conversation_tokens = self._sum_tokens(state.token_events)
            daily: dict[str, dict[str, Any]] = {}
            for event in requests:
                timestamp = float(event.get("timestamp") or 0.0)
                if not timestamp:
                    continue
                day = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
                bucket = daily.setdefault(day, {
                    "date": day,
                    "requests": 0,
                    "request_tokens": 0,
                    "generated_tokens": 0,
                    "token_usage_available": False,
                })
                bucket["requests"] += 1
                bucket["request_tokens"] += int(event.get("tokens") or 0)
            for event in token_events:
                timestamp = float(event.get("timestamp") or 0.0)
                if not timestamp:
                    continue
                day = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
                bucket = daily.setdefault(day, {
                    "date": day,
                    "requests": 0,
                    "request_tokens": 0,
                    "generated_tokens": 0,
                    "token_usage_available": False,
                })
                bucket["generated_tokens"] += int(event.get("output_tokens") or 0)
                bucket["token_usage_available"] = True
            return {
                "available": True,
                "window": {
                    "requests": len(requests),
                    "characters": sum(int(event.get("characters") or 0) for event in requests),
                    "request_tokens": sum(
                        int(event.get("tokens") or 0) for event in requests
                    ),
                    "request_tokens_estimated": True,
                    "tokens": window_tokens.get("total_tokens", 0),
                    "generated_tokens": window_tokens.get("output_tokens", 0),
                    "token_usage_available": bool(state.token_events),
                    "token_usage": window_tokens,
                    "request_events": [
                        {
                            "timestamp": event.get("timestamp", 0.0),
                            "characters": event.get("characters", 0),
                            "tokens": event.get("tokens", 0),
                        }
                        for event in requests
                    ],
                    "daily": [daily[day] for day in sorted(daily)],
                },
                "conversation": {
                    "requests": len(state.requests),
                    "characters": sum(
                        int(event.get("characters") or 0) for event in state.requests
                    ),
                    "request_tokens": sum(
                        int(event.get("tokens") or 0) for event in state.requests
                    ),
                    "request_tokens_estimated": True,
                    "tokens": conversation_tokens.get("total_tokens", 0),
                    "generated_tokens": conversation_tokens.get("output_tokens", 0),
                    "token_usage_available": bool(state.token_events),
                    "token_usage": conversation_tokens,
                },
            }
