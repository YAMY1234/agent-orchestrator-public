"""Federate sessions from remote Agent Orchestrator dashboard nodes.

The local dashboard remains the browser-facing control plane.  Each remote
node runs the ordinary dashboard bound to its loopback interface and is
reached through one optional SSH local-forward.  Session discovery is cached
in a background thread so an unavailable node can never delay local API calls.
"""

from __future__ import annotations

import base64
import copy
import errno
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

import httpx


_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_REMOTE_RUN_PREFIX = "remote~"
_DEVICE_PIN_RE = re.compile(
    r"(?:PIN|code)\s+([A-Z0-9-]{4,})\s+at\s+(https?://\S+)",
    re.IGNORECASE,
)
_DEVICE_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def _command_from_config(raw: Any, field_name: str) -> tuple[str, ...]:
    if raw in (None, ""):
        return ()
    if (not isinstance(raw, list) or not raw
            or any(not isinstance(item, str) or not item for item in raw)):
        raise ValueError(f"{field_name} must be a non-empty JSON string array")
    if len(raw) > 64 or sum(len(item) for item in raw) > 16_384:
        raise ValueError(f"{field_name} is too large")
    return tuple(raw)


@dataclass(frozen=True)
class RemoteNodeReconnectSettings:
    """Server-side-only commands for restoring one remote dashboard node."""

    enabled: bool = False
    transport_probe_command: tuple[str, ...] = ()
    tunnel_command: tuple[str, ...] = ()
    tunnel_command_uses_pty: bool = True
    credential_probe_command: tuple[str, ...] = ()
    authenticate_command: tuple[str, ...] = ()
    post_authenticate_command: tuple[str, ...] = ()
    start_command: tuple[str, ...] = ()
    timeout_seconds: float = 600.0
    health_timeout_seconds: float = 45.0


def qualify_run_id(node_id: str, run_id: str) -> str:
    """Return a URL-safe, stable dashboard run id for a remote session."""
    encoded = base64.urlsafe_b64encode(run_id.encode("utf-8")).decode("ascii")
    return f"{_REMOTE_RUN_PREFIX}{node_id}~{encoded.rstrip('=')}"


def parse_qualified_run_id(value: str) -> Optional[tuple[str, str]]:
    """Decode a remote run id, returning ``None`` for ordinary local ids."""
    if not value.startswith(_REMOTE_RUN_PREFIX):
        return None
    payload = value[len(_REMOTE_RUN_PREFIX):]
    try:
        node_id, encoded = payload.split("~", 1)
    except ValueError:
        return None
    if not _NODE_ID_RE.fullmatch(node_id) or not encoded:
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        run_id = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return (node_id, run_id) if run_id else None


@dataclass(frozen=True)
class RemoteNodeSettings:
    id: str
    label: str
    url: str
    projects_root: str = ""
    token: str = ""
    ssh_host: str = ""
    local_port: int = 0
    remote_port: int = 0
    auto_tunnel: bool = False
    poll_interval_seconds: float = 3.0
    connect_timeout_seconds: float = 2.0
    request_timeout_seconds: float = 8.0
    reconnect: RemoteNodeReconnectSettings = RemoteNodeReconnectSettings()

    @property
    def authorization_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def upstream_url(self, path: str) -> str:
        return self.url.rstrip("/") + "/" + path.lstrip("/")

    def websocket_url(self, path: str) -> str:
        base = self.url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return base + "/" + path.lstrip("/")


def _resolve_token(raw: dict[str, Any]) -> str:
    env_name = str(raw.get("token_env") or "").strip()
    if env_name:
        return str(os.environ.get(env_name, "")).strip()
    return str(raw.get("token") or "").strip()


