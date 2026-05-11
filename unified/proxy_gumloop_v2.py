"""Gumloop v2 proxy - OpenCode/OMO-friendly wrapper behavior for gl2-* models."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi.responses import JSONResponse, StreamingResponse

from .gumloop.client import send_chat, update_gummie_config
from .gumloop.parser import build_openai_chunk, build_openai_done, build_openai_tool_call_chunk
from .gumloop.tool_converter import convert_messages_with_tools, parse_tool_calls
from .proxy_gumloop import _ensure_turnstile_key, _get_auth, _get_turnstile

log = logging.getLogger("unified.proxy_gumloop_v2")


def _map_gl2_model(model: str) -> str:
    bare = model.removeprefix("gl2-")
    if any(x in bare for x in ("claude", "haiku", "sonnet", "opus")):
        bare = bare.replace(".", "-")
    return bare


def _extract_system(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return "\n\n".join(part for part in parts if part)


def _openai_tools_to_gumloop(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool.get("function", {})
        result.append(
            {
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return result


def _tool_uses_to_openai(tool_uses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in tool_uses:
        result.append(
            {
                "id": item.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": json.dumps(item.get("input", {}), ensure_ascii=False),
                },
            }
        )
    return result


async def proxy_chat_completions(
    body: dict,
    account: dict,
    client_wants_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    from . import database as db

    auth = _get_auth(account)
    await _ensure_turnstile_key()
    turnstile = _get_turnstile()
    gummie_id = account.get("gl_gummie_id", "")
    if not gummie_id:
        return JSONResponse({"error": {"message": "Account has no gummie_id", "type": "server_error"}}, status_code=503), 0.0

    raw_model = body.get("model", "gl2-claude-sonnet-4-5")
    gl_model = _map_gl2_model(raw_model)
    messages = body.get("messages", [])
    if not messages:
        return JSONResponse({"error": {"message": "No messages provided", "type": "invalid_request_error"}}, status_code=400), 0.0

    system_prompt = _extract_system(messages)
    gumloop_tools = _openai_tools_to_gumloop(body.get("tools", []))
    converted_messages = convert_messages_with_tools(messages, tools=gumloop_tools, system=system_prompt)

    interaction_id = None
    chat_session_id = body.get("chat_session_id")
    if chat_session_id:
        try:
            interaction_id = await db.get_or_create_gumloop_interaction_id(int(chat_session_id))
        except (TypeError, ValueError):
            interaction_id = None
    if not interaction_id:
        interaction_id = str(uuid.uuid4()).replace("-", "")[:22]

    try:
        await update_gummie_config(
            gummie_id=gummie_id,
            auth=auth,
            system_prompt=system_prompt or None,
            tools=None,
            model_name=gl_model,
            proxy_url=proxy_url,
        )
    except Exception as e:
        log.warning("Failed to update Gumloop v2 gummie config: %s", e)

    stream_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if client_wants_stream:
        return _stream_gumloop_v2(
            gummie_id,
            converted_messages,
            auth,
            turnstile,
            raw_model,
            stream_id,
            created,
            interaction_id,
            proxy_url,
        ), 0.0

    return await _accumulate_gumloop_v2(
        gummie_id,
        converted_messages,
        auth,
        turnstile,
        raw_model,
        stream_id,
        created,
        interaction_id,
        proxy_url,
    )


def _stream_gumloop_v2(
    gummie_id: str,
    messages: list[dict[str, Any]],
    auth,
    turnstile,
    display_model: str,
    stream_id: str,
    created: int,
    interaction_id: str,
    proxy_url: str | None,
) -> StreamingResponse:
    async def stream_sse() -> AsyncIterator[bytes]:
        full_text = ""
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        yield build_openai_chunk(stream_id, display_model, content="", role="assistant", created=created).encode()
        async for event in send_chat(gummie_id, messages, auth, turnstile, interaction_id=interaction_id, proxy_url=proxy_url):
            etype = event.get("type", "")
            if etype == "text-delta":
                delta = event.get("delta", "")
                if delta:
                    full_text += delta
            elif etype == "finish":
                event_usage = event.get("usage") or {}
                usage["prompt_tokens"] += event_usage.get("input_tokens", 0)
                usage["completion_tokens"] += event_usage.get("output_tokens", 0)
                usage["total_tokens"] += event_usage.get("total_tokens", 0)
                if not event.get("final", True):
                    continue
                break
            elif etype == "error":
                error_msg = event.get("error", "Unknown Gumloop v2 error")
                err = {"error": {"message": error_msg, "type": "proxy_error"}}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()
                yield build_openai_done().encode()
                return

        remaining_text, tool_uses = parse_tool_calls(full_text)
        if remaining_text:
            for i, word in enumerate(remaining_text.split(" ")):
                chunk_text = word if i == 0 else " " + word
                if chunk_text:
                    yield build_openai_chunk(stream_id, display_model, content=chunk_text, created=created).encode()

        for idx, tc in enumerate(_tool_uses_to_openai(tool_uses)):
            yield build_openai_tool_call_chunk(
                stream_id,
                display_model,
                idx,
                tc["id"],
                tc["function"]["name"],
                tc["function"]["arguments"],
                created=created,
            ).encode()

        finish_reason = "tool_calls" if tool_uses else "stop"
        yield build_openai_chunk(stream_id, display_model, finish_reason=finish_reason, created=created, usage=usage).encode()
        yield build_openai_done().encode()

    return StreamingResponse(
        stream_sse(),
        status_code=200,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


async def _accumulate_gumloop_v2(
    gummie_id: str,
    messages: list[dict[str, Any]],
    auth,
    turnstile,
    display_model: str,
    stream_id: str,
    created: int,
    interaction_id: str,
    proxy_url: str | None,
) -> tuple[JSONResponse, float]:
    try:
        full_text = ""
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        async for event in send_chat(gummie_id, messages, auth, turnstile, interaction_id=interaction_id, proxy_url=proxy_url):
            etype = event.get("type", "")
            if etype == "text-delta":
                full_text += event.get("delta", "")
            elif etype == "finish":
                event_usage = event.get("usage") or {}
                prompt_tokens += event_usage.get("input_tokens", 0)
                completion_tokens += event_usage.get("output_tokens", 0)
                total_tokens += event_usage.get("total_tokens", 0)
                if event.get("final", True):
                    break
            elif etype == "error":
                return JSONResponse({"error": {"message": event.get("error", "Unknown Gumloop v2 error"), "type": "proxy_error"}}, status_code=502), 0.0

        remaining_text, tool_uses = parse_tool_calls(full_text)
        tool_calls = _tool_uses_to_openai(tool_uses)
        message: dict[str, Any] = {"role": "assistant", "content": remaining_text or None}
        if tool_calls:
            message["tool_calls"] = tool_calls
        finish_reason = "tool_calls" if tool_calls else "stop"
        response = {
            "id": stream_id,
            "object": "chat.completion",
            "created": created,
            "model": display_model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total_tokens},
        }
        return JSONResponse(response, status_code=200), 0.0
    except Exception as e:
        log.error("Gumloop v2 error: %s", e, exc_info=True)
        return JSONResponse({"error": {"message": f"Gumloop v2 error: {e}", "type": "proxy_error"}}, status_code=502), 0.0
