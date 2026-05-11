from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = event.get("type", "")
    normalized = {
        "type": event_type,
        "final": bool(event.get("final", False)),
    }

    for key in (
        "delta",
        "toolName",
        "input",
        "output",
        "error",
        "errorType",
        "usage",
        "interaction_id",
    ):
        if key in event:
            normalized[key] = event[key]

    return normalized


class EventRecorder:
    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict[str, Any]) -> None:
        normalized = normalize_event(event)
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(normalized, ensure_ascii=False) + "\n")