def settings_from_dict(data: dict[str, Any]) -> tuple[RemoteNodeSettings, ...]:
    raw_nodes = data.get("remote_nodes")
    if not isinstance(raw_nodes, list):
        return ()
    nodes: list[RemoteNodeSettings] = []
    seen: set[str] = set()
    for raw in raw_nodes:
        if not isinstance(raw, dict) or not bool(raw.get("enabled", True)):
            continue
        node_id = str(raw.get("id") or "").strip()
        if not _NODE_ID_RE.fullmatch(node_id):
            raise ValueError(
                "remote_nodes[].id must start with an alphanumeric character "
                "and contain only letters, digits, '_' or '-'"
            )
        if node_id in seen:
            raise ValueError(f"duplicate remote node id: {node_id}")
        seen.add(node_id)
        ssh_host = str(raw.get("ssh_host") or "").strip()
        local_port = int(raw.get("local_port") or 0)
        remote_port = int(raw.get("remote_port") or 0)
        url = str(raw.get("url") or "").strip()
        if not url and local_port:
            url = f"http://127.0.0.1:{local_port}"
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"remote node {node_id!r} requires an http(s) url")
        auto_tunnel = bool(raw.get("auto_tunnel", False))
        if auto_tunnel and (not ssh_host or not local_port or not remote_port):
            raise ValueError(
                f"remote node {node_id!r} auto_tunnel requires ssh_host, "
                "local_port, and remote_port"
            )
        raw_reconnect = raw.get("reconnect")
        if raw_reconnect is None:
            raw_reconnect = {}
        if not isinstance(raw_reconnect, dict):
            raise ValueError(f"remote node {node_id!r} reconnect must be an object")
        reconnect_enabled = bool(raw_reconnect.get("enabled", False))
        reconnect = RemoteNodeReconnectSettings(
            enabled=reconnect_enabled,
            transport_probe_command=_command_from_config(
                raw_reconnect.get("transport_probe_command"),
                "remote_nodes[].reconnect.transport_probe_command",
            ),
            tunnel_command=_command_from_config(
                raw_reconnect.get("tunnel_command"),
                "remote_nodes[].reconnect.tunnel_command",
            ),
            tunnel_command_uses_pty=bool(
                raw_reconnect.get("tunnel_command_uses_pty", True)
            ),
            credential_probe_command=_command_from_config(
                raw_reconnect.get("credential_probe_command"),
                "remote_nodes[].reconnect.credential_probe_command",
            ),
            authenticate_command=_command_from_config(
                raw_reconnect.get("authenticate_command"),
                "remote_nodes[].reconnect.authenticate_command",
            ),
            post_authenticate_command=_command_from_config(
                raw_reconnect.get("post_authenticate_command"),
                "remote_nodes[].reconnect.post_authenticate_command",
            ),
            start_command=_command_from_config(
                raw_reconnect.get("start_command"),
                "remote_nodes[].reconnect.start_command",
            ),
            timeout_seconds=max(
                30.0, float(raw_reconnect.get("timeout_seconds", 600.0))
            ),
            health_timeout_seconds=max(
                5.0,
                float(raw_reconnect.get("health_timeout_seconds", 45.0)),
            ),
        )
        if reconnect.enabled and not any((
            reconnect.tunnel_command,
            reconnect.authenticate_command,
            reconnect.start_command,
        )):
            raise ValueError(
                f"remote node {node_id!r} reconnect requires at least one "
                "tunnel, authenticate, or start command"
            )
        nodes.append(RemoteNodeSettings(
            id=node_id,
            label=str(raw.get("label") or node_id).strip() or node_id,
            url=url.rstrip("/"),
            projects_root=str(raw.get("projects_root") or "").strip(),
            token=_resolve_token(raw),
            ssh_host=ssh_host,
            local_port=local_port,
            remote_port=remote_port,
            auto_tunnel=auto_tunnel,
            poll_interval_seconds=max(
                1.0, float(raw.get("poll_interval_seconds", 3.0))
            ),
            connect_timeout_seconds=max(
                0.5, float(raw.get("connect_timeout_seconds", 2.0))
            ),
            request_timeout_seconds=max(
                1.0, float(raw.get("request_timeout_seconds", 8.0))
            ),
            reconnect=reconnect,
        ))
    return tuple(nodes)


def load_settings(config_path: Path) -> tuple[RemoteNodeSettings, ...]:
    try:
        data = json.loads(config_path.expanduser().read_text())
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(data, dict):
        return ()
    return settings_from_dict(data)


