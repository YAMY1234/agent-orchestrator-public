import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import uvicorn
from uvicorn.config import WS_PROTOCOLS
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from agent_orchestrator import dashboard
from agent_orchestrator.remote_nodes import parse_qualified_run_id


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _fake_remote_node() -> FastAPI:
    app = FastAPI()
    app.state.last_create = {}
    app.state.last_restore = {}

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "instance_id": "remote-instance",
            "backend_id": "current-backend",
        }

    @app.get("/api/config")
    def config():
        return {"projects_root": "/home/example/Projects"}

    @app.get("/api/sessions")
    def sessions():
        return {
            "instance_id": "remote-instance",
            "backend_id": "current-backend",
            "snapshot": {"ready": True},
            "sessions": [{
                "run_id": "remote-run 1",
                "display_name": "Remote smoke task",
                "tmux_session": "orch-remote-smoke",
                "resume_id": "live-resume",
                "kind": "run",
                "alive": True,
            }],
        }

    @app.get("/api/active-snapshot")
    def active_snapshot():
        return {
            "ok": True,
            "saved_at": "2026-09-10T12:00:00",
            "backend_id": "previous-backend",
            "sessions": [
                {
                    "source_run_id": "remote-run 1",
                    "display_name": "Remote smoke task",
                    "resume_id": "live-resume",
                },
                {
                    "source_run_id": "recoverable-run",
                    "display_name": "Recover me",
                    "resume_id": "recoverable-resume",
                },
            ],
        }

    @app.post("/api/active-snapshot/restore")
    async def restore_active_snapshot(request: Request):
        app.state.last_restore = await request.json()
        return {
            "ok": True,
            "restored_count": 1,
            "skipped_count": 1,
            "restored": [{"run_id": "restored-run"}],
        }

    @app.get("/api/native-activity")
    def native_activity():
        return {"sessions": [], "updated_at": time.time()}

    @app.get("/api/sessions/{run_id}/tty")
    def tty(run_id: str):
        assert run_id == "remote-run 1"
        return {"ok": True, "url": "/tty/orch-remote-smoke/"}

    @app.post("/api/sessions/{run_id}/send")
    async def send(run_id: str, request: Request):
        body = await request.json()
        return {"ok": True, "run_id": run_id, "received": body}

    @app.post("/api/sessions/{run_id}/stop")
    def stop(run_id: str):
        return {
            "ok": True,
            "run_id": run_id,
            "reason": "agent already exited",
            "resume_persisted": True,
        }

    @app.post("/api/sessions/{run_id}/paste-image")
    async def paste_image(run_id: str, request: Request):
        body = await request.body()
        return {
            "ok": True,
            "run_id": run_id,
            "content_type": request.headers.get("content-type"),
            "bytes": len(body),
            "path": "/remote/pasted-images/clipboard.png",
        }

    @app.post("/api/create")
    async def create(request: Request):
        body = await request.json()
        app.state.last_create = body
        return {"ok": True, "run_id": "created-remote"}

    @app.get("/tty/{session}/", response_class=HTMLResponse)
    def tty_index(session: str):
        return f"<html><body>remote tty: {session}</body></html>"

    @app.websocket("/tty/{session}/ws")
    async def tty_ws(ws: WebSocket, session: str):
        await ws.accept(subprotocol="tty")
        message = await ws.receive_text()
        await ws.send_text(f"{session}:{message}")
        await ws.close()

    return app


class RemoteNodeProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        # Uvicorn's auto selector still imports the deprecated
        # ``websockets.legacy`` implementation with newer websockets releases.
        # The suite treats warnings as errors, so prefer the modern SansIO
        # protocol when the installed Uvicorn provides it. Older supported
        # Uvicorn versions continue to use their normal auto selection.
        ws_protocol = (
            "websockets-sansio"
            if "websockets-sansio" in WS_PROTOCOLS else "auto"
        )
        cls.server = uvicorn.Server(uvicorn.Config(
            _fake_remote_node(),
            host="127.0.0.1",
            port=cls.port,
            log_level="error",
            access_log=False,
            ws=ws_protocol,
        ))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.time() + 5
        while not cls.server.started and time.time() < deadline:
            time.sleep(0.02)
        if not cls.server.started:
            raise RuntimeError("fake remote node did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)

    def test_http_and_websocket_terminal_proxy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outputs = root / "outputs"
            outputs.mkdir()
            config = root / "dashboard.local.json"
            config.write_text(json.dumps({
                "projects_root": str(root),
                "remote_nodes": [{
                    "id": "dev",
                    "label": "Dev box",
                    "url": f"http://127.0.0.1:{self.port}",
                    "poll_interval_seconds": 1,
                }],
            }))
            with patch.dict(os.environ, {
                "ORCH_DASHBOARD_CONFIG": str(config),
                "ORCH_ACTIVE_SNAPSHOT_AUTOSAVE": "0",
            }):
                app = dashboard.create_app(outputs, ttyd_enabled=False)
                with TestClient(app) as client:
                    remote = None
                    deadline = time.time() + 4
                    while time.time() < deadline:
                        rows = client.get("/api/sessions").json()["sessions"]
                        remote = next(
                            (row for row in rows if row.get("node_id") == "dev"),
                            None,
                        )
                        if remote:
                            break
                        time.sleep(0.05)
                    self.assertIsNotNone(remote)
                    run_id = remote["run_id"]
                    self.assertEqual(
                        parse_qualified_run_id(run_id),
                        ("dev", "remote-run 1"),
                    )

                    tty = client.get(f"/api/sessions/{run_id}/tty")
                    self.assertEqual(tty.status_code, 200)
                    tty_url = tty.json()["url"]
                    self.assertEqual(
                        tty_url,
                        "/remote-nodes/dev/tty/orch-remote-smoke/",
                    )
                    index = client.get(tty_url)
                    self.assertEqual(index.status_code, 200)
                    self.assertIn("remote tty: orch-remote-smoke", index.text)
                    self.assertIn("orch-ttyd-interactions-v1", index.text)

                    sent = client.post(
                        f"/api/sessions/{run_id}/send",
                        json={"text": "hello", "enter": True},
                    )
                    self.assertEqual(sent.status_code, 200)
                    self.assertEqual(sent.json()["received"]["text"], "hello")
                    self.assertEqual(
                        parse_qualified_run_id(sent.json()["run_id"]),
                        ("dev", "remote-run 1"),
                    )

                    stopped = client.post(
                        f"/api/sessions/{run_id}/stop",
                    )
                    self.assertEqual(stopped.status_code, 200)
                    self.assertTrue(stopped.json()["ok"])
                    self.assertTrue(stopped.json()["resume_persisted"])
                    self.assertEqual(
                        parse_qualified_run_id(stopped.json()["run_id"]),
                        ("dev", "remote-run 1"),
                    )

                    image = b"\x89PNG\r\n\x1a\nremote-image"
                    pasted = client.post(
                        f"/api/sessions/{run_id}/paste-image",
                        content=image,
                        headers={"Content-Type": "image/png"},
                    )
                    self.assertEqual(pasted.status_code, 200)
                    self.assertEqual(pasted.json()["bytes"], len(image))
                    self.assertEqual(pasted.json()["content_type"], "image/png")
                    self.assertEqual(
                        parse_qualified_run_id(pasted.json()["run_id"]),
                        ("dev", "remote-run 1"),
                    )

                    created = client.post("/api/create", json={
                        "node_id": "dev",
                        "agent": "codex",
                        "cwd": "/home/example/Projects/demo",
                        "mode": "iterm",
                    })
                    self.assertEqual(created.status_code, 200)
                    self.assertEqual(
                        parse_qualified_run_id(created.json()["run_id"]),
                        ("dev", "created-remote"),
                    )
                    self.assertEqual(
                        self.server.config.app.state.last_create["mode"],
                        "background",
                    )

                    recovery = client.get("/api/nodes/dev/recovery")
                    self.assertEqual(recovery.status_code, 200)
                    self.assertTrue(recovery.json()["backend_changed"])
                    self.assertEqual(recovery.json()["recoverable_count"], 1)
                    self.assertEqual(
                        recovery.json()["recoverable_sessions"][0]["display_name"],
                        "Recover me",
                    )

                    restored = client.post(
                        "/api/nodes/dev/active-snapshot/restore",
                        json={"mode": "background", "skip_existing": True},
                    )
                    self.assertEqual(restored.status_code, 200)
                    self.assertEqual(restored.json()["restored_count"], 1)
                    self.assertEqual(
                        self.server.config.app.state.last_restore,
                        {"mode": "background", "skip_existing": True},
                    )

                    with client.websocket_connect(
                        "/remote-nodes/dev/tty/orch-remote-smoke/ws",
                        subprotocols=["tty"],
                    ) as websocket:
                        websocket.send_text("ping")
                        self.assertEqual(
                            websocket.receive_text(),
                            "orch-remote-smoke:ping",
                        )


if __name__ == "__main__":
    unittest.main()
