from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class RunRecorder:
    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        entry = {
            "type": event_type,
            "seq": self._next_seq(),
            "ts": time.time(),
            **payload,
        }
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def role_start(self, role: str, model: str, iteration: int | None = None) -> None:
        self.record("role_start", {"role": role, "model": model, "iteration": iteration})

    def role_end(self, role: str, status: str, duration_ms: int, iteration: int | None = None) -> None:
        self.record("role_end", {"role": role, "status": status, "duration_ms": duration_ms, "iteration": iteration})

    def reasoning_delta(self, role: str, delta: str, iteration: int | None = None) -> None:
        self.record("reasoning_delta", {"role": role, "delta": delta, "iteration": iteration})

    def message_delta(self, role: str, delta: str, iteration: int | None = None) -> None:
        self.record("message_delta", {"role": role, "delta": delta, "iteration": iteration})

    def tool_call_start(self, phase: str, tool: str, arguments: dict[str, Any], iteration: int) -> None:
        self.record("tool_call_start", {"phase": phase, "tool": tool, "arguments": arguments, "iteration": iteration})

    def tool_result(self, phase: str, tool: str, result_summary: Any, iteration: int) -> None:
        self.record("tool_result", {"phase": phase, "tool": tool, "result_summary": result_summary, "iteration": iteration})

    def final_summary(self, verification: dict[str, Any], changed_files: list[str]) -> None:
        self.record(
            "final_summary",
            {
                "verification": verification,
                "changed_files": changed_files,
                "transcript_path": str(self.output_path),
            },
        )
