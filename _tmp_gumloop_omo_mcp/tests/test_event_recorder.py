from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _tmp_gumloop_omo_mcp.wrappers.event_recorder import EventRecorder, normalize_event
from _tmp_gumloop_omo_mcp.wrappers.run_recorder import RunRecorder


class EventRecorderTests(unittest.TestCase):
    def test_normalize_keeps_relevant_fields(self) -> None:
        event = {
            "type": "tool-call",
            "toolName": "read_file",
            "input": {"path": "hello.txt"},
            "unused": "ignore",
        }
        normalized = normalize_event(event)
        self.assertEqual(normalized["type"], "tool-call")
        self.assertEqual(normalized["toolName"], "read_file")
        self.assertNotIn("unused", normalized)

    def test_recorder_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "events.jsonl"
            recorder = EventRecorder(output)
            recorder.write({"type": "finish", "final": True})
            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["type"], "finish")

    def test_run_recorder_stream_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "run.jsonl"
            recorder = RunRecorder(output)
            recorder.role_start("planner", "gl-claude-opus-4-7")
            recorder.message_delta("planner", "{")
            recorder.role_end("planner", "done", 12)
            lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines[0]["type"], "role_start")
            self.assertEqual(lines[1]["type"], "message_delta")
            self.assertEqual(lines[2]["type"], "role_end")
            self.assertEqual(lines[0]["seq"], 1)
            self.assertEqual(lines[2]["seq"], 3)


if __name__ == "__main__":
    unittest.main()
