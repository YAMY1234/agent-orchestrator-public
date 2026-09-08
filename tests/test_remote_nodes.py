import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_orchestrator import dashboard

from agent_orchestrator.remote_nodes import (
    RemoteNodeReconnectManager,
    RemoteNodeRegistry,
    parse_qualified_run_id,
    qualify_run_id,
    remote_api_path,
    settings_from_dict,
)


class RemoteRunIdTest(unittest.TestCase):
    def test_round_trip_is_url_safe(self):
        source = "qwen/rubin 中文 session::42"
        qualified = qualify_run_id("remote-dev", source)
        self.assertNotIn("/", qualified)
        self.assertEqual(
            parse_qualified_run_id(qualified),
            ("remote-dev", source),
        )

    def test_local_and_malformed_ids_are_not_remote(self):
        self.assertIsNone(parse_qualified_run_id("ordinary-local-run"))
        self.assertIsNone(parse_qualified_run_id("remote~bad node~abc"))
        self.assertIsNone(parse_qualified_run_id("remote~node~!"))

    def test_remote_api_path_quotes_original_id(self):
        self.assertEqual(
            remote_api_path("node", "folder/run 1", "send"),
            "/api/sessions/folder%2Frun%201/send",
        )


class RemoteNodeSettingsTest(unittest.TestCase):
    def test_derives_loopback_url_and_keeps_secrets_server_side(self):
        data = {
            "remote_nodes": [{
                "id": "dev",
                "label": "Dev",
                "ssh_host": "dev-box",
                "local_port": 17861,
                "remote_port": 7861,
                "auto_tunnel": True,
                "token": "secret",
            }]
        }
        settings = settings_from_dict(data)
        self.assertEqual(len(settings), 1)
        self.assertEqual(settings[0].url, "http://127.0.0.1:17861")
        registry = RemoteNodeRegistry(settings)
        browser = registry.browser_config()
        self.assertEqual(browser[0]["id"], "dev")
        self.assertNotIn("token", browser[0])
        self.assertNotIn("url", browser[0])
        self.assertNotIn("ssh_host", browser[0])

    def test_rejects_unsafe_or_duplicate_ids(self):
        with self.assertRaises(ValueError):
            settings_from_dict({"remote_nodes": [{
                "id": "../bad", "url": "http://127.0.0.1:1",
            }]})
        with self.assertRaises(ValueError):
            settings_from_dict({"remote_nodes": [
                {"id": "same", "url": "http://127.0.0.1:1"},
                {"id": "same", "url": "http://127.0.0.1:2"},
            ]})

    def test_disabled_nodes_are_ignored(self):
        settings = settings_from_dict({"remote_nodes": [{
            "id": "dev", "url": "http://127.0.0.1:1", "enabled": False,
        }]})
        self.assertEqual(settings, ())

    def test_reconnect_commands_remain_server_side(self):
        settings = settings_from_dict({"remote_nodes": [{
            "id": "dev",
            "url": "http://127.0.0.1:1",
            "reconnect": {
                "enabled": True,
                "start_command": ["ssh", "dev", "start-dashboard"],
            },
        }]})
        registry = RemoteNodeRegistry(settings)
        browser = registry.browser_config()[0]
        self.assertTrue(browser["reconnect_enabled"])
        rendered = json.dumps(browser)
        self.assertNotIn("start-dashboard", rendered)
        self.assertNotIn("ssh", rendered)

    def test_non_pty_tunnel_mode_is_configurable(self):
        settings = settings_from_dict({"remote_nodes": [{
            "id": "dev",
            "url": "http://127.0.0.1:1",
            "reconnect": {
                "enabled": True,
                "tunnel_command": ["ssh", "-N", "dev"],
                "tunnel_command_uses_pty": False,
            },
        }]})
        self.assertFalse(settings[0].reconnect.tunnel_command_uses_pty)


