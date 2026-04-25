"""OpenAI SSE chunk builders for Gumloop responses."""

from __future__ import annotations

import json
from typing import Any, Optional


def build_openai_chunk(
    stream_id: str,
    model: str,
    content: Optional[str] = None,
    role: Optional[str] = None,
    finish_reason: Optional[str] = None,
    created: int = 0,
    usage: Optional[dict] = None,
) -> str:
    delta: dict[str, Any] = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    chunk: dict[str, Any] = {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage:
        chunk["usage"] = usage
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def build_openai_tool_call_chunk(
    stream_id: str,
    model: str,
    tool_call_index: int,
    tool_call_id: str,
    function_name: str,
    arguments_delta: str,
    created: int = 0,
) -> str:
    tc = {
        "index": tool_call_index,
        "id": tool_call_id,
        "type": "function",
        "function": {"name": function_name, "arguments": arguments_delta},
    }
    chunk = {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def build_openai_done() -> str:
    return "data: [DONE]\n\n"
