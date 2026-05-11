from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _tmp_gumloop_omo_mcp.harnesses.omo_pseudo_loop import run_loop
from _tmp_gumloop_omo_mcp.tests.test_omo_pseudo_loop import FakeMCPClient, FakeReasoner


class OmoSessionModeTests(unittest.TestCase):
    def test_read_only_session_skips_executor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sandbox_config.json"
            config_path.write_text(json.dumps({"mcp_url": "http://fake.example/mcp"}), encoding="utf-8")

            with patch("_tmp_gumloop_omo_mcp.harnesses.omo_pseudo_loop.ProxyGLRoleReasoner", FakeReasoner), patch(
                "_tmp_gumloop_omo_mcp.harnesses.omo_pseudo_loop.SandboxMCPClient", FakeMCPClient
            ):
                result = asyncio.run(run_loop(config_path, "read_hello", 3, allow_writes=False, workspace_goal="inspect workspace"))

            transcript = Path(result["output_path"]).read_text(encoding="utf-8")
            self.assertIn('"type": "executor_skipped"', transcript)

    def test_session_start_records_goal_and_write_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sandbox_config.json"
            config_path.write_text(json.dumps({"mcp_url": "http://fake.example/mcp"}), encoding="utf-8")

            with patch("_tmp_gumloop_omo_mcp.harnesses.omo_pseudo_loop.ProxyGLRoleReasoner", FakeReasoner), patch(
                "_tmp_gumloop_omo_mcp.harnesses.omo_pseudo_loop.SandboxMCPClient", FakeMCPClient
            ):
                result = asyncio.run(run_loop(config_path, "read_hello", 3, allow_writes=True, workspace_goal="edit sandbox"))

            first_line = Path(result["output_path"]).read_text(encoding="utf-8").splitlines()[0]
            entry = json.loads(first_line)
            self.assertEqual(entry["workspace_goal"], "edit sandbox")
            self.assertTrue(entry["write_enabled"])
