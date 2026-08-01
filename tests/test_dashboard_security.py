import os
import plistlib
import stat
import subprocess
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import dashboard
import local_settings
from launchd.render_plist import render_plist


class LocalSettingsTests(unittest.TestCase):
    def test_explicit_token_wins(self):
        with patch.dict(
            os.environ, {"ORCH_DASHBOARD_TOKEN": "explicit-token"}
        ):
            self.assertEqual(
                local_settings.dashboard_token(), "explicit-token"
            )

    def test_installed_plist_is_a_legacy_fallback(self):
        missing = Mock()
        missing.read_text.side_effect = OSError
        installed = Mock()
        installed.read_bytes.return_value = plistlib.dumps({
            "EnvironmentVariables": {
                "ORCH_DASHBOARD_TOKEN": "installed-token"
            }
        })
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(
                 local_settings, "dashboard_token_file", return_value=missing
             ), \
             patch.object(
                 local_settings, "LEGACY_DASHBOARD_TOKEN_FILE", missing
             ), \
             patch.object(
                 local_settings, "INSTALLED_DASHBOARD_PLIST", installed
             ):
            self.assertEqual(
                local_settings.dashboard_token(), "installed-token"
            )

    def test_remote_bind_requires_authentication(self):
        local_settings.require_dashboard_auth("127.0.0.1", "")
        local_settings.require_dashboard_auth("::1", "")
        with self.assertRaises(ValueError):
            local_settings.require_dashboard_auth("0.0.0.0", "")
        local_settings.require_dashboard_auth("0.0.0.0", "token")

    def test_launchagent_renderer_escapes_values_and_hides_token(self):
        template = Path("launchd/com.user.orch-dashboard.plist.template")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "dashboard.plist"
            project_dir = "/Users/example/Projects/R&D <dashboard>"
            token_file = "/Users/example/.config/orch/token&cache"
            render_plist(template, destination, [
                "/usr/bin/python3",
                project_dir,
                "/usr/local/bin:/usr/bin:/bin",
                token_file,
                f"{project_dir}/outputs",
                "/Users/example",
                "127.0.0.1",
                "7860",
            ])
            data = plistlib.loads(destination.read_bytes())

            self.assertEqual(data["WorkingDirectory"], project_dir)
            environment = data["EnvironmentVariables"]
            self.assertEqual(
                environment["ORCH_DASHBOARD_TOKEN_FILE"], token_file
            )
            self.assertNotIn("ORCH_DASHBOARD_TOKEN", environment)
            self.assertEqual(
                data["ProgramArguments"][4], "127.0.0.1"
            )
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)


class DashboardAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.autosave = os.environ.get("ORCH_ACTIVE_SNAPSHOT_AUTOSAVE")
        os.environ["ORCH_ACTIVE_SNAPSHOT_AUTOSAVE"] = "0"
        cls.app = dashboard.create_app(
            Path("/nonexistent/agent-orchestrator-auth-test"),
            token="test-token", ttyd_enabled=False,
        )

    @classmethod
    def tearDownClass(cls):
        if cls.autosave is None:
            os.environ.pop("ORCH_ACTIVE_SNAPSHOT_AUTOSAVE", None)
        else:
            os.environ["ORCH_ACTIVE_SNAPSHOT_AUTOSAVE"] = cls.autosave

    def test_http_and_tty_routes_require_authentication(self):
        with TestClient(self.app) as client:
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertNotIn("outputs_dir", health.json())
            self.assertEqual(health.json()["bind_host"], "127.0.0.1")
            self.assertEqual(health.json()["scheme"], "http")
            self.assertEqual(client.get("/api/sessions").status_code, 401)
            self.assertEqual(client.get("/tty/missing").status_code, 401)

            root = client.get("/?token=test-token")
            self.assertEqual(root.status_code, 200)
            self.assertEqual(root.cookies.get("orch_token"), "test-token")
            self.assertIn("HttpOnly", root.headers.get("set-cookie", ""))
            self.assertEqual(client.get("/api/sessions").status_code, 200)

            schema = client.get("/openapi.json").json()
            self.assertNotIn(
                "/tty/{session}/{subpath}", schema.get("paths", {})
            )

    def test_tty_websocket_rejects_missing_token(self):
        with TestClient(self.app) as client:
            with self.assertRaises(WebSocketDisconnect) as caught:
                with client.websocket_connect("/tty/missing/ws"):
                    pass
            self.assertEqual(caught.exception.code, 1008)

    def test_lifespan_starts_and_stops_autosave_thread(self):
        with patch.dict(os.environ, {
            "ORCH_ACTIVE_SNAPSHOT_AUTOSAVE": "1",
            "ORCH_ACTIVE_SNAPSHOT_AUTOSAVE_INTERVAL_SEC": "3600",
        }):
            app = dashboard.create_app(
                Path("/nonexistent/agent-orchestrator-lifespan-test"),
                ttyd_enabled=False,
            )

        with TestClient(app):
            thread = app.state.active_snapshot_autosave_thread
            self.assertIsInstance(thread, threading.Thread)
            self.assertTrue(thread.is_alive())

        self.assertIsNone(app.state.active_snapshot_autosave_stop)
        self.assertIsNone(app.state.active_snapshot_autosave_thread)
        self.assertFalse(thread.is_alive())


class CleanCommandTests(unittest.TestCase):
    def test_clean_only_stops_orchestrator_sessions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            log_path = temp / "killed.txt"
            tmux = bin_dir / "tmux"
            tmux.write_text("""#!/usr/bin/env bash
if [[ "$1" == "list-sessions" ]]; then
    printf 'orch-first\\npersonal-work\\norch-second\\n'
elif [[ "$1" == "kill-session" ]]; then
    printf '%s\\n' "$3" >> "$TMUX_TEST_LOG"
else
    exit 2
fi
""")
            tmux.chmod(0o700)
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["TMUX_TEST_LOG"] = str(log_path)

            result = subprocess.run(
                ["bash", "scripts/clean.sh"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                log_path.read_text().splitlines(),
                ["orch-first", "orch-second"],
            )


class DeploymentScriptTests(unittest.TestCase):
    def test_live_sync_preserves_local_and_runtime_data(self):
        script = Path("launchd/deploy.sh").read_text()
        for path in (
            "outputs/",
            "projects/",
            ".dashboard-certs/",
            "dashboard.local.json",
            "tasks/local-test.yaml",
            "tasks/fix-hang-issue.yaml",
            "tasks/private/",
            "docs/dashboard-session-summary.md",
            "docs/internal/",
        ):
            self.assertIn(f"--exclude='{path}'", script)
        self.assertIn("--filter='protect docs/'", script)
        self.assertIn("$LIVE_DIR/.venv/bin/python", script)
        self.assertIn("Python 3.10+ is required", script)


if __name__ == "__main__":
    unittest.main()
