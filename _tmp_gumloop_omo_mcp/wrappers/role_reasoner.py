from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _tmp_gumloop_omo_mcp.wrappers.gumloop_client_wrapper import load_config, stream_gumloop_events


def extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in model output")
    return json.loads(text[start : end + 1])


class ScriptedRoleReasoner:
    def __init__(self, scripted_outputs: dict[str, list[dict[str, Any]]]):
        self.scripted_outputs = {k: list(v) for k, v in scripted_outputs.items()}

    async def run_role(self, role: str, system_prompt: str, context: dict[str, Any]) -> tuple[dict[str, Any], str]:
        queue = self.scripted_outputs.get(role, [])
        if not queue:
            raise RuntimeError(f"No scripted output left for role {role}")
        payload = queue.pop(0)
        return payload, json.dumps(payload, ensure_ascii=False)


class GumloopRoleReasoner:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)

    async def run_role(self, role: str, system_prompt: str, context: dict[str, Any]) -> tuple[dict[str, Any], str]:
        user_payload = json.dumps(context, ensure_ascii=False, indent=2)
        messages = [{"role": "user", "content": user_payload}]
        full_text = ""
        last_error = ""

        async for event in stream_gumloop_events(self.config, messages, system_prompt):
            event_type = event.get("type", "")
            if event_type == "text-delta":
                full_text += event.get("delta", "")
            elif event_type == "error":
                last_error = event.get("error", "Unknown Gumloop error")
            elif event_type == "finish" and event.get("final", True):
                break

        if last_error:
            raise RuntimeError(f"Gumloop role '{role}' failed: {last_error}")

        try:
            return extract_json_object(full_text), full_text
        except Exception:
            retry_prompt = (
                system_prompt
                + "\nReturn ONLY one valid JSON object matching the requested schema."
            )
            retry_text = ""
            async for event in stream_gumloop_events(self.config, messages, retry_prompt):
                event_type = event.get("type", "")
                if event_type == "text-delta":
                    retry_text += event.get("delta", "")
                elif event_type == "error":
                    last_error = event.get("error", "Unknown Gumloop error")
                elif event_type == "finish" and event.get("final", True):
                    break
            if last_error:
                raise RuntimeError(f"Gumloop role '{role}' failed: {last_error}")
            return extract_json_object(retry_text), retry_text