class RemoteNodeReconnectManagerTest(unittest.TestCase):
    def test_recovery_during_transport_skips_unneeded_credential_probe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            healthy = root / "healthy"
            credential_ran = root / "credential-ran"
            transport = (
                "import pathlib; "
                f"pathlib.Path({str(healthy)!r}).write_text('ok')"
            )
            credential = (
                "import pathlib; "
                f"pathlib.Path({str(credential_ran)!r}).write_text('unexpected')"
            )
            settings = settings_from_dict({"remote_nodes": [{
                "id": "dev",
                "label": "Dev box",
                "url": "http://127.0.0.1:1",
                "reconnect": {
                    "enabled": True,
                    "transport_probe_command": [sys.executable, "-c", transport],
                    "credential_probe_command": [sys.executable, "-c", credential],
                    "start_command": [
                        sys.executable, "-c", "raise SystemExit(99)",
                    ],
                },
            }]})
            manager = RemoteNodeReconnectManager(
                settings, health_check=lambda _: healthy.exists(),
            )
            try:
                manager.start("dev")
                finished = self._wait_for_phase(manager, "dev", "succeeded")
                self.assertEqual(finished["message"], "Dev box is online again.")
                self.assertFalse(credential_ran.exists())
            finally:
                manager.stop()

    def test_non_pty_tunnel_waits_for_forwarded_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tunnel = root / "tunnel"
            healthy = root / "healthy"
            make_tunnel = (
                "import pathlib,time; "
                f"pathlib.Path({str(tunnel)!r}).write_text('ok'); "
                "time.sleep(0.5)"
            )
            start = (
                "import pathlib; "
                f"pathlib.Path({str(healthy)!r}).write_text('ok')"
            )
            settings = settings_from_dict({"remote_nodes": [{
                "id": "dev",
                "url": "http://127.0.0.1:17861",
                "local_port": 17861,
                "reconnect": {
                    "enabled": True,
                    "tunnel_command": [sys.executable, "-c", make_tunnel],
                    "tunnel_command_uses_pty": False,
                    "start_command": [sys.executable, "-c", start],
                    "health_timeout_seconds": 5,
                },
            }]})
            manager = RemoteNodeReconnectManager(
                settings, health_check=lambda _: healthy.exists(),
            )
            manager._port_open = lambda _: tunnel.exists()
            try:
                manager.start("dev")
                finished = self._wait_for_phase(manager, "dev", "succeeded")
                self.assertEqual(finished["step"], "complete")
                self.assertTrue(tunnel.exists())
            finally:
                manager.stop()

    def test_transport_authentication_precedes_tunnel_creation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            authenticated = root / "authenticated"
            tunnel = root / "tunnel"
            healthy = root / "healthy"
            order = root / "order"
            probe = (
                "import pathlib,sys; "
                f"sys.exit(0 if pathlib.Path({str(authenticated)!r}).exists() else 1)"
            )
            authenticate = (
                "import pathlib; "
                "print('Authenticate with PIN ABCD1234 at "
                "https://login.example/device and press ENTER.', flush=True); "
                "input(); "
                f"pathlib.Path({str(order)!r}).open('a').write('authenticate\\n'); "
                f"pathlib.Path({str(authenticated)!r}).write_text('ok')"
            )
            make_tunnel = (
                "import pathlib; "
                f"pathlib.Path({str(order)!r}).open('a').write('tunnel\\n'); "
                f"pathlib.Path({str(tunnel)!r}).write_text('ok')"
            )
            start = (
                "import pathlib; "
                f"pathlib.Path({str(healthy)!r}).write_text('ok')"
            )
            settings = settings_from_dict({"remote_nodes": [{
                "id": "dev",
                "url": "http://127.0.0.1:17861",
                "local_port": 17861,
                "reconnect": {
                    "enabled": True,
                    "transport_probe_command": [sys.executable, "-c", probe],
                    "authenticate_command": [sys.executable, "-c", authenticate],
                    "tunnel_command": [sys.executable, "-c", make_tunnel],
                    "start_command": [sys.executable, "-c", start],
                    "timeout_seconds": 30,
                    "health_timeout_seconds": 5,
                },
            }]})
            manager = RemoteNodeReconnectManager(
                settings, health_check=lambda _: healthy.exists(),
            )
            manager._port_open = lambda _: tunnel.exists()
            try:
                manager.start("dev")
                waiting = self._wait_for_phase(manager, "dev", "waiting_for_user")
                self.assertEqual(waiting["step"], "authenticate")
                manager.continue_after_verification("dev")
                self._wait_for_phase(manager, "dev", "succeeded")
                self.assertEqual(
                    order.read_text().splitlines(), ["authenticate", "tunnel"]
                )
            finally:
                manager.stop()

    def test_device_code_flow_waits_for_continue_then_recovers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "authenticated"
            probe = (
                "import pathlib,sys; "
                f"sys.exit(0 if pathlib.Path({str(marker)!r}).exists() else 1)"
            )
            authenticate = (
                "import os,pathlib; fd=os.open('/dev/tty',os.O_RDWR); "
                "os.write(fd,b'Authenticate with PIN ABCD1234 at "
                "https://login.example/device and press ENTER.\\n'); "
                "os.read(fd,1024); "
                f"pathlib.Path({str(marker)!r}).write_text('ok')"
            )
            settings = settings_from_dict({"remote_nodes": [{
                "id": "dev",
                "label": "Dev box",
                "url": "http://127.0.0.1:1",
                "reconnect": {
                    "enabled": True,
                    "credential_probe_command": [sys.executable, "-c", probe],
                    "authenticate_command": [
                        sys.executable, "-c", authenticate,
                    ],
                    "timeout_seconds": 30,
                    "health_timeout_seconds": 5,
                },
            }]})
            manager = RemoteNodeReconnectManager(
                settings,
                health_check=lambda _: marker.exists(),
            )
            try:
                manager.start("dev")
                waiting = self._wait_for_phase(manager, "dev", "waiting_for_user")
                self.assertEqual(waiting["verification_code"], "ABCD1234")
                self.assertEqual(
                    waiting["verification_url"], "https://login.example/device"
                )
                self.assertTrue(waiting["continue_required"])
                manager.continue_after_verification("dev")
                finished = self._wait_for_phase(manager, "dev", "succeeded")
                self.assertEqual(finished["step"], "complete")
                self.assertTrue(marker.exists())
            finally:
                manager.stop()

    def test_running_reconnect_can_be_cancelled(self):
        sleep_command = [sys.executable, "-c", "import time; time.sleep(30)"]
        settings = settings_from_dict({"remote_nodes": [{
            "id": "dev",
            "url": "http://127.0.0.1:1",
            "reconnect": {
                "enabled": True,
                "transport_probe_command": [
                    sys.executable, "-c", "raise SystemExit(1)",
                ],
                "tunnel_command": sleep_command,
                "timeout_seconds": 60,
            },
        }]})
        manager = RemoteNodeReconnectManager(
            settings, health_check=lambda _: False,
        )
        try:
            manager.start("dev")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if manager.status("dev")["step"] == "tunnel":
                    break
                time.sleep(0.02)
            manager.cancel("dev")
            cancelled = self._wait_for_phase(manager, "dev", "cancelled")
            self.assertEqual(cancelled["message"], "Reconnect cancelled.")
        finally:
            manager.stop()

    @staticmethod
    def _wait_for_phase(manager, node_id, phase, timeout=6.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = manager.status(node_id)
            if status["phase"] == phase:
                return status
            if status["phase"] in {"failed", "cancelled"}:
                raise AssertionError(status)
            time.sleep(0.05)
        raise AssertionError(
            f"timed out waiting for {phase}: {manager.status(node_id)}"
        )


class RemoteNodeRegistryTest(unittest.TestCase):
    def setUp(self):
        self.settings = settings_from_dict({"remote_nodes": [{
            "id": "dev",
            "label": "Dev box",
            "url": "http://127.0.0.1:17861",
            "projects_root": "/home/example/Projects",
        }]})
        self.registry = RemoteNodeRegistry(self.settings)

    def test_sessions_are_qualified_and_keep_remote_identity(self):
        with self.registry._lock:
            self.registry._states["dev"].update({
                "online": True,
                "last_seen_at": 123.0,
                "sessions": [{
                    "run_id": "run-1",
                    "display_name": "Remote task",
                    "alive": True,
                }],
            })
        rows = self.registry.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["remote_run_id"], "run-1")
        self.assertEqual(parse_qualified_run_id(rows[0]["run_id"]), ("dev", "run-1"))
        self.assertEqual(rows[0]["node_label"], "Dev box")
        self.assertTrue(rows[0]["node_online"])

    def test_offline_node_retains_last_snapshot(self):
        with self.registry._lock:
            self.registry._states["dev"].update({
                "online": False,
                "error": "connection refused",
                "sessions": [{"run_id": "run-1", "alive": True}],
            })
        row = self.registry.sessions()[0]
        self.assertFalse(row["node_online"])
        self.assertEqual(row["node_error"], "connection refused")

    def test_public_status_marks_inventory_unready_during_node_warmup(self):
        status = self.registry.public_status()[0]
        self.assertFalse(status["session_inventory_ready"])

        with self.registry._lock:
            self.registry._states["dev"].update({
                "online": True,
                "session_inventory_ready": True,
            })

        status = self.registry.public_status()[0]
        self.assertTrue(status["session_inventory_ready"])

    def test_refresh_requests_do_not_bypass_poll_interval(self):
        calls = []
        first_refresh = threading.Event()

        def record_refresh():
            calls.append(time.monotonic())
            first_refresh.set()

        self.registry._refresh_all = record_refresh
        self.registry.start()
        try:
            self.assertTrue(first_refresh.wait(0.5))
            for _ in range(5):
                self.registry.request_refresh()
            time.sleep(0.2)
            self.assertEqual(len(calls), 1)
        finally:
            self.registry.stop()


