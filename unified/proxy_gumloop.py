"""Gumloop proxy — WebSocket chat with per-account auth and Turnstile captcha.

Routes gl-* model requests through Gumloop's WebSocket API.
Each account has its own GumloopAuth instance. Turnstile solver is shared.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import AsyncIterator

from fastapi.responses import StreamingResponse, JSONResponse

from .gumloop.auth import GumloopAuth
from .gumloop.turnstile import TurnstileSolver
from .gumloop.client import (
    send_chat,
    update_gummie_config,
    upload_file,
    GumloopStreamHandler,
)
import base64
import re
import httpx as _httpx

from .gumloop.parser import build_openai_chunk, build_openai_done, build_openai_tool_call_chunk
from .gumloop.tool_converter import (
    convert_messages_simple,
    convert_messages_with_tools,
    parse_tool_calls,
    detect_tool_loop,
)

log = logging.getLogger("unified.proxy_gumloop")

# Auth cache: account_id → GumloopAuth
_auth_cache: dict[int, GumloopAuth] = {}

# Shared turnstile solver (tokens are account-independent)
_turnstile: TurnstileSolver | None = None

# Gumloop models (native names)
GUMLOOP_MODELS = [
    "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
    "claude-sonnet-4-5", "claude-haiku-4-5",
    "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano",
    "gpt-5.3-code", "gpt-5.2", "gpt-5.2-codex",
]


def _get_turnstile() -> TurnstileSolver:
    """Get or create the shared TurnstileSolver."""
    global _turnstile
    if _turnstile is None:
        # Will be populated from DB on first API call, or from env as fallback
        api_key = os.getenv("CAPTCHA_API_KEY", "")
        _turnstile = TurnstileSolver(api_key)
    return _turnstile


async def _ensure_turnstile_key() -> None:
    """Load captcha API key from DB settings if not already set."""
    ts = _get_turnstile()
    if ts._api_key:
        return
    from . import database as db
    key = await db.get_setting("captcha_api_key", "")
    if key:
        ts.update_api_key(key)


def _get_auth(account: dict) -> GumloopAuth:
    """Get or create a GumloopAuth for an account."""
    acct_id = account["id"]
    if acct_id in _auth_cache:
        auth = _auth_cache[acct_id]
        # Update tokens if DB has newer ones
        db_token = account.get("gl_id_token", "")
        db_refresh = account.get("gl_refresh_token", "")
        if db_refresh and db_refresh != auth.refresh_token:
            auth.refresh_token = db_refresh
        if db_token and db_token != auth.id_token:
            auth.id_token = db_token
            auth.expires_at = 0  # Force refresh
        return auth

    auth = GumloopAuth(
        refresh_token=account.get("gl_refresh_token", ""),
        user_id=account.get("gl_user_id", ""),
        id_token=account.get("gl_id_token", ""),
    )
    _auth_cache[acct_id] = auth
    return auth


def _map_gl_model(model: str) -> str:
    """Map gl-prefixed model to Gumloop's internal name.

    gl-claude-opus-4.7 → claude-opus-4-7
    gl-claude-opus-4-7 → claude-opus-4-7
    gl-gpt-5.4 → gpt-5.4 (GPT models keep dots)
    """
    bare = model.removeprefix("gl-")
    # Claude models: dots → dashes for Gumloop
    if any(x in bare for x in ("claude", "haiku", "sonnet", "opus")):
        bare = bare.replace(".", "-")
    # Validate against known models
    if bare in GUMLOOP_MODELS:
        return bare
    # Fallback: return as-is
    return bare


def _detect_media_type(data: bytes, fallback: str = "image/png") -> str:
    """Detect image media type from magic bytes."""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    if data[:2] == b'\xff\xd8':
        return "image/jpeg"
    if data[:4] == b'GIF8':
        return "image/gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "image/webp"
    if data[:4] == b'%PDF':
        return "application/pdf"
    return fallback


def _ext_from_media_type(media_type: str) -> str:
    """Get file extension from media type."""
    m = {
        "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
        "image/webp": "webp", "application/pdf": "pdf",
    }
    return m.get(media_type, "png")


async def _extract_image_data(image_url: str) -> tuple[bytes, str] | None:
    """Extract image bytes from OpenAI image_url format.

    Supports:
    - data:image/png;base64,... (inline base64)
    - https://... (download URL)

    Returns (bytes, media_type) or None.
    """
    if image_url.startswith("data:"):
        # data:image/png;base64,iVBOR...
        match = re.match(r'data:([^;]+);base64,(.+)', image_url)
        if match:
            media_type = match.group(1)
            raw = base64.b64decode(match.group(2))
            return raw, media_type
        return None

    if image_url.startswith("http"):
        try:
            async with _httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(image_url)
                resp.raise_for_status()
                data = resp.content
                # Detect type from content-type header or magic bytes
                ct = resp.headers.get("content-type", "")
                media_type = ct.split(";")[0].strip() if ct else _detect_media_type(data)
                return data, media_type
        except Exception as e:
            log.warning("Failed to download image from %s: %s", image_url[:80], e)
            return None

    return None


def _convert_openai_messages(body: dict) -> tuple[list[dict], str | None, list[dict] | None]:
    """Convert OpenAI chat/completions body to Gumloop message format.

    Returns (messages, system_prompt, tools_for_gumloop).
    """
    messages = body.get("messages", [])
    tools = body.get("tools")

    # Extract system prompt
    system_prompt = None
    filtered_msgs = []
    for msg in messages:
        role = msg.get("role", "user")
        if role == "system":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            system_prompt = (system_prompt + "\n" + content) if system_prompt else content
        else:
            filtered_msgs.append(msg)

    # Convert tool definitions for Gumloop REST API
    tools_for_gumloop = None
    if tools:
        tools_for_gumloop = []
        for t in tools:
            func = t.get("function", t)
            tools_for_gumloop.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {}),
            })

    # Convert messages to simple role/content format
    raw_msgs = []
    for msg in filtered_msgs:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Handle tool role → user with tool_result format
        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            raw_msgs.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_call_id, "content": content}],
            })
            continue

        # Handle assistant with tool_calls
        tool_calls = msg.get("tool_calls")
        if role == "assistant" and tool_calls:
            parts = []
            if content:
                parts.append({"type": "text", "text": content})
            for tc in tool_calls:
                func = tc.get("function", {})
                parts.append({
                    "type": "tool_use",
                    "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                    "name": func.get("name", ""),
                    "input": json.loads(func.get("arguments", "{}")) if isinstance(func.get("arguments"), str) else func.get("arguments", {}),
                })
            raw_msgs.append({"role": "assistant", "content": parts})
            continue

        # Handle content arrays (may contain text + image_url blocks)
        images = []
        if isinstance(content, list):
            text_parts = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    text_parts.append(p.get("text", ""))
                elif p.get("type") == "image_url":
                    img_url = p.get("image_url", {})
                    url = img_url.get("url", "") if isinstance(img_url, dict) else str(img_url)
                    if url:
                        images.append(url)
            content = "\n".join(text_parts)

        if role == "assistant":
            raw_msgs.append({"role": "assistant", "content": content or ""})
        else:
            msg_entry: dict[str, Any] = {"role": "user", "content": content or ""}
            if images:
                msg_entry["_images"] = images  # Will be uploaded later
            raw_msgs.append(msg_entry)

    # Save _images before tool conversion (which strips unknown fields)
    images_by_content: dict[str, list[str]] = {}
    for msg in raw_msgs:
        imgs = msg.get("_images")
        if imgs and msg.get("content"):
            images_by_content[msg["content"]] = imgs

    # Use tool_converter to handle tool_use/tool_result blocks
    if tools_for_gumloop:
        converted = convert_messages_with_tools(
            raw_msgs,
            tools=[{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools_for_gumloop],
            system=system_prompt,
        )
        # system already embedded by convert_messages_with_tools
        # Re-inject _images
        for msg in converted:
            imgs = images_by_content.get(msg.get("content", ""))
            if imgs:
                msg["_images"] = imgs
        return converted, None, tools_for_gumloop
    else:
        converted = convert_messages_simple(raw_msgs)
        # Re-inject _images
        for msg in converted:
            imgs = images_by_content.get(msg.get("content", ""))
            if imgs:
                msg["_images"] = imgs
        return converted, system_prompt, None


async def proxy_chat_completions(
    body: dict,
    account: dict,
    client_wants_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    """Proxy chat completion to Gumloop via WebSocket.

    Returns (response, cost). Cost is always 0 (no credit tracking).
    """
    auth = _get_auth(account)
    await _ensure_turnstile_key()
    turnstile = _get_turnstile()
    gummie_id = account.get("gl_gummie_id", "")

    if not gummie_id:
        return JSONResponse(
            {"error": {"message": "Account has no gummie_id", "type": "server_error"}},
            status_code=503,
        ), 0.0

    # Map model
    raw_model = body.get("model", "gl-claude-sonnet-4-5")
    gl_model = _map_gl_model(raw_model)

    # Convert messages
    messages, system_prompt, tools_for_gumloop = _convert_openai_messages(body)
    has_tools = bool(tools_for_gumloop or body.get("tools"))

    # Upload images if any messages have _images
    interaction_id = str(uuid.uuid4()).replace("-", "")[:22]
    for msg in messages:
        image_urls = msg.pop("_images", None)
        if not image_urls or msg.get("role") != "user":
            continue

        gl_parts = []
        for img_url in image_urls:
            try:
                result = await _extract_image_data(img_url)
                if not result:
                    log.warning("Could not extract image data from URL")
                    continue
                img_data, media_type = result
                ext = _ext_from_media_type(media_type)
                file_info = await upload_file(
                    auth=auth,
                    file_data=img_data,
                    file_name=f"image.{ext}",
                    content_type=media_type,
                    interaction_id=interaction_id,
                    proxy_url=proxy_url,
                )
                part_id = f"part_{uuid.uuid4().hex[:20]}"
                gl_parts.append({
                    "id": part_id,
                    "type": "file",
                    "timestamp": __import__("time").strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "file": file_info,
                })
                log.info("Uploaded image: %s (%d bytes, %s) preview=%s", file_info["filename"], len(img_data), media_type, file_info.get("preview_url", "")[:80])
            except Exception as e:
                log.error("Image upload failed: %s", e, exc_info=True)

        if gl_parts:
            msg["_gl_parts"] = gl_parts
            log.info("Injected %d file parts into message (content=%s)", len(gl_parts), msg.get("content", "")[:50])

    # Validate messages
    if not messages:
        return JSONResponse(
            {"error": {"message": "No messages provided", "type": "invalid_request_error"}},
            status_code=400,
        ), 0.0

    # Check for tool loops
    if has_tools:
        loop_error = detect_tool_loop(messages)
        if loop_error:
            return JSONResponse(
                {"error": {"message": loop_error, "type": "invalid_request_error"}},
                status_code=400,
            ), 0.0

    # Update gummie config (model, system_prompt, tools)
    try:
        await update_gummie_config(
            gummie_id=gummie_id,
            auth=auth,
            system_prompt=system_prompt,
            tools=tools_for_gumloop,
            model_name=gl_model,
            proxy_url=proxy_url,
        )
    except Exception as e:
        log.warning("Failed to update gummie config: %s", e)
        # Continue anyway — fallback to text-based tools

    # Persist refreshed tokens
    from . import database as db
    updated = auth.get_updated_tokens()
    if updated.get("gl_id_token"):
        try:
            await db.update_account(account["id"], **updated)
        except Exception:
            pass

    stream_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    # Check if any message has images — use same interaction_id for WS chat
    has_images = any(m.get("_gl_parts") for m in messages)

    if client_wants_stream:
        return _stream_gumloop(
            gummie_id, messages, auth, turnstile, gl_model, raw_model,
            stream_id, created, has_tools, proxy_url,
            interaction_id=interaction_id if has_images else None,
        ), 0.0
    else:
        return await _accumulate_gumloop(
            gummie_id, messages, auth, turnstile, gl_model, raw_model,
            stream_id, created, has_tools, proxy_url,
            interaction_id=interaction_id if has_images else None,
        )


def _stream_gumloop(
    gummie_id: str,
    messages: list[dict],
    auth: GumloopAuth,
    turnstile: TurnstileSolver,
    gl_model: str,
    display_model: str,
    stream_id: str,
    created: int,
    has_tools: bool,
    proxy_url: str | None,
    interaction_id: str | None = None,
) -> StreamingResponse:
    """Stream Gumloop response as OpenAI SSE chunks."""
    _stream_state = {
        "cost": 0.0,
        "content": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "done": False,
    }

    async def stream_sse() -> AsyncIterator[bytes]:
        try:
            handler = GumloopStreamHandler(model=gl_model)
            first_chunk = True
            full_text = ""

            async for event in send_chat(
                gummie_id, messages, auth, turnstile,
                interaction_id=interaction_id, proxy_url=proxy_url,
            ):
                ev = handler.handle_event(event)
                ev_type = ev.get("type")

                if ev_type == "text_delta" and ev.get("delta"):
                    full_text += ev["delta"]
                    role_arg = "assistant" if first_chunk else None
                    yield build_openai_chunk(
                        stream_id, display_model,
                        content=ev["delta"], role=role_arg, created=created,
                    ).encode()
                    first_chunk = False

                elif ev_type == "reasoning_delta" and ev.get("delta"):
                    # Emit reasoning as content (OpenAI format doesn't have separate reasoning)
                    full_text += ev["delta"]
                    role_arg = "assistant" if first_chunk else None
                    yield build_openai_chunk(
                        stream_id, display_model,
                        content=ev["delta"], role=role_arg, created=created,
                    ).encode()
                    first_chunk = False

                elif ev_type == "tool_result" and ev.get("result"):
                    # Tool result (e.g., sandbox_python output) — emit as content
                    result_text = str(ev["result"])
                    if result_text:
                        full_text += result_text
                        role_arg = "assistant" if first_chunk else None
                        yield build_openai_chunk(
                            stream_id, display_model,
                            content=result_text, role=role_arg, created=created,
                        ).encode()
                        first_chunk = False

                elif ev_type == "finish":
                    is_final = ev.get("final", True)
                    usage = ev.get("usage", {})
                    _stream_state["prompt_tokens"] += usage.get("input_tokens", 0)
                    _stream_state["completion_tokens"] += usage.get("output_tokens", 0)
                    _stream_state["total_tokens"] += usage.get("total_tokens", 0)

                    if not is_final:
                        # Multi-step: more events coming (tool calls)
                        continue

                    # Parse tool calls from response if tools enabled
                    finish_reason = "stop"
                    if has_tools:
                        remaining_text, tool_uses = parse_tool_calls(full_text)
                        if tool_uses:
                            finish_reason = "tool_calls"
                            for i, tu in enumerate(tool_uses):
                                yield build_openai_tool_call_chunk(
                                    stream_id, display_model, i,
                                    tu["id"], tu["name"],
                                    json.dumps(tu["input"], ensure_ascii=False),
                                    created=created,
                                ).encode()

                    yield build_openai_chunk(
                        stream_id, display_model,
                        finish_reason=finish_reason, created=created,
                        usage={
                            "prompt_tokens": _stream_state["prompt_tokens"],
                            "completion_tokens": _stream_state["completion_tokens"],
                            "total_tokens": _stream_state["total_tokens"],
                        },
                    ).encode()
                    yield build_openai_done().encode()
                    _stream_state["content"] = full_text
                    _stream_state["done"] = True
                    break

        except Exception as e:
            log.error("Gumloop streaming error: %s", e, exc_info=True)
            err = {"error": {"message": str(e) or "Stream error", "type": "proxy_error"}}
            yield f"data: {json.dumps(err)}\n\n".encode()
            yield b"data: [DONE]\n\n"
            _stream_state["done"] = True

    resp = StreamingResponse(
        stream_sse(),
        status_code=200,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    resp._gl_stream_state = _stream_state  # type: ignore[attr-defined]
    return resp


async def _accumulate_gumloop(
    gummie_id: str,
    messages: list[dict],
    auth: GumloopAuth,
    turnstile: TurnstileSolver,
    gl_model: str,
    display_model: str,
    stream_id: str,
    created: int,
    has_tools: bool,
    proxy_url: str | None,
    interaction_id: str | None = None,
) -> tuple[JSONResponse, float]:
    """Accumulate Gumloop response into OpenAI chat.completion JSON."""
    try:
        handler = GumloopStreamHandler(model=gl_model)
        async for event in send_chat(
            gummie_id, messages, auth, turnstile,
            interaction_id=interaction_id, proxy_url=proxy_url,
        ):
            handler.handle_event(event)

        full_text = handler.get_full_text()
        # If no text output but has reasoning, include reasoning as content
        if not full_text and handler.get_full_reasoning():
            full_text = handler.get_full_reasoning()
        finish_reason = "stop"
        message: dict = {"role": "assistant", "content": full_text}

        # Parse tool calls
        if has_tools:
            remaining_text, tool_uses = parse_tool_calls(full_text)
            if tool_uses:
                finish_reason = "tool_calls"
                message["content"] = remaining_text or None
                message["tool_calls"] = [
                    {
                        "id": tu["id"],
                        "type": "function",
                        "function": {
                            "name": tu["name"],
                            "arguments": json.dumps(tu["input"], ensure_ascii=False),
                        },
                    }
                    for tu in tool_uses
                ]

        response = {
            "id": stream_id,
            "object": "chat.completion",
            "created": created,
            "model": display_model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens": handler.input_tokens,
                "completion_tokens": handler.output_tokens,
                "total_tokens": handler.total_tokens,
            },
        }
        return JSONResponse(response, status_code=200), 0.0

    except Exception as e:
        log.error("Gumloop non-streaming error: %s", e, exc_info=True)
        return JSONResponse(
            {"error": {"message": f"Gumloop error: {e}", "type": "proxy_error"}},
            status_code=502,
        ), 0.0


def get_captcha_stats() -> dict:
    """Return captcha solve stats for dashboard display."""
    if _turnstile is None:
        return {"solved": 0, "errors": 0, "has_key": False}
    return {
        "solved": _turnstile.solve_count,
        "errors": _turnstile.solve_errors,
        "has_key": bool(_turnstile._api_key),
    }


async def close_all_clients() -> None:
    """Cleanup auth cache and turnstile solver."""
    global _auth_cache, _turnstile
    _auth_cache.clear()
    if _turnstile:
        _turnstile.close()
        _turnstile = None
