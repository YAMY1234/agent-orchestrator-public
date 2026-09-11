import base64
import io
import json
import os
import plistlib
import stat
import subprocess
import sys
import threading
import tempfile
import time
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


class NativeResumeCaptureTests(unittest.TestCase):
    def test_codex_resume_matches_symlinked_working_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            real_cwd = root / "lustre" / "project"
            alias_cwd = home / "Projects"
            transcript_dir = home / ".codex" / "sessions" / "2026" / "08" / "31"
            real_cwd.mkdir(parents=True)
            alias_cwd.parent.mkdir(parents=True)
            alias_cwd.symlink_to(real_cwd, target_is_directory=True)
            transcript_dir.mkdir(parents=True)
            transcript = transcript_dir / (
                "rollout-2026-08-31T21-09-18-"
                "01a05b28-3d80-7582-9614-f6c86990d736.jsonl"
            )
            transcript.write_text(json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": "01a05b28-3d80-7582-9614-f6c86990d736",
                    "timestamp": "2026-09-01T04:09:18Z",
                    "cwd": str(real_cwd),
                },
            }) + "\n")

            with patch.object(dashboard.Path, "home", return_value=home):
                meta, candidates = dashboard._find_codex_resume_near_start(
                    str(alias_cwd), "2026-09-01T04:08:21Z",
                )

            self.assertEqual(
                meta["resume_id"],
                "01a05b28-3d80-7582-9614-f6c86990d736",
            )
            self.assertEqual(len(candidates), 1)


class DashboardNotificationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (dashboard.STATIC_DIR / "index.html").read_text()

    def test_same_native_session_reuses_one_system_notification_tag(self):
        self.assertIn('const notifKey = nativeIdentity(s) || s.run_id;', self.source)
        self.assertIn('tag: "orch-idle-" + notifKey,', self.source)
        self.assertIn('systemNotifs.set(notifKey, n);', self.source)

    def test_manual_session_inspection_acknowledges_notification(self):
        self.assertIn(
            'if (card.dataset.runId) idleNotif.acknowledge(card.dataset.runId);',
            self.source,
        )
        self.assertIn(
            'idleNotif.acknowledge(runId);\n      }, { capture: true });',
            self.source,
        )
        self.assertIn('try { notification.close(); } catch (_) {}', self.source)

    def test_screen_activity_fallback_cannot_notify(self):
        marker = "// Terminal screen hashes remain useful for the green activity"
        start = self.source.index(marker)
        end = self.source.index("// Prune tracks", start)
        fallback_block = self.source[start:end]
        self.assertNotIn("notify(", fallback_block)
        self.assertNotIn("markUnread(", fallback_block)

    def test_image_paste_is_wired_for_input_and_tty_iframe(self):
        self.assertIn(
            'sendToTty: ctrl.usingTty,',
            self.source,
        )
        self.assertIn(
            "bridgeTtyControlSlash(ifr, runId);",
            self.source,
        )
        self.assertIn('/paste-image`, {', self.source)
        self.assertIn("normalizeClipboardImageForUpload(file)", self.source)
        self.assertIn('"image/tiff", "image/x-tiff"', self.source)

    def test_iframe_files_remain_binary_across_javascript_realms(self):
        self.assertIn("function isRawRequestBody(body)", self.source)
        self.assertIn('tag === "[object File]"', self.source)
        api_start = self.source.index("async function api(")
        api_end = self.source.index("function authQS()", api_start)
        self.assertIn(
            "const rawBody = isRawRequestBody(opts.body);",
            self.source[api_start:api_end],
        )

    def test_tty_image_paste_uses_native_agent_input(self):
        self.assertIn(
            'if (sendToTty && !session.remote && files.length === 1)',
            self.source,
        )
        self.assertIn('await sendKey(runId, "C-v");', self.source)
        self.assertIn('agent.includes("claude")', self.source)
        self.assertIn('paths.map(path => `@${path} `)', self.source)
        self.assertIn('await sendText(runId, text, false);', self.source)
        self.assertNotIn('<image name=[Pasted Image]', self.source)


class DashboardExitedSessionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (dashboard.STATIC_DIR / "index.html").read_text()

    def test_exited_agent_gets_a_contextual_end_control(self):
        self.assertIn('class="btn-end-session"', self.source)
        self.assertIn("endExitedSession(runId, btnEndSession)", self.source)
        self.assertIn('text: "exited"', self.source)

    def test_close_pane_remains_separate_from_ending_session(self):
        self.assertIn('class="btn-unpin" title="Close this pane">×</button>', self.source)
        end_start = self.source.index("async function endExitedSession(")
        end_block = self.source[end_start:self.source.index(
            "// ---------- Layout / tty toggles ----------", end_start
        )]
        self.assertIn("/stop`, { method: \"POST\" }", end_block)
        self.assertIn("closePane(runId);", end_block)


class DashboardAgentExitDetectionTests(unittest.TestCase):
    def setUp(self):
        dashboard._SESSION_BUSY_HASH.clear()
        dashboard._SESSION_LAST_CHANGE.clear()
        dashboard._SESSION_ACTIVITY_STREAK_START.clear()
        dashboard._SESSION_LAST_SUSTAINED_ACTIVE.clear()
        dashboard._SESSION_BACKGROUND_ACTIVE_START.clear()

    def test_recent_launcher_exit_marker_marks_agent_exited(self):
        with patch.object(
            dashboard,
            "tmux_capture_activity",
            return_value=(
                "bash: Bus error (core dumped)\n--- Agent exited ---\n"
                + "\n" * 40
            ),
        ):
            activity = dashboard._probe_session_activity("orch-crashed")

        self.assertTrue(activity["agent_exited"])
        self.assertFalse(activity["busy"])

    def test_marker_mentioned_in_prose_does_not_mark_agent_exited(self):
        with patch.object(
            dashboard,
            "tmux_capture_activity",
            return_value="The marker is --- Agent exited --- when the wrapper ends.\n",
        ):
            activity = dashboard._probe_session_activity("orch-working")

        self.assertFalse(activity["agent_exited"])

    def test_completed_turn_with_detached_shell_is_not_agent_activity(self):
        status = "* Cogitated for 38s · done 12:22 AM · 1 shell still running"
        active, reason = dashboard._detect_background_active(status)
        detached, detached_reason = dashboard._detect_detached_shell(status)

        self.assertFalse(active)
        self.assertEqual(reason, "")
        self.assertTrue(detached)
        self.assertIn("1 shell still running", detached_reason)

    def test_agent_waiting_for_shell_to_finish_is_background_activity(self):
        active, reason = dashboard._detect_background_active(
            "* Waiting for the verification command to finish"
        )

        self.assertTrue(active)
        self.assertIn("Waiting for", reason)


class DashboardPanelStateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (dashboard.STATIC_DIR / "index.html").read_text()

    def test_stale_session_poll_cannot_overwrite_pending_panel_state(self):
        self.assertIn("const pendingPanelStates = new Map();", self.source)
        self.assertIn(
            "sessions = mergePendingPanelStates(data.sessions || []);",
            self.source,
        )
        self.assertIn(
            "pending.confirmed && serverValue === pending.value",
            self.source,
        )

    def test_panel_state_is_applied_before_persistence_finishes(self):
        start = self.source.index("async function setPanelState(")
        end = self.source.index("async function setTerminalTheme(", start)
        mutation = self.source[start:end]
        self.assertLess(
            mutation.index("applyPanelStateLocally(runId, value);"),
            mutation.index("await api("),
        )
        self.assertIn("pending.sequence === sequence", mutation)

    def test_persisted_panel_state_requests_a_fresh_session_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outputs = root / "outputs"
            run_dir = outputs / "demo-run"
            run_dir.mkdir(parents=True)
            session_file = run_dir / "session.json"
            session_file.write_text(json.dumps({
                "name": "demo",
                "cwd": str(root),
                "status": "running",
            }))
            app = dashboard.create_app(
                outputs, ttyd_enabled=False, remote_nodes_enabled=False,
            )

            with TestClient(app) as client, patch.object(
                app.state.session_snapshots, "request_refresh"
            ) as request_refresh:
                response = client.post(
                    "/api/sessions/demo-run::demo/panel-state",
                    json={"panel_state": "p0"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                json.loads(session_file.read_text())["panel_state"], "p0",
            )
            request_refresh.assert_called_once_with()


class DashboardSidebarLocationGroupingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (dashboard.STATIC_DIR / "index.html").read_text()

    def test_sidebar_groups_by_location_and_keeps_priority_on_each_item(self):
        render_start = self.source.index("function renderList()")
        render_end = self.source.index("function sidebarGroupKey(", render_start)
        render = self.source[render_start:render_end]
        self.assertIn("sidebarLocationGroups(filtered)", render)
        self.assertIn('class="session-location-group', render)
        self.assertIn("location.items.map(sessionItemHtml)", render)
        self.assertNotIn('class="session-group', render)
        self.assertNotIn("priorityCollapseKey", render)
        self.assertNotIn('class="session-node-badge', self.source)

    def test_location_identity_uses_remote_node_metadata(self):
        self.assertIn("function sidebarLocationInfo(session)", self.source)
        self.assertIn('key: "local", label: "Local"', self.source)
        self.assertIn("session.node_label || configured?.label || nodeId", self.source)
        self.assertIn("REMOTE_NODES.map((node, index)", self.source)

    def test_offline_remote_sessions_keep_their_saved_pane_slots(self):
        self.assertIn("function remoteNodeIdFromRunId(runId)", self.source)
        self.assertIn("function retainMissingRemoteSlot(runId, nodeStates)", self.source)
        self.assertIn("!retainMissingRemoteSlot(slots[i], remoteNodeStates)", self.source)

    def test_location_groups_have_distinct_visual_tones(self):
        self.assertIn("function sidebarLocationToneClass(location)", self.source)
        self.assertIn("location-tone-local", self.source)
        self.assertIn("location-tone-remote-1", self.source)
        self.assertIn("location-tone-remote-2", self.source)
        self.assertIn(
            ".session-location-group + .session-location-group",
            self.source,
        )

    def test_double_clicking_sidebar_session_opens_zoomed_tty(self):
        render_start = self.source.index("function renderList()")
        render_end = self.source.index("async function deleteEndedSession", render_start)
        render = self.source[render_start:render_end]
        self.assertIn('node.addEventListener("dblclick", (ev) => {', render)
        self.assertIn("clearTimeout(clickTimer);", render)
        self.assertIn("idleNotif.focusSession(id);", render)
        self.assertIn("visualState, focusSession,", self.source)

    def test_offline_remote_group_offers_self_service_reconnect(self):
        self.assertIn('id="reconnect-modal"', self.source)
        self.assertIn('data-node-reconnect=', self.source)
        self.assertIn("async function openNodeReconnect(nodeId, label)", self.source)
        self.assertIn("/reconnect/continue", self.source)
        self.assertIn("/reconnect/cancel", self.source)

    def test_reconnect_offers_explicit_snapshot_recovery(self):
        self.assertIn('id="reconnect-recovery"', self.source)
        self.assertIn('id="reconnect-restore"', self.source)
        self.assertIn("async function loadNodeRecoveryStatus", self.source)
        self.assertIn("/active-snapshot/restore", self.source)

    def test_remote_reconnect_status_polling_is_serial_and_bounded(self):
        self.assertIn("function scheduleNodeReconnectPoll", self.source)
        self.assertIn("const controller = new AbortController();", self.source)
        self.assertNotIn("setInterval(pollNodeReconnect", self.source)

    def test_active_sessions_suppress_attention_visuals(self):
        self.assertIn("function sessionActivityMode(s)", self.source)
        attention_start = self.source.index(
            "function sessionPersistentAttentionState(s)"
        )
        attention_end = self.source.index(
            "function sessionVisibleUnreadState(s)", attention_start,
        )
        attention = self.source[attention_start:attention_end]
        self.assertIn('if (!s || sessionActivityMode(s)) return "";', attention)
        self.assertIn('return "background";', self.source)
        self.assertIn(".dot.background-working", self.source)
        self.assertIn(".pane-card.background-working", self.source)

    def test_unread_header_does_not_use_persistent_attention(self):
        start = self.source.index("function updatePaneUnreadVisual(ctrl, session)")
        end = self.source.index("function activePanelCandidates()", start)
        update = self.source[start:end]
        self.assertIn("const state = sessionVisibleUnreadState(s);", update)
        self.assertNotIn("const state = sessionAttentionState(s);", update)

    def test_goal_active_is_background_work(self):
        start = self.source.index("function sessionGoalActive(s)")
        end = self.source.index("function sessionPersistentAttentionState(s)", start)
        activity = self.source[start:end]
        self.assertIn('goalState === "pursuing"', activity)
        self.assertIn("if (sessionGoalActive(s)", activity)
        self.assertIn('return "background";', activity)

    def test_waiting_native_state_ignores_a_detached_shell(self):
        start = self.source.index("function sessionTerminalWaitActive(s)")
        end = self.source.index("function sessionPersistentAttentionState(s)", start)
        activity = self.source[start:end]
        self.assertIn("function sessionDetachedShellHint(s)", activity)
        self.assertIn('if (nativeWaiting) return "";', activity)
        self.assertIn("sessionTerminalWaitActive(s)", activity)

    def test_remote_reconnect_retries_transient_ssh_outages(self):
        self.assertIn("function retryableNodeReconnectFailure(status)", self.source)
        self.assertIn("function scheduleNodeReconnectRetry(status)", self.source)
        self.assertIn("retrying automatically in 5 seconds", self.source)
        self.assertIn("}, 5000);", self.source)

    def test_overdue_p0_and_p1_waiting_states_keep_attention_borders(self):
        start = self.source.index("function sessionPersistentAttentionState(s)")
        end = self.source.index("function sessionVisibleUnreadState(s)", start)
        attention = self.source[start:end]
        self.assertIn("waitingAge >= 5 * 60", attention)
        self.assertIn('priority === "p0"', attention)
        self.assertIn('return "needs-input"', attention)
        self.assertIn('priority === "p1"', attention)
        self.assertIn('return "reminder"', attention)
        self.assertIn("function sessionVisibleUnreadState(s)", self.source)

    def test_blocked_visual_is_distinct_from_needs_input(self):
        start = self.source.index("function sessionPersistentAttentionState(s)")
        end = self.source.index("function sessionVisibleUnreadState(s)", start)
        attention = self.source[start:end]
        self.assertIn('missionState === "blocked"', attention)
        self.assertIn('return "blocked";', attention)
        self.assertIn('.dot.unread-blocked', self.source)
        self.assertIn('.pane-card.attention-blocked', self.source)

    def test_priority_badges_no_longer_own_pane_border(self):
        self.assertNotIn(".pane-card.idle-p0", self.source)
        self.assertNotIn(".pane-card.idle-p1", self.source)
        self.assertIn(".pane-card.attention-ready", self.source)
        self.assertIn(".pane-card.attention-reminder", self.source)
        self.assertIn(".pane-card.attention-needs-input", self.source)
        self.assertIn(".pane-card.attention-blocked", self.source)

    def test_notifications_use_effective_activity_not_raw_busy_only(self):
        check_start = self.source.index("function check(list)")
        check_end = self.source.index("function visualState(runId)", check_start)
        check = self.source[check_start:check_end]
        self.assertIn("const busy = !!sessionActivityMode(s);", check)
        self.assertIn("!sessionActivityMode(fresh)", check)


class ClipboardImageUploadTests(unittest.TestCase):
    def test_upload_stores_a_private_png_beside_the_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outputs = root / "outputs"
            run_dir = outputs / "demo-run"
            run_dir.mkdir(parents=True)
            (run_dir / "session.json").write_text(json.dumps({
                "name": "demo",
                "cwd": str(root),
                "status": "running",
            }))
            app = dashboard.create_app(
                outputs, ttyd_enabled=False, remote_nodes_enabled=False,
            )
            image = b"\x89PNG\r\n\x1a\nlocal-image"

            with TestClient(app) as client:
                response = client.post(
                    "/api/sessions/demo-run::demo/paste-image",
                    content=image,
                    headers={"Content-Type": "image/png"},
                )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            target = Path(payload["path"])
            self.assertEqual(target.parent, (run_dir / "pasted-images").resolve())
            self.assertEqual(target.read_bytes(), image)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_upload_converts_macos_tiff_clipboard_image_to_png(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outputs = root / "outputs"
            run_dir = outputs / "demo-run"
            run_dir.mkdir(parents=True)
            (run_dir / "session.json").write_text(json.dumps({
                "name": "demo",
                "cwd": str(root),
                "status": "running",
            }))
            source = io.BytesIO()
            Image.new("RGB", (3, 2), (20, 80, 160)).save(source, format="TIFF")
            app = dashboard.create_app(
                outputs, ttyd_enabled=False, remote_nodes_enabled=False,
            )

            with TestClient(app) as client:
                response = client.post(
                    "/api/sessions/demo-run::demo/paste-image",
                    content=source.getvalue(),
                    headers={"Content-Type": "image/tiff"},
                )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            target = Path(payload["path"])
            self.assertEqual(target.suffix, ".png")
            self.assertEqual(payload["content_type"], "image/png")
            self.assertTrue(target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_upload_rejects_non_image_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            outputs = Path(temp_dir) / "outputs"
            run_dir = outputs / "demo-run"
            run_dir.mkdir(parents=True)
            (run_dir / "session.json").write_text(json.dumps({"name": "demo"}))
            app = dashboard.create_app(
                outputs, ttyd_enabled=False, remote_nodes_enabled=False,
            )

            with TestClient(app) as client:
                response = client.post(
                    "/api/sessions/demo-run::demo/paste-image",
                    content=b"not an image",
                    headers={"Content-Type": "text/plain"},
                )

            self.assertEqual(response.status_code, 415)


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

    def test_ttyd_output_theme_mapper_rewrites_true_black_across_frames(self):
        mapper = terminal_theme.TtydOutputThemeMapper("soft-dark")
        first = mapper.transform(b"0before\x1b[48;2;0;")
        second = mapper.transform(b"00;0mafter")

        self.assertEqual(first, [b"0before"])
        self.assertEqual(second, [b"0\x1b[48;2;31;36;44mafter"])
        self.assertIsNone(mapper.finish())

    def test_ttyd_output_theme_mapper_preserves_other_protocol_messages(self):
        mapper = terminal_theme.TtydOutputThemeMapper("soft-dark")
        self.assertEqual(mapper.transform(b"2preferences"), [b"2preferences"])
        self.assertEqual(
            mapper.transform(b"0\x1b[48;2;33;58;43mcontent"),
            [b"0\x1b[48;2;33;58;43mcontent"],
        )

    def test_ttyd_interaction_patch_is_idempotent(self):
        original = b"<html><body>terminal</body></html>"
        patched = terminal_theme.patch_ttyd_index_interactions(original)
        self.assertIn(b"orch-ttyd-interactions-v1", patched)
        self.assertIn(b"originalTriggerMouseEvent", patched)
        self.assertIn(b"_oscLinkService", patched)
        self.assertIn(b"getLinkData", patched)
        self.assertIn(b"extended.urlId", patched)
        self.assertIn(b"cleanHttpUrl", patched)
        self.assertIn("（【《〈「『〔［｛，。；：！？、".encode(), patched)
        self.assertIn(b"cleanHttpUrl(oscUrl)", patched)
        self.assertIn(b"cleanHttpUrl(raw)", patched)
        self.assertIn(b"balance < 0 && url.endsWith(closing)", patched)
        self.assertIn(b'screen.addEventListener("mousemove"', patched)
        self.assertIn(b"window.open(pendingUrl", patched)
        self.assertEqual(
            terminal_theme.patch_ttyd_index_interactions(patched),
            patched,
        )
        self.assertEqual(
            terminal_theme.patch_ttyd_index_interactions(b"not html"),
            b"not html",
        )


class TtydRecoveryTests(unittest.TestCase):
    def test_tmux_hyperlink_passthrough_preserves_existing_features(self):
        current = subprocess.CompletedProcess(
            args=["tmux", "show-options"], returncode=0,
            stdout="xterm*:clipboard:focus:title\n*:RGB\n", stderr="",
        )
        updated = subprocess.CompletedProcess(
            args=["tmux", "set-option"], returncode=0,
            stdout="", stderr="",
        )
        with patch.object(
            dashboard.subprocess, "run", side_effect=[current, updated],
        ) as run:
            enabled = dashboard._enable_tmux_hyperlink_passthrough()

        self.assertTrue(enabled)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                "tmux", "set-option", "-as", "terminal-features",
                ",xterm*:hyperlinks",
            ],
        )

    def test_tmux_hyperlink_passthrough_is_idempotent(self):
        current = subprocess.CompletedProcess(
            args=["tmux", "show-options"], returncode=0,
            stdout="xterm*:clipboard:hyperlinks:title\n", stderr="",
        )
        with patch.object(
            dashboard.subprocess, "run", return_value=current,
        ) as run:
            enabled = dashboard._enable_tmux_hyperlink_passthrough()

        self.assertTrue(enabled)
        run.assert_called_once()

    def test_persisted_theme_index_reads_active_run_and_task_sessions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            outputs = Path(temp_dir)
            run_dir = outputs / "run-one"
            task_dir = outputs / "run-two"
            run_dir.mkdir()
            task_dir.mkdir()
            (run_dir / "session.json").write_text(json.dumps({
                "tmux_session": "orch-run-one",
                "terminal_theme": "Soft Dark",
            }))
            (task_dir / "state.json").write_text(json.dumps({
                "task": {
                    "tmux_session": "orch-task-two",
                    "terminal_theme": "soft-green",
                },
            }))
            (outputs / ".active_sessions_snapshot.json").write_text(json.dumps({
                "sessions": [
                    {
                        "kind": "run",
                        "run_name": "run-one",
                        "tmux_session": "orch-run-one",
                        "terminal_theme": "light",
                    },
                    {
                        "kind": "task",
                        "run_name": "run-two",
                        "task": "task",
                        "tmux_session": "orch-task-two",
                        "terminal_theme": "light",
                    },
                ],
            }))

            themes = dashboard._terminal_themes_by_tmux_session(outputs)

        self.assertEqual(themes["orch-run-one"], "soft-dark")
        self.assertEqual(themes["orch-task-two"], "soft-green")

    def test_direct_proxy_reconnect_uses_remembered_theme(self):
        manager = dashboard.TtydManager(
            enabled=False,
            session_themes={"orch-themed": "soft-dark"},
        )
        manager.enabled = True
        proc = Mock()
        proc.poll.return_value = None

        with patch.object(
            dashboard, "tmux_alive", return_value=True,
        ), patch.object(
            dashboard, "_enable_tmux_hyperlink_passthrough",
            return_value=True,
        ), patch.object(
            dashboard, "ensure_shadow_session", return_value="orch-themed-web",
        ), patch.object(
            manager, "_next_free_port", return_value=7801,
        ), patch.object(
            manager, "_wait_for_port", return_value=True,
        ), patch.object(
            dashboard.subprocess, "Popen", return_value=proc,
        ) as popen:
            port = manager.port_for("orch-themed")

        self.assertEqual(port, 7801)
        command = popen.call_args.args[0]
        theme_option = next(
            value for value in command
            if isinstance(value, str) and value.startswith("theme=")
        )
        self.assertIn('"background":"#1f242c"', theme_option)
        self.assertEqual(manager.theme_for("orch-themed"), "soft-dark")

    def test_existing_shadow_is_not_reconfigured_on_reconnect(self):
        with patch.object(
            dashboard, "tmux_alive", side_effect=[True, True],
        ), patch.object(dashboard.subprocess, "run") as run:
            shadow = dashboard.ensure_shadow_session(
                "orch-task-existing", owner="dashboard-owner",
            )

        self.assertEqual(shadow, "orch-task-existing-web")
        run.assert_not_called()

    def test_shadow_option_timeout_does_not_escape_to_asgi(self):
        created = subprocess.CompletedProcess(
            args=["tmux", "new-session"], returncode=0,
            stdout="", stderr="",
        )
        with patch.object(
            dashboard, "tmux_alive", side_effect=[True, False],
        ), patch.object(
            dashboard.subprocess, "run",
            side_effect=[created, subprocess.TimeoutExpired("tmux", 5)],
        ) as run:
            shadow = dashboard.ensure_shadow_session(
                "orch-task-new", owner="dashboard-owner",
            )

        self.assertEqual(shadow, "orch-task-new-web")
        self.assertEqual(run.call_count, 2)

    def test_ensure_restarts_ttyd_when_shadow_session_is_missing(self):
        manager = dashboard.TtydManager(enabled=False)
        manager.enabled = True
        old_proc = Mock()
        old_proc.poll.return_value = None
        new_proc = Mock()
        manager._procs["orch-task-1"] = (old_proc, 7800, "soft-dark")

        with patch.object(
            dashboard,
            "tmux_alive",
            side_effect=lambda name: name == "orch-task-1",
        ), patch.object(
            dashboard, "_enable_tmux_hyperlink_passthrough",
            return_value=True,
        ), patch.object(
            manager, "_stop_proc"
        ) as stop_proc, patch.object(
            dashboard, "ensure_shadow_session",
            return_value="orch-task-1-web",
        ) as ensure_shadow, patch.object(
            manager, "_next_free_port", return_value=7801,
        ), patch.object(
            manager, "_wait_for_port", return_value=True,
        ), patch.object(
            dashboard.subprocess, "Popen", return_value=new_proc,
        ):
            port = manager.ensure("orch-task-1", theme="soft-dark")

        self.assertEqual(port, 7801)
        stop_proc.assert_called_once_with(old_proc)
        ensure_shadow.assert_called_once_with(
            "orch-task-1", owner=manager._owner
        )
        self.assertEqual(
            manager._procs["orch-task-1"],
            (new_proc, 7801, "soft-dark"),
        )

    def test_port_proxy_repairs_a_missing_shadow(self):
        manager = dashboard.TtydManager(enabled=False)
        old_proc = Mock()
        old_proc.poll.return_value = None
        manager._procs["orch-task-2"] = (old_proc, 7800, "soft-green")

        with patch.object(
            dashboard, "tmux_alive", return_value=False,
        ), patch.object(
            manager, "_stop_proc"
        ) as stop_proc, patch.object(
            manager, "ensure", return_value=7802,
        ) as ensure:
            port = manager.port_for("orch-task-2")

        self.assertEqual(port, 7802)
        stop_proc.assert_called_once_with(old_proc)
        ensure.assert_called_once_with("orch-task-2", theme="soft-green")

    def test_old_manager_cannot_kill_a_new_managers_shadow(self):
        with patch.object(
            dashboard, "_shadow_owner", return_value="new-owner",
        ), patch.object(dashboard.subprocess, "run") as run:
            killed = dashboard.kill_shadow_session(
                "orch-task-3", owner="old-owner"
            )

        self.assertFalse(killed)
        run.assert_not_called()

    def test_reap_dead_uses_one_tmux_inventory_for_all_panes(self):
        manager = dashboard.TtydManager(enabled=False)
        first = Mock()
        first.poll.return_value = None
        second = Mock()
        second.poll.return_value = None
        manager._procs = {
            "orch-one": (first, 7800, ""),
            "orch-two": (second, 7801, ""),
        }

        with patch.object(
            dashboard,
            "tmux_list_sessions",
            return_value=["orch-one", "orch-one-web"],
        ) as inventory, patch.object(
            dashboard, "tmux_alive", side_effect=AssertionError(
                "reap_dead must not probe every session separately"
            )
        ), patch.object(
            dashboard, "kill_shadow_session"
        ) as kill_shadow:
            reaped = manager.reap_dead(min_interval_s=0)

        self.assertEqual(reaped, 1)
        inventory.assert_called_once_with()
        first.terminate.assert_not_called()
        second.terminate.assert_called_once_with()
        kill_shadow.assert_called_once_with("orch-two", owner=manager._owner)


class SessionSnapshotTests(unittest.TestCase):
    def test_slow_scan_does_not_block_snapshot_readers_or_queue_refreshes(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def scan():
            calls.append(time.time())
            entered.set()
            release.wait(2)
            return [{"run_id": "demo::task"}]

        service = dashboard.SessionSnapshotService(scan, interval_s=60)
        service.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        snapshot = service.snapshot()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertFalse(snapshot["ready"])
        self.assertTrue(snapshot["scanning"])
        self.assertFalse(service.request_refresh())

        release.set()
        deadline = time.time() + 2
        while not service.snapshot()["ready"] and time.time() < deadline:
            time.sleep(0.01)
        ready = service.snapshot()
        service.stop()

        self.assertTrue(ready["ready"])
        self.assertEqual(ready["sessions"], [{"run_id": "demo::task"}])
        self.assertEqual(len(calls), 1)

    def test_foreign_backend_empty_autosave_preserves_recovery_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            outputs = Path(temp_dir)
            original = {
                "schema_version": 3,
                "backend_id": "previous-backend",
                "sessions": [{"resume_id": "resume-me"}],
            }
            dashboard._active_snapshot_path(outputs).write_text(json.dumps(original))
            empty = {
                "schema_version": 3,
                "backend_id": "current-backend",
                "sessions": [],
            }
            with patch.object(
                dashboard, "_build_active_snapshot", return_value=empty
            ):
                _, result = dashboard._save_active_snapshot(
                    outputs,
                    saved_by="auto-hourly",
                    preserve_foreign_nonempty=True,
                )

            self.assertTrue(result["write_skipped"])
            self.assertEqual(
                json.loads(dashboard._active_snapshot_path(outputs).read_text()),
                original,
            )


class ConversationMetricBackgroundTests(unittest.TestCase):
    def test_followup_snapshot_does_not_wait_for_cold_background_parse(self):
        entered = threading.Event()
        release = threading.Event()

        class SlowCache(dashboard.TranscriptMetricsCache):
            def _update(self, path, agent, state):
                entered.set()
                release.wait(2)
                state.initialized = True

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "large.jsonl"
            path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
            cache = SlowCache()
            with patch.object(dashboard, "_CONVERSATION_METRICS", cache):
                first = dashboard._conversation_metrics_snapshot(
                    path, "codex", window_start=0, window_end=time.time(),
                )
                self.assertTrue(first["loading"])
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                second = dashboard._conversation_metrics_snapshot(
                    path, "codex", window_start=0, window_end=time.time(),
                )
                elapsed = time.monotonic() - started
                release.set()

            self.assertTrue(second["loading"])
            self.assertLess(elapsed, 0.1)


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
                remote_nodes_enabled=False,
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
            self.assertTrue(health.json()["instance_id"])
            self.assertTrue(health.json()["backend_id"])
            self.assertEqual(client.get("/api/sessions").status_code, 401)
            self.assertEqual(client.get("/tty/missing").status_code, 401)

            root = client.get("/?token=test-token")
            self.assertEqual(root.status_code, 200)
            self.assertEqual(root.cookies.get("orch_token"), "test-token")
            self.assertIn("HttpOnly", root.headers.get("set-cookie", ""))
            sessions = client.get("/api/sessions")
            self.assertEqual(sessions.status_code, 200)
            self.assertEqual(
                sessions.json()["instance_id"], health.json()["instance_id"]
            )
            self.assertEqual(
                sessions.json()["backend_id"], health.json()["backend_id"]
            )
            self.assertIn("remote_nodes", sessions.json())

            schema = client.get("/openapi.json").json()
            self.assertNotIn(
                "/tty/{session}/{subpath}", schema.get("paths", {})
            )

            today = time.strftime("%Y-%m-%d")
            analytics = client.post("/api/mission-control/analytics", json={
                "start_date": today,
                "end_date": today,
                "run_ids": [],
            })
            self.assertEqual(analytics.status_code, 200)
            self.assertEqual(analytics.json()["days"], 1)
            self.assertEqual(analytics.json()["series"][0]["date"], today)

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
                ttyd_enabled=False, remote_nodes_enabled=False,
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
    def test_scan_command_accepts_config_over_stdin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            (root / "project" / "result.txt").write_text("ready")
            payload = base64.urlsafe_b64encode(json.dumps({
                "paths": ["project"],
                "excludes": [],
                "digest_files": False,
            }).encode()).decode()
            stdout = io.StringIO()

            with patch.object(sys, "stdin", io.StringIO(payload)), \
                    patch.object(sys, "stdout", stdout):
                result = sync_status.main([
                    "scan", "--root", str(root), "--config-stdin",
                ])

            self.assertEqual(result, 0)
            records = [json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertIn("project/result.txt", {
                record["path"] for record in records
            })

    def test_session_sync_api_previews_and_queues_derived_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            outputs = temp / "outputs"
            run_dir = outputs / "demo-run"
            run_dir.mkdir(parents=True)
            local = temp / "Projects"
            remote = temp / "remote"
            repo = local / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / "src").mkdir()
            remote.mkdir()
            (run_dir / "session.json").write_text(json.dumps({
                "name": "demo",
                "cwd": str(repo / "src"),
                "linked_folders": [],
            }))
            settings = sync_status.SyncSettings(
                enabled=True, local_root=str(local), remote_root=str(remote),
                paths=("repo",),
                state_db=str(temp / "state" / "sync.sqlite3"),
            )

            with patch.object(
                dashboard, "load_sync_settings", return_value=settings,
            ):
                app = dashboard.create_app(
                    outputs, ttyd_enabled=False, remote_nodes_enabled=False,
                )
            with patch.object(app.state.sync_status, "request_sync") as request_sync, \
                    patch.object(app.state.sync_status, "cancel_sync") as cancel_sync:
                with TestClient(app) as client:
                    preview = client.get("/api/sessions/demo-run::demo/sync-scope")
                    queued = client.post(
                        "/api/sessions/demo-run::demo/sync",
                        json={"mode": "when_idle"},
                    )
                    cancelled = client.post("/api/sync/cancel")

            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.json()["paths"], ["repo"])
            self.assertEqual(queued.status_code, 200)
            self.assertEqual(queued.json()["scope"], "session")
            self.assertEqual(cancelled.status_code, 200)
            cancel_sync.assert_called_once_with()
            request_sync.assert_called_once_with(
                "when_idle", paths=["repo"], scope_label="demo",
                scope_run_id="demo-run::demo",
            )

    def test_busy_sync_paths_use_each_sessions_concrete_project_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            outputs = temp / "outputs"
            outputs.mkdir()
            root = temp / "Projects"
            repo_a = root / "repo-a"
            repo_b = root / "repo-b"
            (repo_a / ".git").mkdir(parents=True)
            (repo_b / ".git").mkdir(parents=True)
            (repo_b / "src").mkdir()
            task_file = root / "current" / "topic" / "result.md"
            task_file.parent.mkdir(parents=True)
            task_file.write_text("result")
            settings = sync_status.SyncSettings(
                enabled=True, local_root=str(root), remote_root=str(temp / "remote"),
                paths=("repo-a",),
                state_db=str(temp / "state" / "sync.sqlite3"),
            )
            runs = [
                {"alive": True, "busy": True, "background_active": False,
                 "cwd": str(root), "linked_folders": []},
                {"alive": True, "busy": True, "background_active": False,
                 "cwd": str(root), "linked_folders": [
                     {"path": str(repo_a), "type": "folder"},
                     {"path": str(task_file), "type": "file"},
                 ]},
                {"alive": True, "busy": True, "background_active": False,
                 "cwd": str(repo_b / "src"), "linked_folders": []},
            ]

            with patch.object(
                dashboard, "load_sync_settings", return_value=settings,
            ):
                app = dashboard.create_app(
                    outputs, ttyd_enabled=False, remote_nodes_enabled=False,
                )
            with patch.object(dashboard, "_discover_runs", return_value=runs):
                paths = app.state.sync_status._busy_paths_provider()

            self.assertEqual(paths, [
                "current/topic/result.md", "repo-a", "repo-b",
            ])

    def test_session_sync_scope_uses_git_roots_and_linked_task_items(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Projects"
            repo = root / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / "src").mkdir()
            task = root / "current" / "topic"
            task.mkdir(parents=True)
            result = task / "result.md"
            result.write_text("result")
            ignored = root / "_to_delete" / "old"
            ignored.mkdir(parents=True)

            paths = dashboard._session_sync_paths({
                "cwd": str(repo / "src"),
                "linked_folders": [
                    {"path": str(result), "type": "file"},
                    {"path": str(repo / "src"), "type": "folder"},
                    {"path": str(ignored), "type": "folder"},
                    {"path": "https://example.com/task", "type": "url"},
                    {"path": str(root.parent / "outside"), "type": "folder"},
                ],
            }, root)

            self.assertEqual(paths, ["repo", "current/topic/result.md"])

    def test_session_sync_scope_ignores_projects_root_without_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Projects"
            root.mkdir()

            paths = dashboard._session_sync_paths({
                "cwd": str(root), "linked_folders": [],
            }, root)

            self.assertEqual(paths, [])

    def test_scoped_sync_rejects_workspace_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            local = temp / "local"
            remote = temp / "remote"
            local.mkdir()
            remote.mkdir()
            service = sync_status.SyncStatusService(sync_status.SyncSettings(
                enabled=True, local_root=str(local), remote_root=str(remote),
                paths=("tracked",),
                state_db=str(temp / "state" / "sync.sqlite3"),
            ))
            try:
                service.refresh_now()
                service.store.set_meta("baseline_initialized_at", "now")
                with self.assertRaisesRegex(ValueError, "at least one path"):
                    service.request_sync("now", paths=("",))
            finally:
                service.stop()

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

    def test_scoped_session_sync_publishes_completion_handoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            local = temp / "local"
            remote = temp / "remote"
            local.mkdir()
            remote.mkdir()
            (local / "project").mkdir()
            (remote / "project").mkdir()
            (local / "project" / "file.txt").write_text("same")
            (remote / "project" / "file.txt").write_text("same")
            seen = []

            def publish(job, cancel):
                seen.append((job["scope_run_id"], cancel.is_set()))
                return {"state": "ready", "remote_run_id": "handoff::demo"}

            service = sync_status.SyncStatusService(
                sync_status.SyncSettings(
                    enabled=True, local_root=str(local), remote_root=str(remote),
                    paths=("project",),
                    state_db=str(temp / "state" / "sync.sqlite3"),
                ),
                completion_callback=publish,
            )
            try:
                service.refresh_now()
                service.initialize_baseline("remote")
                service.request_sync(
                    "now", paths=("project",), scope_run_id="run::demo",
                )

                self.assertTrue(service._execute_sync_request(
                    "now", service._sync_paths,
                ))

                job = service.status()["sync_job"]
                self.assertEqual(job["state"], "complete")
                self.assertEqual(job["handoff"]["state"], "ready")
                self.assertEqual(seen, [("run::demo", False)])
            finally:
                service.stop()

    def test_session_handoff_creates_stopped_remote_resume_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            outputs = temp / "outputs"
            run_dir = outputs / "demo-run"
            run_dir.mkdir(parents=True)
            transcript = temp / "claude.jsonl"
            transcript.write_bytes(b'{"sessionId":"resume-123"}\n{"partial":')
            (run_dir / "session.json").write_text(json.dumps({
                "name": "demo",
                "label": "Demo task",
                "agent": "claude",
                "cwd": "/workspace/demo",
                "status": "running",
                "resume_agent": "claude",
                "resume_id": "resume-123",
                "resume_source_path": str(transcript),
                "linked_folders": [{
                    "path": "/workspace/demo", "label": "demo",
                    "type": "folder",
                }],
            }))
            remote_code = temp / "remote" / "agent-orchestrator"
            settings = sync_status.SyncSettings(
                enabled=True,
                local_root=str(temp / "Projects"),
                remote_root=str(temp / "RemoteProjects"),
                remote_code_root=str(remote_code),
            )

            result = dashboard._publish_session_handoff(
                outputs,
                settings,
                {
                    "scope_run_id": "demo-run::demo",
                    "scope_paths": ["repo"],
                },
                threading.Event(),
            )

            self.assertEqual(result["state"], "ready")
            remote_run = remote_code / "outputs" / "handoff-claude-resume-123"
            metadata = json.loads((remote_run / "session.json").read_text())
            self.assertEqual(metadata["status"], "handoff-ready")
            self.assertEqual(metadata["resume_id"], "resume-123")
            remote_transcript = Path(metadata["resume_source_path"])
            self.assertEqual(
                remote_transcript.read_bytes(),
                b'{"sessionId":"resume-123"}\n',
            )
            self.assertEqual(
                stat.S_IMODE(remote_transcript.stat().st_mode), 0o600,
            )

    def test_extracts_only_explicit_progress_counter(self):
        progress = dashboard._extract_terminal_progress("""
Starting documentation validation
[3/8] Check internal links
Worked for 2m 10s
""")
        self.assertEqual(progress["current"], 3)
        self.assertEqual(progress["total"], 8)
        self.assertEqual(progress["percent"], 37.5)
        self.assertEqual(progress["headline"], "Check internal links")
        self.assertEqual(progress["source"], "explicit-counter")

    def test_marks_blocked_goal_and_conservative_input_prompt(self):
        progress = dashboard._extract_terminal_progress("""
Compilation completed
Goal blocked: access approval required
Please approve the remote login
""")
        self.assertEqual(progress["goal_state"], "blocked")
        self.assertTrue(progress["needs_input"])
        payload = dashboard._mission_control_payload({
            "started_at": "2026-08-09T08:00:00",
            "tmux_session": "orch-demo",
            "panel_state": "p0",
        }, {"busy": False}, progress)
        self.assertEqual(payload["state"], "blocked")
        self.assertTrue(payload["needs_attention"])

    def test_goal_achievement_is_not_session_completion(self):
        progress = dashboard._extract_terminal_progress(
            "Goal achieved (2h 15m)"
        )
        waiting = dashboard._mission_control_payload({
            "started_at": "2026-08-09T08:00:00",
            "tmux_session": "orch-demo",
            "panel_state": "p0",
        }, {"busy": False}, progress)
        done = dashboard._mission_control_payload({
            "started_at": "2026-08-09T08:00:00",
            "tmux_session": "orch-demo",
            "panel_state": "done",
        }, {"busy": False}, progress)
        self.assertEqual(progress["goal_state"], "achieved")
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(done["state"], "completed")

    def test_goal_active_is_working_without_terminal_output(self):
        progress = dashboard._extract_terminal_progress(
            "manual mode on · 2 monitors\n/goal active (18h)"
        )
        payload = dashboard._mission_control_payload({
            "started_at": "2026-09-02T08:00:00",
            "tmux_session": "orch-goal-active",
            "panel_state": "p1",
        }, {"busy": False}, progress)
        self.assertEqual(progress["goal_state"], "pursuing")
        self.assertEqual(payload["state"], "working")
        self.assertFalse(payload["needs_attention"])

    def test_detects_only_current_claude_input_prompt(self):
        ready = dashboard._detect_claude_prompt_ready("""
✶ Considering… (1m 43s · thinking)
────────────────────────────────────
❯
────────────────────────────────────
⏸ manual mode on · ? for shortcuts · ← 1 agent
""")
        running = dashboard._detect_claude_prompt_ready("""
❯ an earlier user request
✶ Considering… (4s · esc to interrupt)
""")
        self.assertTrue(ready)
        self.assertFalse(running)

    def test_timeline_v1_completed_segments_migrate_to_waiting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            outputs = Path(temp_dir) / "outputs"
            outputs.mkdir()
            (outputs / ".activity_timeline.json").write_text(json.dumps({
                "version": 1,
                "updated_at": 1000.0,
                "sessions": {
                    "orch-demo": {
                        "alive": True,
                        "run_id": "demo::task",
                        "tmux_session": "orch-demo",
                        "display_name": "Demo",
                        "current_state": "completed",
                        "last_seen_at": 1020.0,
                        "segments": [{
                            "state": "completed", "start": 1000.0, "end": 1020.0,
                        }],
                    },
                },
            }))
            payload = dashboard._activity_timeline_payload(
                outputs, hours=1, now=1020.0,
            )
            row = payload["sessions"][0]
            self.assertEqual(payload["version"], 2)
            self.assertEqual(row["current_state"], "waiting")
            self.assertEqual(row["segments"][0]["state"], "waiting")

    def test_activity_timeline_can_include_recently_ended_sessions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            outputs = Path(temp_dir) / "outputs"
            outputs.mkdir()
            run = {
                "alive": True,
                "tmux_session": "orch-ended-demo",
                "run_id": "ended-demo::task",
                "display_name": "Ended demo",
                "agent": "codex",
                "panel_state": "flag",
                "mission_control": {
                    "state": "waiting", "priority": "",
                    "needs_attention": False,
                },
            }
            dashboard._record_activity_timeline_snapshot(
                outputs, [run], now=1000.0, force=True,
            )
            dashboard._record_activity_timeline_snapshot(
                outputs, [], now=1010.0, force=True,
            )

            active_only = dashboard._activity_timeline_payload(
                outputs, hours=1, now=1010.0,
            )
            with_ended = dashboard._activity_timeline_payload(
                outputs, hours=1, now=1010.0, include_ended=True,
            )

            self.assertEqual(active_only["sessions"], [])
            self.assertEqual(len(with_ended["sessions"]), 1)
            row = with_ended["sessions"][0]
            self.assertFalse(row["alive"])
            self.assertEqual(row["panel_state"], "flag")

    def test_activity_timeline_persists_intervals_and_marks_gaps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            outputs = Path(temp_dir) / "outputs"
            outputs.mkdir()
            run = {
                "alive": True,
                "tmux_session": "orch-demo",
                "run_id": "demo::task",
                "display_name": "Demo task",
                "agent": "codex",
                "background_active": False,
                "mission_control": {
                    "state": "waiting", "priority": "p0",
                },
            }
            dashboard._record_activity_timeline_snapshot(
                outputs, [run], now=1000.0, force=True,
            )
            run["mission_control"]["state"] = "working"
            dashboard._record_activity_timeline_snapshot(
                outputs, [run], now=1010.0, force=True,
            )
            dashboard._record_activity_timeline_snapshot(
                outputs, [run], now=1020.0, force=True,
            )

            payload = dashboard._activity_timeline_payload(
                outputs, hours=1, now=1020.0,
            )
            row = payload["sessions"][0]
            self.assertEqual(row["current_state"], "working")
            self.assertEqual(row["working_s"], 15.0)
            self.assertEqual(row["idle_s"], 5.0)
            self.assertEqual(row["current_streak_s"], 15.0)
            self.assertEqual(row["utilization"], 75.0)

            dashboard._record_activity_timeline_snapshot(
                outputs, [run], now=1060.0, force=True,
            )
            dashboard._record_activity_timeline_snapshot(
                outputs, [run], now=1070.0, force=True,
            )
            payload = dashboard._activity_timeline_payload(
                outputs, hours=1, now=1070.0,
            )
            row = payload["sessions"][0]
            self.assertGreaterEqual(row["unknown_s"], 40.0)
            self.assertEqual(row["current_streak_s"], 10.0)
            persisted = (outputs / ".activity_timeline.json").read_text()
            self.assertNotIn("terminal", persisted.lower())

    def test_activity_timeline_preserves_daily_busy_rollups(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            outputs = Path(temp_dir) / "outputs"
            outputs.mkdir()
            start = time.mktime((2026, 8, 18, 10, 0, 0, 0, 0, -1))
            run = {
                "alive": True,
                "tmux_session": "orch-daily-demo",
                "run_id": "daily-demo::task",
                "display_name": "Daily demo",
                "agent": "codex",
                "background_active": True,
                "background_active_started_ts": start - 3600,
                "mission_control": {"state": "working", "priority": "p0"},
            }
            dashboard._record_activity_timeline_snapshot(
                outputs, [run], now=start, force=True,
            )
            dashboard._record_activity_timeline_snapshot(
                outputs, [], now=start + 60, force=True,
            )

            with dashboard._ACTIVITY_TIMELINE_LOCK:
                data = dashboard._load_activity_timeline_unlocked(outputs)
                entry = data["sessions"]["orch-daily-demo"]
                daily = dict(entry["daily_busy"])
            self.assertAlmostEqual(daily["2026-08-18"], 3600.0, delta=1.0)

    def test_mission_analytics_aggregates_range_and_dedupes_transcripts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            outputs = Path(temp_dir) / "outputs"
            outputs.mkdir()
            (outputs / ".activity_timeline.json").write_text(json.dumps({
                "version": 2,
                "updated_at": 0,
                "sessions": {
                    "orch-one": {
                        "run_id": "run-one", "tmux_session": "orch-one",
                        "agent": "codex", "alive": True,
                        "daily_busy": {
                            "2026-08-18": 3600, "2026-08-19": 7200,
                        },
                        "segments": [],
                    },
                    "orch-two": {
                        "run_id": "run-two", "tmux_session": "orch-two",
                        "agent": "codex", "alive": True,
                        "daily_busy": {"2026-08-19": 1800},
                        "segments": [],
                    },
                },
            }))

            def attach(_outputs, payload):
                for row in payload["sessions"]:
                    row["conversation_metrics"] = {
                        "available": True,
                        "source_id": "shared-transcript",
                        "window": {"daily": [
                            {
                                "date": "2026-08-18", "requests": 2,
                                "request_tokens": 11, "generated_tokens": 7,
                                "token_usage_available": True,
                            },
                            {
                                "date": "2026-08-19", "requests": 1,
                                "request_tokens": 5, "generated_tokens": 3,
                                "token_usage_available": True,
                            },
                        ]},
                    }
                return payload

            with patch.object(dashboard, "_attach_conversation_metrics", attach):
                result = dashboard._mission_analytics_payload(
                    outputs,
                    start_day="2026-08-18",
                    end_day="2026-08-19",
                    run_ids={"run-one", "run-two"},
                    now=time.mktime((2026, 8, 19, 12, 0, 0, 0, 0, -1)),
                )

            self.assertEqual(result["session_count"], 2)
            self.assertEqual(result["source_count"], 1)
            self.assertEqual(result["totals"]["busy_s"], 12600.0)
            self.assertEqual(result["totals"]["requests"], 3)
            self.assertEqual(result["totals"]["request_tokens"], 16)
            self.assertEqual(result["totals"]["generated_tokens"], 10)

    def test_scoped_sync_ignores_unrelated_workspace_conflict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            local = temp / "local"
            remote = temp / "remote"
            for root in (local, remote):
                (root / "project").mkdir(parents=True)
                (root / "other").mkdir(parents=True)
                (root / "project" / "code.py").write_text("base")
                (root / "other" / "state.json").write_text("base")
            service = sync_status.SyncStatusService(sync_status.SyncSettings(
                enabled=True, local_root=str(local), remote_root=str(remote),
                paths=("project", "other"),
                state_db=str(temp / "state" / "sync.sqlite3"),
            ))
            try:
                service.refresh_now()
                service.initialize_baseline("remote")
                (local / "project" / "code.py").write_text("local update")
                (local / "other" / "state.json").write_text("local conflict")
                (remote / "other" / "state.json").write_text("remote conflict")
                service.refresh_now()
                service.request_sync(
                    "now", paths=("project",), scope_label="Project session",
                )

                self.assertTrue(service._execute_sync_request(
                    "now", service._sync_paths,
                ))

                self.assertEqual(
                    (remote / "project" / "code.py").read_text(),
                    "local update",
                )
                self.assertEqual(
                    (remote / "other" / "state.json").read_text(),
                    "remote conflict",
                )
                status = service.status()
                self.assertEqual(status["sync_job"]["state"], "complete")
                self.assertEqual(status["sync_job"]["scope"], "paths")
                self.assertEqual(status["sync_job"]["scope_paths"], ["project"])
                self.assertEqual(status["sync_job"]["scope_label"], "Project session")
                self.assertEqual(status["counts"]["conflict"], 1)
            finally:
                service.stop()

    def test_waiting_scoped_sync_can_be_cancelled_without_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            local = temp / "local"
            remote = temp / "remote"
            for root in (local, remote):
                (root / "project").mkdir(parents=True)
                (root / "project" / "code.py").write_text("base")
            service = sync_status.SyncStatusService(
                sync_status.SyncSettings(
                    enabled=True, local_root=str(local), remote_root=str(remote),
                    paths=("project",),
                    state_db=str(temp / "state" / "sync.sqlite3"),
                ),
                busy_paths_provider=lambda: ("project",),
            )
            try:
                service.refresh_now()
                service.initialize_baseline("remote")
                (local / "project" / "code.py").write_text("local update")
                service.request_sync(
                    "when_idle", paths=("project",),
                    scope_label="Project", scope_run_id="run::project",
                )

                self.assertFalse(service._execute_sync_request(
                    "when_idle", service._sync_paths,
                ))
                self.assertEqual(service.status()["sync_job"]["state"], "waiting_idle")

                service.cancel_sync()
                self.assertTrue(service._execute_sync_request(
                    "when_idle", service._sync_paths,
                ))

                job = service.status()["sync_job"]
                self.assertEqual(job["state"], "cancelled")
                self.assertEqual(job["scope_run_id"], "run::project")
                self.assertEqual(
                    (remote / "project" / "code.py").read_text(), "base",
                )
            finally:
                service.stop()

    def test_scoped_sync_tracks_a_path_outside_global_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            local = temp / "local"
            remote = temp / "remote"
            for root in (local, remote):
                (root / "tracked").mkdir(parents=True)
                (root / "tracked" / "base.txt").write_text("base")
            service = sync_status.SyncStatusService(sync_status.SyncSettings(
                enabled=True, local_root=str(local), remote_root=str(remote),
                paths=("tracked",),
                state_db=str(temp / "state" / "sync.sqlite3"),
            ))
            try:
                service.refresh_now()
                service.initialize_baseline("remote")
                (local / "session-project").mkdir()
                scoped_file = local / "session-project" / "result.txt"
                scoped_file.write_text("first")

                for expected in ("first", "second"):
                    scoped_file.write_text(expected)
                    service.request_sync("now", paths=("session-project",))
                    self.assertTrue(service._execute_sync_request(
                        "now", service._sync_paths,
                    ))
                    self.assertEqual(
                        (remote / "session-project" / "result.txt").read_text(),
                        expected,
                    )

                status = service.status()
                self.assertEqual(status["files"], {
                    "local": 1, "remote": 1, "baseline": 1,
                })
                self.assertEqual(status["counts"]["unchanged"], 1)
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
    def test_cancel_terminates_an_inflight_rsync(self):
        started = threading.Event()
        terminated = threading.Event()

        class BlockingProcess:
            returncode = None

            def communicate(self, timeout=None):
                started.set()
                terminated.wait(timeout=2)
                self.returncode = -15
                return "", "terminated"

            def poll(self):
                return self.returncode

            def terminate(self):
                terminated.set()

            def kill(self):
                terminated.set()

        proc = BlockingProcess()
        transfer = sync_transfer.WorkspaceTransfer(
            local_root=Path("/local/Projects"), remote_root="/remote/Projects",
            remote_host="", excludes=(), timeout_seconds=30,
        )
        errors = []

        def execute():
            try:
                transfer.execute(sync_transfer.TransferPlan(
                    push=["project/file.txt"],
                ))
            except Exception as exc:
                errors.append(exc)

        with patch.object(sync_transfer.subprocess, "Popen", return_value=proc):
            thread = threading.Thread(target=execute)
            thread.start()
            self.assertTrue(started.wait(timeout=1))
            transfer.cancel()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(terminated.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], sync_transfer.TransferCancelled)

    def test_remote_busy_paths_accept_resolved_root_alias(self):
        transfer = sync_transfer.WorkspaceTransfer(
            local_root=Path("/local/Projects"),
            remote_root="/Users/demo/Documents/Projects",
            remote_host="workdesk", excludes=(),
        )
        proc = Mock(returncode=0)
        proc.communicate.return_value = (
            "__ORCH_ROOT__\t/home/demo/Projects\n"
            "orch-root\t/home/demo/Projects\t0\t\n"
            "orch-task\t/home/demo/Projects/repo/src\t0\t/home/demo/Projects/repo\n"
            "orch-task-web\t/home/demo/Projects/repo\t0\t/home/demo/Projects/repo\n"
            "other\t/home/demo/Projects/ignored\t0\t\n",
            "",
        )

        with patch.object(sync_transfer.subprocess, "Popen", return_value=proc) as popen:
            paths = transfer.remote_active_paths()

        self.assertEqual(paths, ["repo"])
        self.assertIn("realpath --", popen.call_args.args[0][-1])

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
