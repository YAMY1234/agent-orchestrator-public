import json
import tempfile
import unittest
from pathlib import Path

from agent_orchestrator.conversation_metrics import TranscriptMetricsCache


def _append(path: Path, *records: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


class ConversationMetricsTests(unittest.TestCase):
    def test_ready_probe_never_waits_for_an_active_indexer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "busy.jsonl"
            path.write_text("")
            cache = TranscriptMetricsCache()
            state = cache._state(path, "codex")
            state.lock.acquire()
            try:
                self.assertFalse(cache.is_ready(path, "codex"))
            finally:
                state.lock.release()

    def test_persistent_index_restores_offset_and_builds_daily_buckets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "codex.jsonl"
            cache_dir = root / "cache"
            _append(
                path,
                {
                    "timestamp": "2026-08-09T10:01:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "user", "id": "u1",
                        "content": [{"type": "input_text", "text": "hello"}],
                    },
                },
                {
                    "timestamp": "2026-08-09T10:01:10Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {
                            "input_tokens": 6, "output_tokens": 4,
                            "total_tokens": 10,
                        }},
                    },
                },
            )
            first = TranscriptMetricsCache(cache_dir=cache_dir)
            first_stats = first.snapshot(
                path, "codex", window_start=0, window_end=2_000_000_000,
            )
            first_size = path.stat().st_size
            self.assertEqual(first_stats["window"]["daily"][0]["requests"], 1)
            self.assertEqual(
                first_stats["window"]["daily"][0]["generated_tokens"], 4,
            )
            cache_files = list(cache_dir.glob("*.json"))
            self.assertEqual(len(cache_files), 1)
            self.assertNotIn("hello", cache_files[0].read_text())

            restored = TranscriptMetricsCache(cache_dir=cache_dir)
            self.assertTrue(restored.is_ready(path, "codex"))
            self.assertEqual(restored._state(path, "codex").offset, first_size)
            _append(path, {
                "timestamp": "2026-08-10T10:02:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message", "role": "user", "id": "u2",
                    "content": [{"type": "input_text", "text": "second"}],
                },
            })
            stats = restored.snapshot(
                path, "codex", window_start=0, window_end=2_000_000_000,
            )
            self.assertEqual(stats["window"]["requests"], 2)
            self.assertEqual(
                [row["date"] for row in stats["window"]["daily"]],
                ["2026-08-09", "2026-08-10"],
            )

    def test_codex_filters_noise_and_uses_cumulative_token_deltas(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "codex.jsonl"
            _append(
                path,
                {
                    "timestamp": "2026-08-09T10:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "# AGENTS.md noise"}],
                    },
                },
                {
                    "timestamp": "2026-08-09T10:01:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "user", "id": "u1",
                        "content": [{"type": "input_text", "text": "hello"}],
                    },
                },
                {
                    "timestamp": "2026-08-09T10:01:10Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {
                            "input_tokens": 6, "output_tokens": 4,
                            "total_tokens": 10,
                        }},
                    },
                },
                {
                    "timestamp": "2026-08-09T10:01:20Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {
                            "input_tokens": 6, "output_tokens": 4,
                            "total_tokens": 10,
                        }},
                    },
                },
                {
                    "timestamp": "2026-08-09T10:01:30Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {
                            "input_tokens": 15, "output_tokens": 10,
                            "total_tokens": 25,
                        }},
                    },
                },
            )
            cache = TranscriptMetricsCache()
            stats = cache.snapshot(
                path, "codex", window_start=0, window_end=2_000_000_000,
            )
            self.assertEqual(stats["window"]["requests"], 1)
            self.assertEqual(stats["window"]["characters"], 5)
            self.assertEqual(stats["window"]["request_tokens"], 2)
            self.assertEqual(stats["window"]["tokens"], 25)
            self.assertEqual(stats["window"]["generated_tokens"], 10)

            _append(path, {
                "timestamp": "2026-08-09T10:02:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message", "role": "user", "id": "u2",
                    "content": [{"type": "input_text", "text": "second"}],
                },
            })
            stats = cache.snapshot(
                path, "codex", window_start=0, window_end=2_000_000_000,
            )
            self.assertEqual(stats["conversation"]["requests"], 2)
            self.assertEqual(stats["conversation"]["characters"], 11)
            self.assertEqual(stats["conversation"]["request_tokens"], 4)

    def test_claude_excludes_tool_and_notification_records_and_dedupes_usage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "claude.jsonl"
            usage = {
                "input_tokens": 3,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 7,
                "output_tokens": 11,
            }
            _append(
                path,
                {
                    "timestamp": "2026-08-09T10:00:00Z", "type": "user",
                    "uuid": "u1",
                    "message": {"role": "user", "content": "hello"},
                },
                {
                    "timestamp": "2026-08-09T10:00:01Z", "type": "user",
                    "uuid": "tool",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "content": "large output"}],
                    },
                },
                {
                    "timestamp": "2026-08-09T10:00:02Z", "type": "user",
                    "uuid": "notification",
                    "message": {
                        "role": "user", "content": "<task-notification>done</task-notification>",
                    },
                },
                {
                    "timestamp": "2026-08-09T10:00:03Z", "type": "assistant",
                    "message": {"id": "a1", "usage": usage},
                },
                {
                    "timestamp": "2026-08-09T10:00:04Z", "type": "assistant",
                    "message": {"id": "a1", "usage": usage},
                },
            )
            stats = TranscriptMetricsCache().snapshot(
                path, "claude", window_start=0, window_end=2_000_000_000,
            )
            self.assertEqual(stats["window"]["requests"], 1)
            self.assertEqual(stats["window"]["characters"], 5)
            self.assertEqual(stats["window"]["request_tokens"], 2)
            self.assertEqual(stats["window"]["tokens"], 26)
            self.assertEqual(stats["window"]["generated_tokens"], 11)
            self.assertEqual(stats["window"]["token_usage"]["output_tokens"], 11)


if __name__ == "__main__":
    unittest.main()