class _SshTunnel:
    """Own one resilient SSH local-forward for a remote node."""

    def __init__(self, settings: RemoteNodeSettings):
        self.settings = settings
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._last_error = ""

    def start(self) -> None:
        if not self.settings.auto_tunnel:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"orch-remote-tunnel-{self.settings.id}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)

    def status(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            running = bool(proc is not None and proc.poll() is None)
            error = self._last_error
        return {
            "enabled": self.settings.auto_tunnel,
            "running": running,
            "local_port": self.settings.local_port,
            "remote_port": self.settings.remote_port,
            "last_error": error,
        }

    def _run(self) -> None:
        delay = 2.0
        while not self._stop.is_set():
            command = [
                "ssh", "-N",
                "-L", (
                    f"127.0.0.1:{self.settings.local_port}:"
                    f"127.0.0.1:{self.settings.remote_port}"
                ),
                "-o", "BatchMode=yes",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=4",
                self.settings.ssh_host,
            ]
            try:
                proc = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                with self._lock:
                    self._proc = proc
                    self._last_error = ""
                while proc.poll() is None and not self._stop.wait(1.0):
                    pass
                if self._stop.is_set() and proc.poll() is None:
                    proc.terminate()
                code = proc.wait(timeout=2.0)
                if not self._stop.is_set():
                    with self._lock:
                        self._last_error = f"ssh tunnel exited with status {code}"
            except (OSError, subprocess.SubprocessError) as exc:
                with self._lock:
                    self._last_error = str(exc)
            finally:
                with self._lock:
                    self._proc = None
            if self._stop.wait(delay):
                break
            delay = min(60.0, delay * 2.0)


class RemoteNodeRegistry:
    """Background cache and connection ownership for configured nodes."""

    def __init__(self, settings: Iterable[RemoteNodeSettings]):
        self.settings = tuple(settings)
        self._by_id = {node.id: node for node in self.settings}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tunnels = {node.id: _SshTunnel(node) for node in self.settings}
        self._states: dict[str, dict[str, Any]] = {
            node.id: {
                "online": False,
                "session_inventory_ready": False,
                "sessions": [],
                "native_activity": [],
                "updated_at": 0.0,
                "last_seen_at": 0.0,
                "error": "not connected",
                "remote_instance_id": "",
                "projects_root": node.projects_root,
            }
            for node in self.settings
        }

    def get(self, node_id: str) -> Optional[RemoteNodeSettings]:
        return self._by_id.get(node_id)

    def start(self) -> None:
        if not self.settings or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._wake.set()
        for tunnel in self._tunnels.values():
            tunnel.start()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="orch-remote-nodes",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        for tunnel in self._tunnels.values():
            tunnel.stop()

    def request_refresh(self) -> None:
        self._wake.set()

    def sessions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self._lock:
            states = copy.deepcopy(self._states)
        for node in self.settings:
            state = states[node.id]
            for raw in state.get("sessions", []):
                row = dict(raw)
                remote_run_id = str(row.get("run_id") or "")
                if not remote_run_id:
                    continue
                row["remote_run_id"] = remote_run_id
                row["run_id"] = qualify_run_id(node.id, remote_run_id)
                row["node_id"] = node.id
                row["node_label"] = node.label
                row["remote"] = True
                row["node_online"] = bool(state.get("online"))
                row["node_last_seen_at"] = float(state.get("last_seen_at") or 0.0)
                row["node_error"] = str(state.get("error") or "")
                rows.append(row)
        return rows

    def native_activity(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self._lock:
            states = copy.deepcopy(self._states)
        for node in self.settings:
            state = states[node.id]
            for raw in state.get("native_activity", []):
                row = dict(raw)
                run_id = str(row.get("run_id") or "")
                if not run_id:
                    continue
                row["remote_run_id"] = run_id
                row["run_id"] = qualify_run_id(node.id, run_id)
                row["node_id"] = node.id
                rows.append(row)
        return rows

    def public_status(self) -> list[dict[str, Any]]:
        with self._lock:
            states = copy.deepcopy(self._states)
        result = []
        for node in self.settings:
            state = states[node.id]
            result.append({
                "id": node.id,
                "label": node.label,
                "online": bool(state.get("online")),
                "session_inventory_ready": bool(
                    state.get("session_inventory_ready")
                ),
                "session_count": len(state.get("sessions") or []),
                "updated_at": float(state.get("updated_at") or 0.0),
                "last_seen_at": float(state.get("last_seen_at") or 0.0),
                "error": str(state.get("error") or ""),
                "projects_root": str(
                    state.get("projects_root") or node.projects_root
                ),
                "remote_instance_id": str(state.get("remote_instance_id") or ""),
                "reconnect_enabled": bool(node.reconnect.enabled),
                "tunnel": self._tunnels[node.id].status(),
            })
        return result

    def browser_config(self) -> list[dict[str, Any]]:
        return [
            {
                "id": row["id"],
                "label": row["label"],
                "online": row["online"],
                "projects_root": row["projects_root"],
                "reconnect_enabled": bool(
                    self._by_id[row["id"]].reconnect.enabled
                ),
            }
            for row in self.public_status()
        ]

    def _run(self) -> None:
        interval = min(
            (node.poll_interval_seconds for node in self.settings),
            default=3.0,
        )
        next_refresh_at = 0.0
        while not self._stop.is_set():
            wait_seconds = max(0.0, next_refresh_at - time.monotonic())
            self._wake.wait(wait_seconds)
            self._wake.clear()
            if self._stop.is_set():
                break
            # Browser session-list polling may ask for a refresh every second.
            # Treat those requests as an early wake-up, not permission to
            # bypass the configured node polling interval.
            if time.monotonic() < next_refresh_at:
                continue
            self._refresh_all()
            next_refresh_at = time.monotonic() + interval

    def _refresh_all(self) -> None:
        workers = min(4, len(self.settings))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {
                pool.submit(self._fetch_node, node): node
                for node in self.settings
            }
            for future in as_completed(futures):
                node = futures[future]
                try:
                    update = future.result()
                except Exception as exc:
                    now = time.time()
                    with self._lock:
                        state = self._states[node.id]
                        state["online"] = False
                        state["updated_at"] = now
                        state["error"] = f"{type(exc).__name__}: {exc}"
                    continue
                with self._lock:
                    self._states[node.id].update(update)

    @staticmethod
    def _client(node: RemoteNodeSettings) -> httpx.Client:
        timeout = httpx.Timeout(
            connect=node.connect_timeout_seconds,
            read=node.request_timeout_seconds,
            write=node.request_timeout_seconds,
            pool=node.connect_timeout_seconds,
        )
        return httpx.Client(
            timeout=timeout,
            headers=node.authorization_headers,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    def _fetch_node(self, node: RemoteNodeSettings) -> dict[str, Any]:
        with self._lock:
            cached_projects_root = str(
                self._states[node.id].get("projects_root") or ""
            )
        with self._client(node) as client:
            health_resp = client.get(node.upstream_url("/api/health"))
            health_resp.raise_for_status()
            health = health_resp.json()
            sessions_resp = client.get(node.upstream_url("/api/sessions"))
            sessions_resp.raise_for_status()
            sessions_payload = sessions_resp.json()
            native_resp = client.get(node.upstream_url("/api/native-activity"))
            native_resp.raise_for_status()
            native_payload = native_resp.json()
            projects_root = cached_projects_root or node.projects_root
            # The projects root is static for the lifetime of a node process.
            # Fetch it only when it was not supplied locally and has not yet
            # been learned, instead of on every activity poll.
            if not projects_root:
                try:
                    config_resp = client.get(node.upstream_url("/api/config"))
                    config_resp.raise_for_status()
                    projects_root = str(
                        config_resp.json().get("projects_root") or ""
                    )
                except (httpx.HTTPError, ValueError, AttributeError):
                    pass
        now = time.time()
        sessions = sessions_payload.get("sessions")
        native = native_payload.get("sessions")
        snapshot = sessions_payload.get("snapshot")
        session_inventory_ready = (
            not isinstance(snapshot, dict)
            or snapshot.get("ready") is not False
        )
        update: dict[str, Any] = {
            "online": True,
            "session_inventory_ready": session_inventory_ready,
            "updated_at": now,
            "last_seen_at": now,
            "error": "",
            "remote_instance_id": str(
                sessions_payload.get("instance_id")
                or health.get("instance_id") or ""
            ),
            "projects_root": projects_root,
            "native_activity": native if isinstance(native, list) else [],
        }
        # A just-started remote dashboard may still be warming its local
        # snapshot. Preserve the last complete list instead of flashing all
        # remote sessions out of the local sidebar.
        if session_inventory_ready:
            update["sessions"] = sessions if isinstance(sessions, list) else []
        return update


class RemoteNodeReconnectManager:
    """Run one config-defined, interactive remote recovery at a time.

    Commands never leave the server.  The browser receives only a small state
    snapshot plus a device-verification URL/PIN when an interactive command
    asks for them.
    """

    _TERMINAL_PHASES = {"succeeded", "failed", "cancelled"}

    def __init__(self, settings: Iterable[RemoteNodeSettings],
                 refresh_callback=None, health_check=None):
        self._nodes = {node.id: node for node in settings}
        self._refresh_callback = refresh_callback
        self._health_check_override = health_check
        self._lock = threading.Lock()
        self._active_node = ""
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._continue: dict[str, threading.Event] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._states = {
            node.id: self._initial_state(node)
            for node in self._nodes.values()
        }

    @staticmethod
    def _initial_state(node: RemoteNodeSettings) -> dict[str, Any]:
        return {
            "node_id": node.id,
            "label": node.label,
            "enabled": bool(node.reconnect.enabled),
            "phase": "idle",
            "step": "",
            "message": "Ready to reconnect.",
            "verification_url": "",
            "verification_code": "",
            "continue_required": False,
            "started_at": 0.0,
            "updated_at": 0.0,
            "completed_at": 0.0,
            "warnings": [],
        }

    def status(self, node_id: str) -> dict[str, Any]:
        self._require_node(node_id)
        with self._lock:
            return copy.deepcopy(self._states[node_id])

    def start(self, node_id: str) -> dict[str, Any]:
        node = self._require_node(node_id)
        if not node.reconnect.enabled:
            raise RuntimeError(f"reconnect is not configured for {node.label}")
        with self._lock:
            active_thread = self._threads.get(self._active_node)
            if (self._active_node and active_thread is not None
                    and active_thread.is_alive()):
                if self._active_node == node_id:
                    return copy.deepcopy(self._states[node_id])
                active_label = self._nodes[self._active_node].label
                raise RuntimeError(
                    f"finish or cancel the {active_label} reconnect first"
                )
            now = time.time()
            self._states[node_id] = {
                **self._initial_state(node),
                "phase": "running",
                "step": "health",
                "message": "Checking the remote dashboard…",
                "started_at": now,
                "updated_at": now,
            }
            cancel = threading.Event()
            continue_event = threading.Event()
            self._cancel[node_id] = cancel
            self._continue[node_id] = continue_event
            self._active_node = node_id
            thread = threading.Thread(
                target=self._run,
                args=(node, cancel, continue_event),
                daemon=True,
                name=f"orch-node-reconnect-{node.id}",
            )
            self._threads[node_id] = thread
            thread.start()
            return copy.deepcopy(self._states[node_id])

    def continue_after_verification(self, node_id: str) -> dict[str, Any]:
        self._require_node(node_id)
        with self._lock:
            state = self._states[node_id]
            if state["phase"] != "waiting_for_user":
                raise RuntimeError("this reconnect is not waiting for verification")
            event = self._continue.get(node_id)
            if event is None:
                raise RuntimeError("reconnect is no longer running")
            event.set()
            state["message"] = "Verification submitted; waiting for the command…"
            state["updated_at"] = time.time()
            return copy.deepcopy(state)

    def cancel(self, node_id: str) -> dict[str, Any]:
        self._require_node(node_id)
        with self._lock:
            event = self._cancel.get(node_id)
            process = self._processes.get(node_id)
            state = self._states[node_id]
            if state["phase"] in self._TERMINAL_PHASES or event is None:
                return copy.deepcopy(state)
            event.set()
        if process is not None and process.poll() is None:
            self._terminate(process)
        return self.status(node_id)

    def stop(self) -> None:
        with self._lock:
            node_ids = list(self._cancel)
        for node_id in node_ids:
            self.cancel(node_id)
        for thread in list(self._threads.values()):
            thread.join(timeout=2.0)

    def _require_node(self, node_id: str) -> RemoteNodeSettings:
        node = self._nodes.get(node_id)
        if node is None:
            raise KeyError(node_id)
        return node

    def _set(self, node_id: str, **updates: Any) -> None:
        with self._lock:
            state = self._states[node_id]
            state.update(updates)
            state["updated_at"] = time.time()

    def _warn(self, node_id: str, message: str) -> None:
        with self._lock:
            warnings = self._states[node_id].setdefault("warnings", [])
            warnings.append(message[:240])
            self._states[node_id]["updated_at"] = time.time()

    def _run(self, node: RemoteNodeSettings, cancel: threading.Event,
             continue_event: threading.Event) -> None:
        try:
            if self._health_check(node):
                self._finish(node, "succeeded", "Remote dashboard is already online.")
                return
            reconnect = node.reconnect
            transport_ok = True
            if reconnect.transport_probe_command:
                self._set(
                    node.id, step="transport",
                    message="Checking the SSH connection…",
                )
                code, _ = self._run_command(
                    node, reconnect.transport_probe_command, cancel,
                    timeout=30.0,
                )
                transport_ok = code == 0

            # Some clusters authenticate the SSH transport itself with a
            # device-code flow.  In that case the tunnel cannot possibly be
            # created before the interactive login has completed.  Reuse the
            # configured authentication command here: it opens the control
            # connection, exposes the URL/PIN through _run_interactive(), and
            # leaves the later tunnel command non-interactive.
            if not transport_ok and reconnect.authenticate_command:
                self._set(
                    node.id, step="authenticate",
                    message="SSH access needs verification…",
                )
                self._run_interactive(
                    node, reconnect.authenticate_command, cancel,
                    continue_event, timeout=reconnect.timeout_seconds,
                )
                if reconnect.transport_probe_command:
                    code, _ = self._run_command(
                        node, reconnect.transport_probe_command, cancel,
                        timeout=30.0,
                    )
                    transport_ok = code == 0

            forward_ok = not node.local_port or self._port_open(node.local_port)
            if not transport_ok or not forward_ok:
                if not reconnect.tunnel_command:
                    raise RuntimeError("SSH tunnel is unavailable and no tunnel command is configured")
                self._set(
                    node.id, step="tunnel",
                    message="Restoring the SSH tunnel…",
                )
                port_timeout = min(
                    60.0, max(10.0, reconnect.health_timeout_seconds)
                )
                if reconnect.tunnel_command_uses_pty:
                    code, output = self._run_interactive(
                        node, reconnect.tunnel_command, cancel, continue_event,
                        timeout=reconnect.timeout_seconds,
                    )
                else:
                    code, output = self._start_detached_tunnel(
                        node, reconnect.tunnel_command, cancel,
                        timeout=port_timeout,
                    )
                if code != 0:
                    raise RuntimeError(self._command_error("SSH tunnel", code, output))
                if reconnect.transport_probe_command:
                    code, output = self._run_command(
                        node, reconnect.transport_probe_command, cancel,
                        timeout=30.0,
                    )
                    if code != 0:
                        raise RuntimeError(self._command_error("SSH connection", code, output))
                if node.local_port and not self._wait_for_port(
                    node.local_port, cancel, timeout=port_timeout
                ):
                    raise RuntimeError("SSH connected, but the local tunnel port did not open")

            # The dashboard can recover independently while the SSH/tunnel
            # checks are running (for example when a managed tunnel reconnects
            # in the background).  Once health is back, do not continue into a
            # credential prompt that is no longer needed.
            if self._health_check(node):
                self._finish(node, "succeeded", f"{node.label} is online again.")
                return

            if reconnect.credential_probe_command:
                self._set(
                    node.id, step="credentials",
                    message="Checking remote credentials and home storage…",
                )
                code, output = self._run_command(
                    node, reconnect.credential_probe_command, cancel,
                    timeout=30.0,
                )
                if code != 0:
                    if not reconnect.authenticate_command:
                        raise RuntimeError(self._command_error("Remote credentials", code, output))
                    self._set(
                        node.id, step="authenticate",
                        message="Remote credentials need verification…",
                    )
                    auth_code, auth_output = self._run_interactive(
                        node, reconnect.authenticate_command, cancel,
                        continue_event, timeout=reconnect.timeout_seconds,
                    )
                    code, output = self._run_command(
                        node, reconnect.credential_probe_command, cancel,
                        timeout=30.0,
                    )
                    if code != 0:
                        detail = output or auth_output
                        raise RuntimeError(self._command_error(
                            "Remote credential verification", auth_code or code, detail
                        ))
                    if auth_code != 0:
                        self._warn(
                            node.id,
                            "The authentication command reported an error, but the "
                            "credential probe succeeded.",
                        )
                    if reconnect.post_authenticate_command:
                        self._set(
                            node.id, step="renew",
                            message="Starting credential renewal…",
                        )
                        renew_code, renew_output = self._run_command(
                            node, reconnect.post_authenticate_command, cancel,
                            timeout=30.0,
                        )
                        if renew_code != 0:
                            self._warn(
                                node.id,
                                self._command_error(
                                    "Automatic credential renewal", renew_code,
                                    renew_output,
                                ),
                            )

            if not self._health_check(node):
                if not reconnect.start_command:
                    raise RuntimeError(
                        "Remote dashboard is offline and no start command is configured"
                    )
                self._set(
                    node.id, step="service",
                    message="Starting the remote dashboard…",
                )
                code, output = self._run_command(
                    node, reconnect.start_command, cancel,
                    timeout=45.0,
                )
                if code != 0:
                    raise RuntimeError(self._command_error(
                        "Remote dashboard start", code, output
                    ))

            self._set(
                node.id, step="verify",
                message="Waiting for the remote dashboard health check…",
            )
            if not self._wait_for_health(
                node, cancel, reconnect.health_timeout_seconds
            ):
                raise RuntimeError("remote dashboard did not become healthy in time")
            self._finish(node, "succeeded", f"{node.label} is online again.")
        except _ReconnectCancelled:
            self._finish(node, "cancelled", "Reconnect cancelled.")
        except Exception as exc:
            self._finish(node, "failed", str(exc) or type(exc).__name__)

    def _finish(self, node: RemoteNodeSettings, phase: str, message: str) -> None:
        now = time.time()
        self._set(
            node.id,
            phase=phase,
            step="complete" if phase == "succeeded" else self.status(node.id)["step"],
            message=message[:600],
            verification_url="",
            verification_code="",
            continue_required=False,
            completed_at=now,
        )
        with self._lock:
            self._processes.pop(node.id, None)
            self._cancel.pop(node.id, None)
            self._continue.pop(node.id, None)
            if self._active_node == node.id:
                self._active_node = ""
        if self._refresh_callback is not None:
            try:
                self._refresh_callback()
            except Exception:
                pass

    def _run_command(self, node: RemoteNodeSettings, command: tuple[str, ...],
                     cancel: threading.Event, timeout: float) -> tuple[int, str]:
        if cancel.is_set():
            raise _ReconnectCancelled()
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return 127, str(exc)
        self._remember_process(node.id, process)
        deadline = time.monotonic() + timeout
        output = ""
        try:
            while True:
                if cancel.is_set():
                    self._terminate(process)
                    raise _ReconnectCancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate(process)
                    return 124, f"command timed out after {timeout:g} seconds"
                try:
                    output, _ = process.communicate(timeout=min(0.25, remaining))
                    if cancel.is_set():
                        raise _ReconnectCancelled()
                    return int(process.returncode or 0), output[-16_384:]
                except subprocess.TimeoutExpired:
                    continue
        finally:
            self._forget_process(node.id, process)

    def _run_interactive(self, node: RemoteNodeSettings,
                         command: tuple[str, ...], cancel: threading.Event,
                         continue_event: threading.Event,
                         timeout: float) -> tuple[int, str]:
        continue_event.clear()
        try:
            process = subprocess.Popen(
                [
                    sys.executable, "-m", "agent_orchestrator.pty_bridge",
                    *command,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            return 127, str(exc)
        self._remember_process(node.id, process)
        deadline = time.monotonic() + timeout
        output = ""
        challenge_seen = ""
        enter_sent = False
        output_fd = process.stdout.fileno() if process.stdout else -1
        try:
            while True:
                if cancel.is_set():
                    self._terminate(process)
                    raise _ReconnectCancelled()
                if time.monotonic() >= deadline:
                    self._terminate(process)
                    return 124, f"interactive command timed out after {timeout:g} seconds"
                readable, _, _ = select.select(
                    [output_fd] if output_fd >= 0 else [], [], [], 0.2
                )
                if readable:
                    try:
                        chunk = os.read(output_fd, 4096)
                    except OSError as exc:
                        if exc.errno not in {errno.EIO, errno.EBADF}:
                            raise
                        chunk = b""
                    if chunk:
                        output = (output + chunk.decode("utf-8", "replace"))[-32_768:]
                        challenge = self._device_challenge(output)
                        challenge_key = "|".join(str(item) for item in challenge)
                        if challenge[0] and challenge_key != challenge_seen:
                            challenge_seen = challenge_key
                            self._set(
                                node.id,
                                phase="waiting_for_user",
                                message=(
                                    "Open the verification page, complete sign-in, "
                                    "then continue here."
                                ),
                                verification_url=challenge[0],
                                verification_code=challenge[1],
                                continue_required=challenge[2],
                            )
                if (challenge_seen and not enter_sent and continue_event.is_set()):
                    continue_event.clear()
                    if self.status(node.id).get("continue_required"):
                        if process.stdin is not None:
                            try:
                                process.stdin.write(b"\n")
                                process.stdin.flush()
                            except (BrokenPipeError, OSError):
                                pass
                    enter_sent = True
                    self._set(
                        node.id,
                        phase="running",
                        message="Verification completed; reconnecting…",
                        verification_url="",
                        verification_code="",
                        continue_required=False,
                    )
                code = process.poll()
                if code is not None:
                    if cancel.is_set():
                        raise _ReconnectCancelled()
                    return int(code), output[-16_384:]
        finally:
            if process.poll() is None:
                self._terminate(process)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            self._forget_process(node.id, process)

    def _start_detached_tunnel(
        self,
        node: RemoteNodeSettings,
        command: tuple[str, ...],
        cancel: threading.Event,
        timeout: float,
    ) -> tuple[int, str]:
        """Start a self-daemonizing tunnel without a controlling terminal.

        Some SSH proxy commands can inherit a PTY after ``ssh -f`` returns,
        which keeps the PTY bridge alive and makes a healthy tunnel look
        stuck. With stdio detached, wait on the actual forwarded port while
        still observing an early launcher failure.
        """
        if cancel.is_set():
            raise _ReconnectCancelled()
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            return 127, str(exc)
        self._remember_process(node.id, process)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if cancel.wait(0.25):
                    self._terminate(process)
                    raise _ReconnectCancelled()
                if node.local_port and self._port_open(node.local_port):
                    if process.poll() is None:
                        threading.Thread(
                            target=process.wait,
                            daemon=True,
                            name=f"orch-tunnel-reaper-{node.id}",
                        ).start()
                    return 0, ""
                code = process.poll()
                if code not in (None, 0):
                    return int(code), ""
                if not node.local_port and code is not None:
                    return int(code), ""
            self._terminate(process)
            return 124, f"tunnel did not open after {timeout:g} seconds"
        finally:
            self._forget_process(node.id, process)

    @staticmethod
    def _device_challenge(output: str) -> tuple[str, str, bool]:
        plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
        pin_match = _DEVICE_PIN_RE.search(plain)
        if pin_match:
            url = pin_match.group(2).rstrip(".,);]")
            context = plain[pin_match.start():pin_match.end() + 80]
            return url, pin_match.group(1), "PRESS ENTER" in context.upper()
        lowered = plain.lower()
        if "browser" in lowered or "authenticate" in lowered or "device" in lowered:
            urls = _DEVICE_URL_RE.findall(plain)
            if urls:
                url = urls[-1].rstrip(".,);]")
                return url, "", "PRESS ENTER" in plain.upper()
        return "", "", False

    def _health_check(self, node: RemoteNodeSettings) -> bool:
        if self._health_check_override is not None:
            return bool(self._health_check_override(node))
        try:
            with RemoteNodeRegistry._client(node) as client:
                response = client.get(node.upstream_url("/api/health"))
                return response.status_code == 200 and bool(response.json().get("ok"))
        except (httpx.HTTPError, ValueError, AttributeError):
            return False

    def _wait_for_health(self, node: RemoteNodeSettings,
                         cancel: threading.Event, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel.wait(1.0):
                raise _ReconnectCancelled()
            if self._health_check(node):
                return True
        return False

    @staticmethod
    def _port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            return False

    def _wait_for_port(self, port: int, cancel: threading.Event,
                       timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._port_open(port):
                return True
            if cancel.wait(0.25):
                raise _ReconnectCancelled()
        return False

    def _remember_process(self, node_id: str, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes[node_id] = process

    def _forget_process(self, node_id: str, process: subprocess.Popen) -> None:
        with self._lock:
            if self._processes.get(node_id) is process:
                self._processes.pop(node_id, None)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass

    @staticmethod
    def _command_error(label: str, code: int, output: str) -> str:
        plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output or "")
        lines = [line.strip() for line in plain.splitlines() if line.strip()]
        detail = " · ".join(lines[-3:])[-500:]
        return f"{label} failed (exit {code})" + (f": {detail}" if detail else "")


class _ReconnectCancelled(Exception):
    pass


def remote_api_path(node_id: str, remote_run_id: str, suffix: str = "") -> str:
    run = quote(remote_run_id, safe="")
    suffix = "/" + suffix.lstrip("/") if suffix else ""
    return f"/api/sessions/{run}{suffix}"
