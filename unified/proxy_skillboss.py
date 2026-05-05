"""SkillBoss proxy — translates OpenAI format to SkillBoss /v1/run (Anthropic format).

Routes skboss-* model requests through SkillBoss API with full tool calling support.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import AsyncIterator

import httpx
from fastapi.responses import StreamingResponse, JSONResponse

log = logging.getLogger("unified.proxy_skillboss")

SKILLBOSS_URL = "https://api.skillboss.co/v1/run"

# Model mapping: strip "skboss-" prefix then map to SkillBoss model IDs
_SKBOSS_MODEL_MAP = {
    "claude-opus-4.7": "tencent_vod/cd-opus-4.7",
    "claude-opus-4.6": "tencent_vod/cd-opus-4.6",
    "claude-sonnet-4.6": "tencent_vod/cd-sonnet-4.6",
    "claude-haiku-4.5": "tencent_vod/cd-haiku-4.5",
    "gpt-5.4": "tencent_vod/gpt-5.4",
    "gpt-5.2": "tencent_vod/gpt-5.2",
    "gpt-5.1": "tencent_vod/gpt-5.1",
    "gpt-5.4-mini": "tencent_vod/gpt-5.4-mini",
    "gpt-5-nano": "tencent_vod/gpt-5-nano",
    "gemini-2.5-flash": "tencent_vod/gemini-2.5-flash",
    "gemini-3.1-pro": "tencent_vod/gemini-3.1-pro-preview",
}

_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=300, write=30, pool=10),
            follow_redirects=True,
        )
    return _client


def _map_model(model: str) -> str:
    """Strip skboss- prefix and map to SkillBoss model ID."""
    name = model.removeprefix("skboss-")
    return _SKBOSS_MODEL_MAP.get(name, name)


# ---------------------------------------------------------------------------
# OpenAI -> Anthropic conversion
# ---------------------------------------------------------------------------

def _extract_system(messages: list) -> str:
    parts = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
    return "\n\n".join(parts) if parts else ""


def _openai_to_anthropic_messages(messages: list) -> list:
    result = []
    for msg in messages:
        role = msg.get("role", "user")
        if role == "system":
            continue

        content = msg.get("content")
        tool_calls = msg.get("tool_calls")
        tool_call_id = msg.get("tool_call_id")

        # Assistant with tool_calls
        if role == "assistant" and tool_calls:
            blocks = []
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in tool_calls:
                func = tc.get("function", {})
                args = func.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, ValueError):
                        args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                    "name": func.get("name", ""),
                    "input": args,
                })
            result.append({"role": "assistant", "content": blocks})

        # Tool result
        elif role == "tool":
            tool_content = content if isinstance(content, str) else json.dumps(content)
            result.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_call_id or "", "content": tool_content}],
            })

        # Regular message
        else:
            if isinstance(content, list):
                anthropic_blocks = []
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype == "text":
                            anthropic_blocks.append({"type": "text", "text": block.get("text", "")})
                        elif btype == "image_url":
                            url = block.get("image_url", {}).get("url", "")
                            if url.startswith("data:"):
                                parts_split = url.split(",", 1)
                                media_type = parts_split[0].split(";")[0].split(":")[1] if ":" in parts_split[0] else "image/png"
                                data = parts_split[1] if len(parts_split) > 1 else ""
                                anthropic_blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
                            else:
                                anthropic_blocks.append({"type": "image", "source": {"type": "url", "url": url}})
                result.append({"role": role, "content": anthropic_blocks if anthropic_blocks else ""})
            else:
                result.append({"role": role, "content": content or ""})

    return result


def _openai_tools_to_anthropic(tools: list) -> list:
    anthropic_tools = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool.get("function", {})
        anthropic_tools.append({
            "name": func.get("name", ""),
            "description": func.get("description", ""),
            "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
        })
    return anthropic_tools


# ---------------------------------------------------------------------------
# Anthropic -> OpenAI conversion
# ---------------------------------------------------------------------------

def _anthropic_to_openai(sb_response: dict, model: str) -> dict:
    content_blocks = sb_response.get("content", [])
    text_parts = []
    tool_calls = []

    for block in content_blocks:
        if isinstance(block, dict):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
        elif isinstance(block, str):
            text_parts.append(block)

    full_text = "".join(text_parts)

    usage = sb_response.get("usage", {})
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)

    stop_reason = sb_response.get("stop_reason", "end_turn")
    if stop_reason == "tool_use":
        finish_reason = "tool_calls"
    elif stop_reason == "end_turn":
        finish_reason = "stop"
    elif stop_reason == "max_tokens":
        finish_reason = "length"
    else:
        finish_reason = "stop"

    message: dict = {"role": "assistant", "content": full_text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
        if not full_text:
            message["content"] = None

    # Extract cost from SkillBoss metadata
    cost = float(sb_response.get("_call_cost_usd", 0) or 0)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "_cost": cost,
    }


# ---------------------------------------------------------------------------
# Main proxy function
# ---------------------------------------------------------------------------

async def proxy_chat_completions(
    body: dict,
    api_key: str,
    client_wants_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    """Proxy chat completion to SkillBoss /v1/run.

    Returns (response, credit_used) tuple.
    """
    model = body.get("model", "")
    messages = body.get("messages", [])
    max_tokens = body.get("max_tokens") or body.get("max_output_tokens") or 4096
    temperature = body.get("temperature")
    tools = body.get("tools", [])
    tool_choice = body.get("tool_choice")

    sb_model = _map_model(model)
    system_prompt = _extract_system(messages)
    anthropic_messages = _openai_to_anthropic_messages(messages)

    # Build SkillBoss request
    sb_inputs: dict = {"messages": anthropic_messages, "max_tokens": max_tokens}
    if system_prompt:
        sb_inputs["system"] = system_prompt
    if temperature is not None:
        sb_inputs["temperature"] = temperature
    if tools:
        sb_inputs["tools"] = _openai_tools_to_anthropic(tools)
        if tool_choice == "auto" or tool_choice is None:
            sb_inputs["tool_choice"] = {"type": "auto"}
        elif tool_choice == "none":
            sb_inputs["tool_choice"] = {"type": "none"}
        elif tool_choice == "required":
            sb_inputs["tool_choice"] = {"type": "any"}
        elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            sb_inputs["tool_choice"] = {"type": "tool", "name": tool_choice["function"]["name"]}

    sb_body = {"model": sb_model, "inputs": sb_inputs}

    client = await _get_client()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # No internal retry — let router_proxy handle account rotation on 429
    resp = None
    sb_data = None
    try:
        resp = await client.post(SKILLBOSS_URL, json=sb_body, headers=headers)
    except httpx.ConnectError:
        return JSONResponse(
            {"error": {"message": "SkillBoss upstream unavailable", "type": "proxy_error"}},
            status_code=502,
        ), 0.0
    except httpx.TimeoutException:
        return JSONResponse(
            {"error": {"message": "SkillBoss timeout", "type": "proxy_error"}},
            status_code=504,
        ), 0.0

    # Surface 429 immediately so router rotates to next account
    if resp.status_code == 200:
        sb_data = resp.json()
        if sb_data.get("code") == 500 and "429" in sb_data.get("message", ""):
            log.warning("SkillBoss rate limited (code 500/429)")
            return JSONResponse(
                {"error": {"message": sb_data.get("message", "Rate limited"), "type": "rate_limit"}},
                status_code=429,
            ), 0.0

    if resp is None:
        return JSONResponse(
            {"error": {"message": "SkillBoss no response", "type": "proxy_error"}},
            status_code=502,
        ), 0.0

    if resp.status_code != 200:
        try:
            error_json = resp.json()
        except Exception:
            error_json = {"error": {"message": resp.text[:300], "type": "upstream_error"}}
        return JSONResponse(error_json, status_code=resp.status_code), 0.0

    if sb_data is None:
        sb_data = resp.json()

    if sb_data.get("code") == 500:
        msg = sb_data.get("message", "Unknown error")
        return JSONResponse(
            {"error": {"message": msg, "type": "upstream_error"}},
            status_code=400,
        ), 0.0

    # Convert to OpenAI format
    openai_response = _anthropic_to_openai(sb_data, model)
    credit_used = openai_response.pop("_cost", 0.0)
    if credit_used <= 0:
        credit_used = 0.01  # fallback minimum

    if client_wants_stream:
        async def stream_sse() -> AsyncIterator[bytes]:
            resp_id = openai_response["id"]
            created = openai_response["created"]
            message = openai_response["choices"][0]["message"]
            finish = openai_response["choices"][0]["finish_reason"]
            content = message.get("content") or ""
            tc_list = message.get("tool_calls", [])

            # First chunk: role
            first = {"id": resp_id, "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
            yield f"data: {json.dumps(first)}\n\n".encode()

            # Content chunks
            if content:
                words = content.split(" ")
                for i, word in enumerate(words):
                    chunk_text = word if i == 0 else " " + word
                    chunk = {"id": resp_id, "object": "chat.completion.chunk", "created": created, "model": model,
                             "choices": [{"index": 0, "delta": {"content": chunk_text}, "finish_reason": None}]}
                    yield f"data: {json.dumps(chunk)}\n\n".encode()

            # Tool call chunks
            for idx, tc in enumerate(tc_list):
                tc_chunk = {"id": resp_id, "object": "chat.completion.chunk", "created": created, "model": model,
                            "choices": [{"index": 0, "delta": {"tool_calls": [{"index": idx, "id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]}, "finish_reason": None}]}
                yield f"data: {json.dumps(tc_chunk)}\n\n".encode()

            # Final
            final = {"id": resp_id, "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                     "usage": openai_response["usage"]}
            yield f"data: {json.dumps(final)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            stream_sse(), status_code=200, media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        ), credit_used

    return JSONResponse(openai_response, status_code=200), credit_used
