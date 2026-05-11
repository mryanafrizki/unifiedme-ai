from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _tmp_gumloop_omo_mcp.wrappers.proxy_role_reasoner import ProxyGLRoleReasoner


class FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self.lines = kwargs.pop("lines", None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, json=None, headers=None):
        lines = [
            'data: {"choices":[{"delta":{"thinking":"think "},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"{\\"status\\":\\"done\\","},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"\\"summary\\":\\"ok\\""},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"}"},"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]
        return FakeStreamResponse(lines)


class ProxyRoleStreamingTests(unittest.TestCase):
    def test_stream_role_yields_reasoning_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sandbox_config.json"
            config_path.write_text(json.dumps({"proxy_api_key": "sk-test", "model": "gl-claude-opus-4-7"}), encoding="utf-8")
            reasoner = ProxyGLRoleReasoner(config_path)

            async def run() -> list[dict]:
                events = []
                async for item in reasoner.stream_role("planner", "prompt", {"task": "x"}):
                    events.append(item)
                return events

            with patch("_tmp_gumloop_omo_mcp.wrappers.proxy_role_reasoner.httpx.AsyncClient", FakeAsyncClient):
                events = asyncio.run(run())

            self.assertEqual(events[0]["type"], "reasoning")
            self.assertEqual(events[1]["type"], "content")
            self.assertEqual(events[-1]["type"], "finish")

    def test_run_role_reassembles_streamed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sandbox_config.json"
            config_path.write_text(json.dumps({"proxy_api_key": "sk-test", "model": "gl-claude-opus-4-7"}), encoding="utf-8")
            reasoner = ProxyGLRoleReasoner(config_path)

            with patch("_tmp_gumloop_omo_mcp.wrappers.proxy_role_reasoner.httpx.AsyncClient", FakeAsyncClient):
                parsed, raw = asyncio.run(reasoner.run_role("planner", "prompt", {"task": "x"}))

            self.assertEqual(parsed["status"], "done")
            self.assertIn("summary", raw)


if __name__ == "__main__":
    unittest.main()
