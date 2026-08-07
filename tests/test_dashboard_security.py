import json
import os
import plistlib
import stat
import subprocess
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_orchestrator import (
    cli, dashboard, local_settings, sync_status, sync_transfer,
)
from agent_orchestrator import terminal_theme
from agent_orchestrator.state import StateManager
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

    def test_package_modules_resolve_repository_resources(self):
        project_dir = Path(__file__).resolve().parents[1]
        self.assertEqual(cli.PROJECT_DIR, project_dir)
        self.assertEqual(cli.SCRIPTS_DIR, project_dir / "scripts")
        self.assertEqual(dashboard.PROJECT_DIR, project_dir)
        self.assertEqual(dashboard.STATIC_DIR, project_dir / "static")
        self.assertEqual(dashboard.SCRIPTS_DIR, project_dir / "scripts")
        self.assertEqual(local_settings.PROJECT_DIR, project_dir)

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


class MetadataConcurrencyTests(unittest.TestCase):
    def test_parallel_link_commands_preserve_metadata_and_every_link(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            metadata = run_dir / "session.json"
            original = {
                "kind": "run",
                "run_id": "concurrent-test::session",
                "name": "concurrent-test",
                "agent": "codex",
                "panel_state": "p1",
                "terminal_theme": "soft-dark",
                "resume": {"id": "resume-id"},
                "linked_folders": [],
            }
            metadata.write_text(json.dumps(original))
            urls = [f"https://example.com/item/{index}" for index in range(16)]
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        str(cli.PROJECT_DIR / "orchestrator.py"),
                        "link-url",
                        url,
                        "--run-dir",
                        str(run_dir),
                    ],
                    cwd=cli.PROJECT_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for url in urls
            ]

            results = [process.communicate(timeout=20) for process in processes]

            failures = [
                f"stdout={stdout}\nstderr={stderr}"
                for process, (stdout, stderr) in zip(processes, results)
                if process.returncode
            ]
            self.assertEqual(failures, [])
            saved = json.loads(metadata.read_text())
            for key, value in original.items():
                if key != "linked_folders":
                    self.assertEqual(saved[key], value)
            self.assertEqual(
                {item["path"] for item in saved["linked_folders"]}, set(urls)
            )
            self.assertEqual(list(run_dir.glob(".session.json.*.tmp")), [])

    def test_state_manager_update_preserves_dashboard_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = Path(temp_dir) / "state.json"
            metadata.write_text(json.dumps({
                "worker": {
                    "status": "pending",
                    "panel_state": "p0",
                    "linked_urls": [{
                        "path": "https://example.com/evidence",
                        "label": "Evidence",
                    }],
                }
            }))

            StateManager(metadata).update("worker", status="running")

            saved = json.loads(metadata.read_text())["worker"]
            self.assertEqual(saved["status"], "running")
            self.assertEqual(saved["panel_state"], "p0")
            self.assertEqual(saved["linked_urls"], [{
                "path": "https://example.com/evidence",
                "label": "Evidence",
            }])


class TerminalThemeTests(unittest.TestCase):
    def test_theme_names_are_normalized(self):
        self.assertEqual(
            terminal_theme.normalize_terminal_theme("Soft Green"),
            "soft-green",
        )
        self.assertEqual(
            terminal_theme.normalize_terminal_theme("white"),
            "light",
        )
        self.assertEqual(terminal_theme.normalize_terminal_theme("dark"), "")
        self.assertEqual(terminal_theme.normalize_terminal_theme("unknown"), "")

    def test_ttyd_theme_patch_changes_only_known_theme_literal(self):
        original = terminal_theme._TTYD_DARK_THEME_JS.encode()
        patched = terminal_theme.patch_ttyd_index_theme(
            b"before " + original + b" after",
            "soft-dark",
        )
        self.assertNotEqual(patched, b"before " + original + b" after")
        self.assertIn(b'background:"#1f242c"', patched)
        self.assertEqual(
            terminal_theme.patch_ttyd_index_theme(b"unchanged", "soft-dark"),
            b"unchanged",
        )

class DashboardAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.autosave = os.environ.get("ORCH_ACTIVE_SNAPSHOT_AUTOSAVE")
        os.environ["ORCH_ACTIVE_SNAPSHOT_AUTOSAVE"] = "0"
        with patch.dict(os.environ, {
            "ORCH_DASHBOARD_CONFIG": "/nonexistent/dashboard.local.json",
        }):
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

    def test_missing_projects_root_falls_back_to_home(self):
        with patch.dict(os.environ, {
            "ORCH_PROJECTS_ROOT": "/definitely/missing/projects/root",
        }):
            config = dashboard._dashboard_client_config()

        self.assertEqual(config["projects_root"], str(Path.home()))

    def test_create_rejects_a_missing_working_directory(self):
        with TestClient(self.app) as client:
            client.get("/?token=test-token")
            response = client.post("/api/create", json={
                "agent": "codex",
                "mode": "background",
                "cwd": "/definitely/missing/working/directory",
            })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "working directory does not exist: "
            "/definitely/missing/working/directory",
        )

    def test_sync_status_is_disabled_by_default(self):
        with TestClient(self.app) as client:
            client.get("/?token=test-token")
            response = client.get("/api/sync/status")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {
                "enabled": False,
                "phase": "disabled",
            })


