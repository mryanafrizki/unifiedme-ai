from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from _tmp_gumloop_omo_mcp.wrappers.role_reasoner import extract_json_object


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROXY_KEY_FILE = ROOT / "unified" / "data" / ".mcp_api_key"


def load_proxy_key(config: dict[str, Any]) -> str:
    key = str(config.get("proxy_api_key", "")).strip()
    if key:
        return key
    if DEFAULT_PROXY_KEY_FILE.exists():
        return DEFAULT_PROXY_KEY_FILE.read_text(encoding="utf-8").strip()
    return ""


class ProxyGLRoleReasoner:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.proxy_base_url = str(self.config.get("proxy_base_url", "http://127.0.0.1:1430")).rstrip("/")
        self.model = str(self.config.get("model", "gl-claude-opus-4-7")).strip()
        self.api_key = load_proxy_key(self.config)

    async def stream_role(self, role: str, system_prompt: str, context: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("No proxy API key available for proxy GL role reasoner")

        user_payload = json.dumps(context, ensure_ascii=False, indent=2)
        response_schema = (
            "Return exactly one JSON object and nothing else. "
            "Do not use markdown fences. Do not narrate."
        )
        messages = [
            {"role": "system", "content": f"{system_prompt}\n{response_schema}"},
            {"role": "user", "content": user_payload},
        ]

        yielded_any = False
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=300, write=30, pool=10)) as client:
            async with client.stream(
                "POST",
                f"{self.proxy_base_url}/v1/chat/completions",
                json={"model": self.model, "messages": messages, "stream": True},
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise RuntimeError(f"Proxy GL role '{role}' failed: HTTP {response.status_code}: {body.decode('utf-8', errors='replace')[:500]}")

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    chunk = json.loads(data_str)
                    if "error" in chunk:
                        err = chunk["error"]
                        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        raise RuntimeError(f"Proxy GL role '{role}' failed: {msg}")
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    if delta.get("thinking"):
                        yielded_any = True
                        yield {"type": "reasoning", "delta": delta["thinking"]}
                    if delta.get("reasoning"):
                        yielded_any = True
                        yield {"type": "reasoning", "delta": delta["reasoning"]}
                    if delta.get("reasoning_content"):
                        yielded_any = True
                        yield {"type": "reasoning", "delta": delta["reasoning_content"]}
                    if delta.get("content"):
                        yielded_any = True
                        yield {"type": "content", "delta": delta["content"]}
                    if choice.get("finish_reason"):
                        yield {"type": "finish", "finish_reason": choice["finish_reason"]}

        if not yielded_any:
            parsed, raw = await self.run_role(role, system_prompt, context)
            yield {"type": "content", "delta": raw}
            yield {"type": "finish", "finish_reason": "stop", "parsed": parsed}

    async def run_role(self, role: str, system_prompt: str, context: dict[str, Any]) -> tuple[dict[str, Any], str]:
        pieces: list[str] = []
        parsed: dict[str, Any] | None = None
        async for event in self.stream_role(role, system_prompt, context):
            if event["type"] == "content":
                pieces.append(event["delta"])
            elif event["type"] == "finish" and event.get("parsed"):
                parsed = event["parsed"]
        raw = "".join(pieces)
        if parsed is None:
            parsed = extract_json_object(raw)
        return parsed, raw
