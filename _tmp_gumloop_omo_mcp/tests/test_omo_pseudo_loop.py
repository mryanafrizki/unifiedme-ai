from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _tmp_gumloop_omo_mcp.harnesses.omo_pseudo_loop import run_loop


class FakeMCPClient:
    def __init__(self, server_url: str):
        self.server_url = server_url
        self.calls: list[tuple[str, dict]] = []

    async def connect(self) -> None:
        return None

    async def list_tools(self) -> list[dict]:
        return [{"name": "read_file"}, {"name": "edit_file"}]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        if name == "read_file":
            return {"text": "1: Hello from the Gumloop OMO MCP sandbox."}
        if name == "edit_file":
            return {"ok": True, "replacements": 1}
        return {"ok": True}

    async def close(self) -> None:
        return None


class FakeReasoner:
    def __init__(self, _config_path: Path):
        self.model = "gl-fake"
        self.outputs = {
            "planner": [({"status": "done", "goal": "Read hello", "exploration_checks": ["read hello"], "execution_steps": [], "validation_checks": ["summary"]}, "planner")],
            "explorer": [
                ({"status": "needs_tool", "tool": "read_file", "arguments": {"path": "hello.txt"}, "reason": "Need file contents"}, "explorer-step1"),
                ({"status": "done", "summary": "Done exploring", "facts": ["hello.txt read"]}, "explorer-step2"),
            ],
            "executor": [({"status": "done", "summary": "Nothing to change", "changed_files": []}, "executor")],
            "verifier": [({"status": "done", "summary": "Verified", "checks": ["read ok"], "risks": []}, "verifier")],
        }

    async def run_role(self, role: str, system_prompt: str, context: dict):
        payload, raw = self.outputs[role].pop(0)
        return payload, raw

    async def stream_role(self, role: str, system_prompt: str, context: dict):
        payload, raw = await self.run_role(role, system_prompt, context)
        yield {"type": "content", "delta": raw}
        yield {"type": "finish", "finish_reason": "stop", "parsed": payload}


class OmoPseudoLoopTests(unittest.TestCase):
    def test_fake_loop_reads_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sandbox_config.json"
            config_path.write_text(json.dumps({"mcp_url": "http://fake.example/mcp"}), encoding="utf-8")

            with patch("_tmp_gumloop_omo_mcp.harnesses.omo_pseudo_loop.ProxyGLRoleReasoner", FakeReasoner), patch(
                "_tmp_gumloop_omo_mcp.harnesses.omo_pseudo_loop.SandboxMCPClient", FakeMCPClient
            ):
                result = asyncio.run(run_loop(config_path, "read_hello", 3))

            self.assertIn("hello.txt read", result["exploration_facts"])
            self.assertEqual(result["verification"]["summary"], "Verified")

    def test_fake_loop_accepts_custom_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sandbox_config.json"
            config_path.write_text(json.dumps({"mcp_url": "http://fake.example/mcp"}), encoding="utf-8")

            with patch("_tmp_gumloop_omo_mcp.harnesses.omo_pseudo_loop.ProxyGLRoleReasoner", FakeReasoner), patch(
                "_tmp_gumloop_omo_mcp.harnesses.omo_pseudo_loop.SandboxMCPClient", FakeMCPClient
            ):
                result = asyncio.run(run_loop(config_path, None, 3, custom_prompt="Read hello please"))

            self.assertEqual(result["verification"]["summary"], "Verified")

    def test_transcript_contains_stream_and_tool_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sandbox_config.json"
            config_path.write_text(json.dumps({"mcp_url": "http://fake.example/mcp"}), encoding="utf-8")

            with patch("_tmp_gumloop_omo_mcp.harnesses.omo_pseudo_loop.ProxyGLRoleReasoner", FakeReasoner), patch(
                "_tmp_gumloop_omo_mcp.harnesses.omo_pseudo_loop.SandboxMCPClient", FakeMCPClient
            ):
                result = asyncio.run(run_loop(config_path, "read_hello", 3))

            lines = Path(result["output_path"]).read_text(encoding="utf-8").splitlines()
            event_types = [json.loads(line)["type"] for line in lines]
            self.assertIn("role_start", event_types)
            self.assertIn("message_delta", event_types)
            self.assertIn("tool_call_start", event_types)
            self.assertIn("tool_result", event_types)
            self.assertIn("final_summary", event_types)


if __name__ == "__main__":
    unittest.main()