class SyncStatusTests(unittest.TestCase):
    def test_classifies_one_sided_changes_deletions_and_conflicts(self):
        record = sync_status.FileRecord
        baseline = {
            name: record(name, "file", 4, 1)
            for name in ("local", "remote", "conflict", "same", "deleted")
        }
        local = dict(baseline)
        remote = dict(baseline)
        local["local"] = record("local", "file", 5, 2)
        remote["remote"] = record("remote", "file", 6, 2)
        local["conflict"] = record("conflict", "file", 7, 2)
        remote["conflict"] = record("conflict", "file", 8, 3)
        shared = record("same", "file", 9, 4)
        local["same"] = shared
        remote["same"] = shared
        local.pop("deleted")

        result = sync_status.classify_records(local, remote, baseline)

        self.assertEqual(result["counts"], {
            "unchanged": 0,
            "local_only": 2,
            "remote_only": 1,
            "same_change": 1,
            "conflict": 1,
        })

    def test_service_uses_persistent_remote_baseline_and_content_hashes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            local = temp / "local"
            remote = temp / "remote"
            local.mkdir()
            remote.mkdir()
            (local / "task.txt").write_text("base")
            (remote / "task.txt").write_text("base")
            stamp = 1_700_000_000_000_000_000
            os.utime(local / "task.txt", ns=(stamp, stamp))
            os.utime(remote / "task.txt", ns=(stamp, stamp))
            settings = sync_status.SyncSettings(
                enabled=True,
                local_root=str(local),
                remote_root=str(remote),
                paths=("task.txt",),
                state_db=str(temp / "state" / "sync.sqlite3"),
            )
            service = sync_status.SyncStatusService(settings)
            try:
                service.refresh_now()
                self.assertEqual(service.initialize_baseline("remote"), 1)

                (local / "task.txt").write_text("local")
                service.refresh_now()
                self.assertEqual(service.status()["counts"]["local_only"], 1)

                (remote / "task.txt").write_text("other")
                service.refresh_now()
                self.assertEqual(service.status()["counts"]["conflict"], 1)

                (remote / "task.txt").write_text("local")
                remote_stamp = stamp + 99_000_000_000
                os.utime(remote / "task.txt", ns=(remote_stamp, remote_stamp))
                service.refresh_now()
                self.assertEqual(service.status()["counts"]["same_change"], 1)
                self.assertEqual(service.status()["counts"]["conflict"], 0)
            finally:
                service.stop()

    def test_scanner_ignores_git_objects_but_tracks_head(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            (repo / ".git" / "refs" / "heads").mkdir(parents=True)
            (repo / ".git" / "objects" / "aa").mkdir(parents=True)
            (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            (repo / ".git" / "refs" / "heads" / "main").write_text("abc123\n")
            (repo / ".git" / "objects" / "aa" / "blob").write_text("object")
            (repo / "code.py").write_text("print('ok')\n")

            records = list(sync_status.scan_paths(root, ("repo",), ()))
            paths = {record.path for record in records}

            self.assertIn("repo/.git/HEAD", paths)
            self.assertIn("repo/code.py", paths)
            self.assertNotIn("repo/.git/objects/aa/blob", paths)

    def test_service_syncs_one_sided_update_and_advances_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            local = temp / "local"
            remote = temp / "remote"
            local.mkdir()
            remote.mkdir()
            (local / "task.txt").write_text("base")
            (remote / "task.txt").write_text("base")
            stamp = 1_700_000_000_000_000_000
            os.utime(local / "task.txt", ns=(stamp, stamp))
            os.utime(remote / "task.txt", ns=(stamp, stamp))
            service = sync_status.SyncStatusService(sync_status.SyncSettings(
                enabled=True, local_root=str(local), remote_root=str(remote),
                paths=("task.txt",),
                state_db=str(temp / "state" / "sync.sqlite3"),
            ))
            try:
                service.refresh_now()
                service.initialize_baseline("remote")
                (local / "task.txt").write_text("new local content")
                service.request_sync("now")

                self.assertTrue(service._execute_sync_request("now"))

                self.assertEqual(
                    (remote / "task.txt").read_text(), "new local content"
                )
                status = service.status()
                self.assertEqual(status["sync_job"]["state"], "complete")
                self.assertEqual(status["counts"]["local_only"], 0)
                self.assertEqual(status["counts"]["conflict"], 0)
            finally:
                service.stop()

    def test_sync_accepts_existing_agreement_as_the_new_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            local = temp / "local"
            remote = temp / "remote"
            local.mkdir()
            remote.mkdir()
            (local / "task.txt").write_text("base")
            (remote / "task.txt").write_text("base")
            service = sync_status.SyncStatusService(sync_status.SyncSettings(
                enabled=True, local_root=str(local), remote_root=str(remote),
                paths=("task.txt",),
                state_db=str(temp / "state" / "sync.sqlite3"),
            ))
            try:
                service.refresh_now()
                service.initialize_baseline("remote")
                (local / "task.txt").write_text("same on both")
                (remote / "task.txt").write_text("same on both")
                service.request_sync("now")

                self.assertTrue(service._execute_sync_request("now"))

                status = service.status()
                self.assertEqual(status["sync_job"]["agreements_accepted"], 1)
                self.assertEqual(status["counts"]["same_change"], 0)
                self.assertEqual(status["counts"]["unchanged"], 1)
            finally:
                service.stop()

    def test_rejects_a_second_sync_while_one_is_queued(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            local = temp / "local"
            remote = temp / "remote"
            local.mkdir()
            remote.mkdir()
            (local / "task.txt").write_text("base")
            (remote / "task.txt").write_text("base")
            service = sync_status.SyncStatusService(sync_status.SyncSettings(
                enabled=True, local_root=str(local), remote_root=str(remote),
                paths=("task.txt",),
                state_db=str(temp / "state" / "sync.sqlite3"),
            ))
            try:
                service.refresh_now()
                service.initialize_baseline("remote")
                service.request_sync("when_idle")

                with self.assertRaisesRegex(RuntimeError, "already active"):
                    service.request_sync("now")
            finally:
                service.stop()

    def test_comparison_uses_rsync_compatible_timestamp_precision(self):
        local = sync_status.FileRecord(
            "same.txt", "file", 4, 1_700_000_000_987_654_321
        )
        remote = sync_status.FileRecord(
            "same.txt", "file", 4, 1_700_000_000_000_000_000
        )

        result = sync_status.classify_records(
            {"same.txt": local}, {"same.txt": remote}, {"same.txt": remote}
        )

        self.assertEqual(result["counts"]["unchanged"], 1)
        self.assertEqual(result["counts"]["local_only"], 0)


class SyncTransferTests(unittest.TestCase):
    def test_plan_skips_conflicts_deletions_git_large_and_busy_paths(self):
        changes = [
            {"path": "push.txt", "state": "local_only", "kind": "file",
             "size": 4, "local_present": True, "remote_present": False},
            {"path": "pull.txt", "state": "remote_only", "kind": "file",
             "size": 4, "local_present": False, "remote_present": True},
            {"path": "busy/out.txt", "state": "local_only", "kind": "file",
             "size": 4, "local_present": True, "remote_present": False},
            {"path": "deleted.txt", "state": "local_only", "kind": "file",
             "size": 4, "local_present": False, "remote_present": True},
            {"path": "repo/.git/HEAD", "state": "local_only", "kind": "git-head",
             "size": 40, "local_present": True, "remote_present": True},
            {"path": "large.bin", "state": "local_only", "kind": "file",
             "size": 100, "local_present": True, "remote_present": False},
            {"path": "conflict.txt", "state": "conflict", "kind": "file",
             "size": 4, "local_present": True, "remote_present": True},
        ]

        plan = sync_transfer.build_transfer_plan(
            changes, local_busy=("busy",), max_file_bytes=50,
        )

        self.assertEqual(plan.push, ["push.txt"])
        self.assertEqual(plan.pull, ["pull.txt"])
        self.assertEqual(plan.busy, ["busy/out.txt"])
        self.assertEqual(plan.deletions, ["deleted.txt"])
        self.assertEqual(plan.git_refs, ["repo/.git/HEAD"])
        self.assertEqual(plan.too_large, ["large.bin"])
        self.assertEqual(plan.conflicts, ["conflict.txt"])

    def test_busy_root_blocks_every_transfer(self):
        change = {
            "path": "project/file.txt", "state": "local_only", "kind": "file",
            "size": 4, "local_present": True, "remote_present": False,
        }
        plan = sync_transfer.build_transfer_plan([change], local_busy=("",))
        self.assertEqual(plan.actionable, 0)
        self.assertEqual(plan.busy, ["project/file.txt"])

    def test_local_transfer_moves_updates_both_ways_without_deleting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            local = temp / "local"
            remote = temp / "remote"
            (local / "project").mkdir(parents=True)
            (remote / "project").mkdir(parents=True)
            (local / "project" / "push.txt").write_text("from local")
            (remote / "project" / "pull.txt").write_text("from remote")
            (remote / "project" / "keep.txt").write_text("keep")
            plan = sync_transfer.TransferPlan(
                push=["project/push.txt"], pull=["project/pull.txt"],
                deletions=["project/keep.txt"],
            )
            transfer = sync_transfer.WorkspaceTransfer(
                local_root=local, remote_root=str(remote), remote_host="",
                excludes=(), timeout_seconds=30,
            )

            result = transfer.execute(plan)

            self.assertTrue(result["ok"])
            self.assertEqual(
                (remote / "project" / "push.txt").read_text(), "from local"
            )
            self.assertEqual(
                (local / "project" / "pull.txt").read_text(), "from remote"
            )
            self.assertEqual(
                (remote / "project" / "keep.txt").read_text(), "keep"
            )


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
