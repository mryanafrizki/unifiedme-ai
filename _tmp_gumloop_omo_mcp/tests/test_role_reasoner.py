from __future__ import annotations

import asyncio
import unittest

from _tmp_gumloop_omo_mcp.wrappers.role_reasoner import ScriptedRoleReasoner, extract_json_object
from _tmp_gumloop_omo_mcp.wrappers.proxy_role_reasoner import load_proxy_key


class RoleReasonerTests(unittest.TestCase):
    def test_extract_json_object(self) -> None:
        payload = extract_json_object("prefix {\"status\": \"done\", \"summary\": \"ok\"} suffix")
        self.assertEqual(payload["status"], "done")

    def test_scripted_role_reasoner(self) -> None:
        async def run() -> tuple[dict, str]:
            reasoner = ScriptedRoleReasoner({"planner": [{"status": "done", "goal": "x"}]})
            return await reasoner.run_role("planner", "", {})

        payload, raw = asyncio.run(run())
        self.assertEqual(payload["goal"], "x")
        self.assertIn("goal", raw)

    def test_load_proxy_key_prefers_config(self) -> None:
        self.assertEqual(load_proxy_key({"proxy_api_key": "abc"}), "abc")


if __name__ == "__main__":
    unittest.main()
