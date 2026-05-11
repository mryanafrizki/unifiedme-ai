from __future__ import annotations

import unittest

from _tmp_gumloop_omo_mcp.wrappers.tool_guard import ToolGuard


class ToolGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = ToolGuard()

    def test_explorer_disallows_write(self) -> None:
        ok, message = self.guard.validate("explorer", "write_file", {"path": "hello.txt", "content": "x"})
        self.assertFalse(ok)
        self.assertIn("not allowed", message)

    def test_rejects_absolute_windows_path(self) -> None:
        ok, message = self.guard.validate("executor", "read_file", {"path": "C:\\temp\\x.txt"})
        self.assertFalse(ok)
        self.assertIn("Unsafe path", message)

    def test_allows_relative_read(self) -> None:
        ok, message = self.guard.validate("explorer", "read_file", {"path": "hello.txt"})
        self.assertTrue(ok)
        self.assertEqual(message, "")


if __name__ == "__main__":
    unittest.main()
