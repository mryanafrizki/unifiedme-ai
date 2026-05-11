from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


READ_ONLY_TOOLS = {
    "list_directory",
    "tree",
    "glob_search",
    "grep",
    "read_file",
    "file_info",
}

EXECUTOR_EXTRA_TOOLS = {
    "write_file",
    "edit_file",
    "bash",
    "run_python",
}

PATH_ARGUMENT_KEYS = {
    "path",
    "old_path",
    "new_path",
    "source",
    "destination",
    "paths",
    "workspace",
    "file_path",
    "filePath",
}


def _is_unsafe_path(value: str) -> bool:
    if not value:
        return False
    if value.startswith("/") or value.startswith("\\"):
        return True
    if PureWindowsPath(value).drive:
        return True
    parts = PurePosixPath(value.replace("\\", "/")).parts
    return ".." in parts


class ToolGuard:
    def __init__(self, allow_executor_writes: bool = True):
        self.allow_executor_writes = allow_executor_writes

    def allowed_tools(self, phase: str) -> set[str]:
        if phase == "explorer":
            return set(READ_ONLY_TOOLS)
        if phase == "executor":
            tools = set(READ_ONLY_TOOLS)
            if self.allow_executor_writes:
                tools |= EXECUTOR_EXTRA_TOOLS
            return tools
        return set()

    def validate(self, phase: str, tool_name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        if tool_name not in self.allowed_tools(phase):
            return False, f"Tool '{tool_name}' is not allowed during {phase} phase"

        for key, value in arguments.items():
            if key not in PATH_ARGUMENT_KEYS:
                continue
            if isinstance(value, str) and _is_unsafe_path(value):
                return False, f"Unsafe path for {key}: {value}"
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and _is_unsafe_path(item):
                        return False, f"Unsafe path for {key}: {item}"

        return True, ""
