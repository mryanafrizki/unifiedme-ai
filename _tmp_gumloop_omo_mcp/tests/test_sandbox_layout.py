from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "_tmp_gumloop_omo_mcp"
WORKSPACE = SANDBOX / "fixtures" / "workspace"


class SandboxLayoutTests(unittest.TestCase):
    def test_workspace_fixture_exists(self) -> None:
        self.assertTrue((WORKSPACE / "hello.txt").exists())
        self.assertTrue((WORKSPACE / "sample_app.py").exists())

    def test_outputs_dir_is_inside_sandbox(self) -> None:
        outputs = SANDBOX / "outputs"
        self.assertEqual(outputs.parent, SANDBOX)

    def test_role_prompts_and_tasks_exist(self) -> None:
        self.assertTrue((SANDBOX / "fixtures" / "role_prompts" / "planner.txt").exists())
        self.assertTrue((SANDBOX / "fixtures" / "role_prompts" / "explorer.txt").exists())
        self.assertTrue((SANDBOX / "fixtures" / "role_prompts" / "executor.txt").exists())
        self.assertTrue((SANDBOX / "fixtures" / "role_prompts" / "verifier.txt").exists())
        self.assertTrue((SANDBOX / "fixtures" / "tasks" / "read_hello.txt").exists())
        self.assertTrue((SANDBOX / "fixtures" / "tasks" / "edit_sample_app.txt").exists())


if __name__ == "__main__":
    unittest.main()
