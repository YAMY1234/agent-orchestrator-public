import json
import tempfile
import time
import unittest
from pathlib import Path

from agent_orchestrator.native_activity import (
    NativeActivityService,
    NativeActivityStore,
    claude_permission_decision,
    handle_claude_hook,
    install_claude_hooks,
    record_agent_event,
)


def wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    return None


class NativeActivityStoreTests(unittest.TestCase):
    def test_claude_hooks_map_work_and_waiting_transitions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = NativeActivityStore(Path(temp_dir) / "activity.sqlite3")
            working = record_agent_event("claude", {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "ABC-123",
                "timestamp": "2026-08-28T20:00:00Z",
            }, store=store)
            waiting = record_agent_event("claude", {
                "hook_event_name": "Stop",
                "session_id": "ABC-123",
                "timestamp": "2026-08-28T20:00:05Z",
            }, store=store)

            self.assertEqual(working["state"], "working")
            self.assertEqual(waiting["state"], "waiting_user")
            row = store.read_all()[0]
            self.assertEqual(row["agent"], "claude")
            self.assertEqual(row["session_id"], "abc-123")
            self.assertEqual(row["event"], "Stop")

    def test_permission_notification_requires_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = NativeActivityStore(Path(temp_dir) / "activity.sqlite3")
            state = record_agent_event("claude", {
                "hook_event_name": "Notification",
                "notification_type": "permission_prompt",
                "session_id": "session",
            }, store=store)
            self.assertEqual(state["state"], "needs_input")


class ClaudeHookInstallerTests(unittest.TestCase):
    def test_installer_preserves_existing_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"
            settings.write_text(json.dumps({
                "model": "opus",
                "hooks": {
                    "Stop": [{
                        "hooks": [{"type": "command", "command": "existing-hook"}],
                    }],
                },
            }))

            _, changed = install_claude_hooks(
                settings, orch_path="/example/orch",
            )
            _, changed_again = install_claude_hooks(
                settings, orch_path="/example/orch",
            )

            saved = json.loads(settings.read_text())
            self.assertTrue(changed)
            self.assertFalse(changed_again)
            self.assertEqual(saved["model"], "opus")
            stop_commands = [
                hook["command"]
                for group in saved["hooks"]["Stop"]
                for hook in group["hooks"]
            ]
            self.assertEqual(stop_commands, [
                "existing-hook",
                "/example/orch agent-event --agent claude",
            ])
            self.assertIn("Notification", saved["hooks"])
            self.assertEqual(
                saved["hooks"]["Notification"][0]["matcher"],
                "permission_prompt|idle_prompt",
            )

    def test_installer_opts_permission_hook_into_orchestrator_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "settings.json"
            install_claude_hooks(settings, orch_path="/example/orch")

            _, changed = install_claude_hooks(
                settings,
                orch_path="/example/orch",
                permission_policy="orchestrator",
            )
            _, changed_again = install_claude_hooks(
                settings,
                orch_path="/example/orch",
                permission_policy="orchestrator",
            )

            saved = json.loads(settings.read_text())
            permission_commands = [
                hook["command"]
                for group in saved["hooks"]["PermissionRequest"]
                for hook in group["hooks"]
            ]
            stop_commands = [
                hook["command"]
                for group in saved["hooks"]["Stop"]
                for hook in group["hooks"]
            ]
            self.assertTrue(changed)
            self.assertFalse(changed_again)
            self.assertEqual(permission_commands, [
                "/example/orch agent-event --agent claude "
                "--permission-policy orchestrator",
            ])
            self.assertEqual(stop_commands, [
                "/example/orch agent-event --agent claude",
            ])


class ClaudePermissionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "hook_event_name": "PermissionRequest",
            "session_id": "session-id",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
        }
        self.orch_env = {
            "ORCH_RUN_ID": "run::task",
            "ORCH_TMUX_SESSION": "orch-task-123",
            "ORCH_AGENT_TYPE": "claude",
        }

    def test_policy_only_approves_orchestrator_claude_sessions(self):
        self.assertIsNone(claude_permission_decision(
            self.payload, policy="observe", environ=self.orch_env,
        ))
        self.assertIsNone(claude_permission_decision(
            self.payload, policy="orchestrator", environ={},
        ))
        self.assertIsNone(claude_permission_decision(
            self.payload,
            policy="orchestrator",
            environ={**self.orch_env, "ORCH_AGENT_TYPE": "codex"},
        ))
        decision = claude_permission_decision(
            self.payload, policy="orchestrator", environ=self.orch_env,
        )
        self.assertEqual(
            decision["hookSpecificOutput"]["decision"]["behavior"],
            "allow",
        )

    def test_auto_approved_permission_remains_working(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = NativeActivityStore(Path(temp_dir) / "activity.sqlite3")
            decision = handle_claude_hook(
                self.payload,
                permission_policy="orchestrator",
                store=store,
                environ=self.orch_env,
            )
            self.assertIsNotNone(decision)
            row = store.read_all()[0]
            self.assertEqual(row["state"], "working")
            self.assertEqual(row["reason"], "Permission auto-approved")

    def test_forked_session_event_updates_orchestrator_resume_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = NativeActivityStore(root / "activity.sqlite3")
            session_path = root / "session.json"
            session_path.write_text(json.dumps({
                "run_id": "run::task",
                "resume": {"agent": "claude", "id": "original-session"},
            }))
            handle_claude_hook(
                {
                    "hook_event_name": "Stop",
                    "session_id": "forked-session",
                    "timestamp": "2026-09-04T18:00:00Z",
                },
                store=store,
                environ={
                    **self.orch_env,
                    "ORCH_SESSION_JSON": str(session_path),
                },
            )

            rows = {
                row["session_id"]: row for row in store.read_all()
            }
            self.assertEqual(
                set(rows), {"forked-session", "original-session"},
            )
            self.assertEqual(rows["forked-session"]["state"], "waiting_user")
            self.assertEqual(rows["original-session"]["state"], "waiting_user")
            self.assertEqual(rows["original-session"]["event"], "Stop")

    def test_telemetry_failure_does_not_block_native_approval(self):
        class FailingStore:
            def record(self, **_kwargs):
                raise OSError("read-only state directory")

        decision = handle_claude_hook(
            self.payload,
            permission_policy="orchestrator",
            store=FailingStore(),
            environ=self.orch_env,
        )
        self.assertEqual(
            decision["hookSpecificOutput"]["decision"]["behavior"],
            "allow",
        )


class CodexNativeActivityTests(unittest.TestCase):
    def test_claude_prompt_overrides_stale_working_hook(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = NativeActivityService(
                store=NativeActivityStore(root / "activity.sqlite3"),
            )
            row = {
                "run_id": "run::claude-ready",
                "agent": "claude",
                "resume_id": "session-id",
                "alive": True,
                "busy": False,
                "activity_sustained_active": False,
                "background_active": False,
                "terminal_prompt_ready": True,
                "activity_last_change_age_s": 600.0,
                "panel_state": "p0",
                "mission_control": {
                    "state": "working",
                    "priority": "p0",
                    "activity_mode": "busy",
                    "needs_attention": False,
                    "attention_reason": "",
                    "progress": {"goal_state": ""},
                },
            }
            service.register_runs([row])
            service._set_state(
                ("claude", "session-id"),
                "working",
                source="claude-hook",
                event="UserPromptSubmit",
                reason="Prompt submitted",
                at=time.time() - 900.0,
            )

            rendered = service.apply([dict(row)])[0]

            self.assertFalse(rendered["busy"])
            self.assertEqual(rendered["native_activity"]["state"], "waiting_user")
            self.assertEqual(
                rendered["native_activity"]["source"], "terminal-prompt"
            )
            self.assertEqual(rendered["mission_control"]["state"], "waiting")
            self.assertTrue(rendered["mission_control"]["needs_attention"])

    def test_claude_goal_keeps_working_at_interactive_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = NativeActivityService(
                store=NativeActivityStore(root / "activity.sqlite3"),
            )
            row = {
                "run_id": "run::claude-goal",
                "agent": "claude",
                "resume_id": "session-id",
                "alive": True,
                "busy": False,
                "activity_sustained_active": False,
                "background_active": False,
                "terminal_prompt_ready": True,
                "mission_control": {
                    "state": "working",
                    "activity_mode": "busy",
                    "progress": {"goal_state": "pursuing"},
                },
            }
            service.register_runs([row])
            service._set_state(
                ("claude", "session-id"),
                "working",
                source="claude-hook",
                event="UserPromptSubmit",
            )

            rendered = service.apply([dict(row)])[0]

            self.assertTrue(rendered["busy"])
            self.assertEqual(rendered["native_activity"]["state"], "working")
            self.assertEqual(rendered["mission_control"]["state"], "working")

    def test_claude_goal_active_overrides_stale_waiting_hook(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = NativeActivityService(
                store=NativeActivityStore(root / "activity.sqlite3"),
            )
            row = {
                "run_id": "run::goal-active",
                "agent": "claude",
                "resume_id": "session-id",
                "alive": True,
                "busy": False,
                "panel_state": "p1",
                "mission_control": {
                    "state": "working",
                    "priority": "p1",
                    "activity_mode": "busy",
                    "needs_attention": False,
                    "attention_reason": "",
                    "progress": {"goal_state": "pursuing"},
                },
            }
            service.register_runs([row])
            service._set_state(
                ("claude", "session-id"),
                "waiting_user",
                source="claude-hook",
                reason="Waiting for your input",
                at=time.time() - 3600.0,
            )

            rendered = service.apply([dict(row)])[0]

            self.assertTrue(rendered["busy"])
            self.assertEqual(rendered["mission_control"]["state"], "working")
            self.assertEqual(
                rendered["mission_control"]["activity_source"], "goal-active"
            )
            self.assertFalse(rendered["mission_control"]["needs_attention"])

    def test_overdue_p0_waiting_state_keeps_mission_attention(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = NativeActivityService(
                store=NativeActivityStore(root / "activity.sqlite3"),
            )
            row = {
                "run_id": "run::waiting-p0",
                "agent": "codex",
                "resume_id": "session-id",
                "alive": True,
                "busy": False,
                "panel_state": "p0",
                "mission_control": {
                    "state": "waiting",
                    "priority": "p0",
                    "activity_mode": "idle",
                    "needs_attention": False,
                    "attention_reason": "",
                },
            }
            service.register_runs([row])
            service._set_state(
                ("codex", "session-id"),
                "waiting_user",
                source="codex-transcript",
                reason="Turn complete",
                at=time.time() - 360.0,
            )

            rendered = service.apply([dict(row)])[0]

            self.assertFalse(rendered["busy"])
            self.assertEqual(rendered["mission_control"]["state"], "waiting")
            self.assertTrue(rendered["mission_control"]["needs_attention"])
            self.assertEqual(
                rendered["mission_control"]["attention_reason"],
                "P0 has been idle",
            )

    def test_overdue_lead_waiting_state_keeps_mission_attention(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = NativeActivityService(
                store=NativeActivityStore(root / "activity.sqlite3"),
            )
            row = {
                "run_id": "run::waiting-lead",
                "agent": "codex",
                "resume_id": "session-id",
                "alive": True,
                "busy": False,
                "panel_state": "lead",
                "mission_control": {
                    "state": "waiting",
                    "priority": "lead",
                    "activity_mode": "idle",
                    "needs_attention": False,
                    "attention_reason": "",
                },
            }
            service.register_runs([row])
            service._set_state(
                ("codex", "session-id"),
                "waiting_user",
                source="codex-transcript",
                reason="Turn complete",
                at=time.time() - 360.0,
            )

            rendered = service.apply([dict(row)])[0]

            self.assertFalse(rendered["busy"])
            self.assertEqual(rendered["mission_control"]["state"], "waiting")
            self.assertTrue(rendered["mission_control"]["needs_attention"])
            self.assertEqual(
                rendered["mission_control"]["attention_reason"],
                "LEAD has been idle",
            )

    def test_background_work_stays_active_when_native_hook_is_waiting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = NativeActivityService(
                store=NativeActivityStore(root / "activity.sqlite3"),
            )
            row = {
                "run_id": "run::background",
                "agent": "codex",
                "resume_id": "session-id",
                "alive": True,
                "busy": True,
                "background_active": True,
                "background_active_age_s": 12.0,
                "background_active_started_ts": time.time() - 12.0,
                "mission_control": {
                    "state": "needs_input",
                    "activity_mode": "idle",
                    "needs_attention": True,
                    "attention_reason": "stale hook",
                },
            }
            service.register_runs([row])
            service._set_state(
                ("codex", "session-id"),
                "waiting_user",
                source="codex-transcript",
                reason="Turn complete",
            )

            rendered = service.apply([dict(row)])[0]

            self.assertTrue(rendered["busy"])
            self.assertEqual(rendered["native_activity"]["state"], "waiting_user")
            self.assertEqual(rendered["mission_control"]["state"], "working")
            self.assertEqual(
                rendered["mission_control"]["activity_source"],
                "terminal-background",
            )
            self.assertFalse(rendered["mission_control"]["needs_attention"])
            self.assertEqual(rendered["mission_control"]["attention_reason"], "")

    def test_sustained_terminal_output_overrides_stale_waiting_hook(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = NativeActivityService(
                store=NativeActivityStore(root / "activity.sqlite3"),
            )
            row = {
                "run_id": "run::streaming",
                "agent": "codex",
                "resume_id": "session-id",
                "alive": True,
                "busy": True,
                "activity_sustained_active": True,
                "activity_streak_age_s": 8.0,
                "mission_control": {
                    "state": "waiting",
                    "activity_mode": "idle",
                    "needs_attention": True,
                },
            }
            service.register_runs([row])
            service._set_state(
                ("codex", "session-id"),
                "waiting_user",
                source="codex-transcript",
                reason="Turn complete",
            )

            rendered = service.apply([dict(row)])[0]

            self.assertTrue(rendered["busy"])
            self.assertEqual(rendered["mission_control"]["state"], "working")
            self.assertEqual(
                rendered["mission_control"]["activity_source"],
                "terminal-output",
            )
            self.assertFalse(rendered["mission_control"]["needs_attention"])

    def test_repeated_resume_only_updates_live_canonical_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            transcript = root / "rollout-shared-session.jsonl"
            transcript.write_text(json.dumps({
                "timestamp": "2026-08-28T20:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started"},
            }) + "\n")
            service = NativeActivityService(
                store=NativeActivityStore(root / "activity.sqlite3"),
                poll_interval_s=0.1,
            )
            old = {
                "run_id": "old::task",
                "agent": "codex",
                "resume_id": "shared-session",
                "alive": False,
                "started_at": "2026-08-01T10:00:00Z",
                "_native_transcript_path": str(transcript),
            }
            current = {
                "run_id": "current::task",
                "agent": "codex",
                "resume_id": "shared-session",
                "alive": True,
                "started_at": "2026-08-28T10:00:00Z",
                "_native_transcript_path": str(transcript),
            }
            service.register_runs([old, current])
            service.start()
            try:
                snapshot = wait_for(lambda: service.snapshot())
                self.assertEqual([row["run_id"] for row in snapshot], ["current::task"])
                rendered = service.apply([dict(old), dict(current)])
                self.assertNotIn("native_activity", rendered[0])
                self.assertEqual(
                    rendered[1]["native_activity"]["state"], "working",
                )
            finally:
                service.stop()

    def test_transcript_boundaries_override_screen_idle_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            transcript = root / "rollout-session-id.jsonl"
            transcript.write_text(json.dumps({
                "timestamp": "2026-08-28T20:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started"},
            }) + "\n")
            service = NativeActivityService(
                store=NativeActivityStore(root / "activity.sqlite3"),
                poll_interval_s=0.1,
            )
            row = {
                "run_id": "run::task",
                "agent": "codex",
                "resume_id": "session-id",
                "alive": True,
                "busy": False,
                "_native_transcript_path": str(transcript),
                "mission_control": {"state": "waiting", "activity_mode": "idle"},
            }
            service.register_runs([row])
            service.start()
            try:
                working = wait_for(lambda: service.snapshot())
                self.assertEqual(
                    working[0]["native_activity"]["state"], "working",
                )

                transcript.write_text(transcript.read_text() + json.dumps({
                    "timestamp": "2026-08-28T20:00:05Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete"},
                }) + "\n")
                waiting = wait_for(lambda: (
                    service.snapshot()
                    if service.snapshot()
                    and service.snapshot()[0]["native_activity"]["state"] == "waiting_user"
                    else None
                ))
                self.assertIsNotNone(waiting)

                rendered = service.apply([dict(row)])[0]
                self.assertFalse(rendered["busy"])
                self.assertEqual(rendered["mission_control"]["state"], "waiting")
                self.assertEqual(
                    rendered["native_activity"]["source"], "codex-transcript",
                )
            finally:
                service.stop()


if __name__ == "__main__":
    unittest.main()