class DashboardFederationTest(unittest.TestCase):
    def test_remote_sessions_are_merged_without_exposing_connection_secrets(self):
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
                    "url": "http://127.0.0.1:17861",
                    "projects_root": "/home/example/Projects",
                    "token": "node-secret",
                    "reconnect": {
                        "enabled": True,
                        "start_command": [
                            sys.executable, "-c", "raise SystemExit(0)",
                        ],
                    },
                }],
            }))
            with patch.dict(os.environ, {
                "ORCH_DASHBOARD_CONFIG": str(config),
                "ORCH_ACTIVE_SNAPSHOT_AUTOSAVE": "0",
            }), patch.object(RemoteNodeRegistry, "start"), \
                    patch.object(RemoteNodeRegistry, "stop"):
                app = dashboard.create_app(outputs, ttyd_enabled=False)
                registry = app.state.remote_nodes
                with registry._lock:
                    registry._states["dev"].update({
                        "online": True,
                        "sessions": [{
                            "run_id": "remote-run",
                            "display_name": "Remote task",
                            "alive": True,
                        }],
                    })
                with TestClient(app) as client:
                    config_payload = client.get("/api/config").json()
                    sessions_payload = client.get("/api/sessions").json()
                    reconnect_payload = client.get(
                        "/api/nodes/dev/reconnect"
                    ).json()

            self.assertEqual(config_payload["remote_nodes"][0]["id"], "dev")
            self.assertTrue(
                config_payload["remote_nodes"][0]["reconnect_enabled"]
            )
            rendered_config = json.dumps(config_payload)
            self.assertNotIn("node-secret", rendered_config)
            self.assertNotIn("127.0.0.1:17861", rendered_config)
            self.assertNotIn("raise SystemExit", rendered_config)
            self.assertEqual(reconnect_payload["phase"], "idle")
            remote = [
                row for row in sessions_payload["sessions"]
                if row.get("node_id") == "dev"
            ]
            self.assertEqual(len(remote), 1)
            self.assertEqual(remote[0]["remote_run_id"], "remote-run")
            self.assertEqual(
                parse_qualified_run_id(remote[0]["run_id"]),
                ("dev", "remote-run"),
            )


if __name__ == "__main__":
    unittest.main()
