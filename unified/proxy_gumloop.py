"""Gumloop proxy — WebSocket chat with per-account auth and Turnstile captcha.

Routes gl-* model requests through Gumloop's WebSocket API.
Each account has its own GumloopAuth instance. Turnstile solver is shared.

MCP Mode: Agent uses MCP tools server-side for file operations.
Client tools from OpenCode are stripped — Gumloop handles everything via MCP.
All tool events (tool-call, tool-result) are streamed as text content so
the client can see what the agent is doing.
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
)
import base64
import re
import httpx as _httpx

from .gumloop.parser import build_openai_chunk, build_openai_done

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
        db_token = account.get("gl_id_token", "")
        db_refresh = account.get("gl_refresh_token", "")
        if db_refresh and db_refresh != auth.refresh_token:
            auth.refresh_token = db_refresh
        if db_token and db_token != auth.id_token:
            auth.id_token = db_token
            auth.expires_at = 0
        return auth

    auth = GumloopAuth(
        refresh_token=account.get("gl_refresh_token", ""),
        user_id=account.get("gl_user_id", ""),
        id_token=account.get("gl_id_token", ""),
    )
    _auth_cache[acct_id] = auth
    return auth


def _map_gl_model(model: str) -> str:
    """Map gl-prefixed model to Gumloop's internal name."""
    bare = model.removeprefix("gl-")
    if any(x in bare for x in ("claude", "haiku", "sonnet", "opus")):
        bare = bare.replace(".", "-")
    if bare in GUMLOOP_MODELS:
        return bare
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
    """Extract image bytes from OpenAI image_url format."""
    if image_url.startswith("data:"):
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
                ct = resp.headers.get("content-type", "")
                media_type = ct.split(";")[0].strip() if ct else _detect_media_type(data)
                return data, media_type
        except Exception as e:
            log.warning("Failed to download image from %s: %s", image_url[:80], e)
            return None

    return None


def _convert_openai_messages_simple(body: dict) -> tuple[list[dict], str | None]:
    """Convert OpenAI messages to simple role/content format for Gumloop.

    Strips tools entirely — Gumloop uses MCP tools server-side.
    Converts tool role messages and tool_calls to plain text so conversation
    history is preserved even if client sent tool interactions.

    Returns (messages, system_prompt).
    """
    messages = body.get("messages", [])

    system_prompt = None
    result = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # System messages → extract as system prompt
        if role == "system":
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            system_prompt = (system_prompt + "\n" + content) if system_prompt else content
            continue

        # Tool role (tool results from client) → convert to user message
        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            tool_text = f"[Tool result for {tool_call_id}]: {content}" if content else ""
            if tool_text:
                result.append({"role": "user", "content": tool_text})
            continue

        # Assistant with tool_calls → convert to plain text
        tool_calls = msg.get("tool_calls")
        if role == "assistant" and tool_calls:
            parts = []
            if content:
                parts.append(content if isinstance(content, str) else str(content))
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "?")
                args = func.get("arguments", "{}")
                parts.append(f"[Called tool: {name}({args})]")
            result.append({"role": "assistant", "content": "\n".join(parts)})
            continue

        # Handle content arrays (text + image_url blocks)
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

        msg_entry = {"role": role, "content": content or ""}
        if images:
            msg_entry["_images"] = images
        result.append(msg_entry)

    # Merge consecutive same-role messages (Gumloop requires strict alternation)
    merged = []
    for msg in result:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n\n" + msg["content"]
            # Merge images if any
            if "_images" in msg:
                merged[-1].setdefault("_images", []).extend(msg["_images"])
        else:
            merged.append(msg)

    return merged, system_prompt


async def proxy_chat_completions(
    body: dict,
    account: dict,
    client_wants_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    """Proxy chat completion to Gumloop via WebSocket.

    MCP mode: strips client tools, agent uses MCP tools server-side.
    All tool activity is streamed as text content.
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

    # Convert messages — strip tools, simple format
    messages, system_prompt = _convert_openai_messages_simple(body)

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
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "file": file_info,
                })
            except Exception as e:
                log.error("Image upload failed: %s", e, exc_info=True)

        if gl_parts:
            msg["_gl_parts"] = gl_parts

    if not messages:
        return JSONResponse(
            {"error": {"message": "No messages provided", "type": "invalid_request_error"}},
            status_code=400,
        ), 0.0

    # Update gummie config — model + system prompt only, NO tools
    try:
        await update_gummie_config(
            gummie_id=gummie_id,
            auth=auth,
            system_prompt=system_prompt,
            tools=None,  # Don't touch tools — MCP handles them
            model_name=gl_model,
            proxy_url=proxy_url,
        )
    except Exception as e:
        log.warning("Failed to update gummie config: %s", e)

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
    has_images = any(m.get("_gl_parts") for m in messages)

    if client_wants_stream:
        return _stream_gumloop(
            gummie_id, messages, auth, turnstile, gl_model, raw_model,
            stream_id, created, proxy_url,
            interaction_id=interaction_id if has_images else None,
        ), 0.0
    else:
        return await _accumulate_gumloop(
            gummie_id, messages, auth, turnstile, gl_model, raw_model,
            stream_id, created, proxy_url,
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
    proxy_url: str | None,
    interaction_id: str | None = None,
) -> StreamingResponse:
    """Stream Gumloop response as OpenAI SSE chunks.

    All WS events (text, reasoning, tool-call, tool-result) are streamed
    as text content. Multi-step tool loops continue until final finish.
    """
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
            first_chunk = True
            full_text = ""

            def emit_text(text: str) -> bytes:
                nonlocal first_chunk, full_text
                if not text:
                    return b""
                full_text += text
                role_arg = "assistant" if first_chunk else None
                first_chunk = False
                return build_openai_chunk(
                    stream_id, display_model,
                    content=text, role=role_arg, created=created,
                ).encode()

            async for event in send_chat(
                gummie_id, messages, auth, turnstile,
                interaction_id=interaction_id, proxy_url=proxy_url,
            ):
                etype = event.get("type", "")

                # ── Text content ──
                if etype == "text-delta":
                    delta = event.get("delta", "")
                    if delta:
                        chunk = emit_text(delta)
                        if chunk:
                            yield chunk

                # ── Reasoning (stream as text) ──
                elif etype == "reasoning-delta":
                    delta = event.get("delta", "")
                    if delta:
                        chunk = emit_text(delta)
                        if chunk:
                            yield chunk

                # ── Tool call started (show what agent is doing) ──
                elif etype == "tool-call":
                    tool_name = event.get("toolName", "?")
                    tool_input = event.get("input", {})
                    # Format tool call as visible text
                    input_preview = json.dumps(tool_input, ensure_ascii=False)
                    if len(input_preview) > 200:
                        input_preview = input_preview[:200] + "..."
                    # Don't emit tool-call text — wait for result
                    log.info("[GL stream] tool-call: %s(%s)", tool_name, input_preview[:100])

                # ── Tool result (show output) ──
                elif etype == "tool-result":
                    tool_name = event.get("toolName", "?")
                    output = event.get("output", "")
                    # Format output
                    if isinstance(output, dict):
                        # Sandbox tools have stdout/stderr
                        stdout = output.get("stdout", "")
                        stderr = output.get("stderr", "")
                        result_text = stdout or stderr or json.dumps(output, ensure_ascii=False)
                    elif isinstance(output, str):
                        result_text = output
                    else:
                        result_text = str(output)
                    # Don't emit raw tool results as text — agent will summarize
                    log.info("[GL stream] tool-result: %s → %s", tool_name, result_text[:100])

                # ── Finish ──
                elif etype == "finish":
                    is_final = event.get("final", True)
                    usage = event.get("usage", {})
                    _stream_state["prompt_tokens"] += usage.get("input_tokens", 0)
                    _stream_state["completion_tokens"] += usage.get("output_tokens", 0)
                    _stream_state["total_tokens"] += usage.get("total_tokens", 0)

                    if not is_final:
                        # Multi-step: agent is executing tools, more coming
                        continue

                    # Final finish — close the stream
                    yield build_openai_chunk(
                        stream_id, display_model,
                        finish_reason="stop", created=created,
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

                # Ignore other events (step-start, keepalive, interaction-name-update, etc.)

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
    proxy_url: str | None,
    interaction_id: str | None = None,
) -> tuple[JSONResponse, float]:
    """Accumulate Gumloop response into OpenAI chat.completion JSON."""
    try:
        full_text = []
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0

        async for event in send_chat(
            gummie_id, messages, auth, turnstile,
            interaction_id=interaction_id, proxy_url=proxy_url,
        ):
            etype = event.get("type", "")

            if etype == "text-delta":
                delta = event.get("delta", "")
                if delta:
                    full_text.append(delta)

            elif etype == "reasoning-delta":
                delta = event.get("delta", "")
                if delta:
                    full_text.append(delta)

            elif etype == "finish":
                usage = event.get("usage", {})
                prompt_tokens += usage.get("input_tokens", 0)
                completion_tokens += usage.get("output_tokens", 0)
                total_tokens += usage.get("total_tokens", 0)
                if event.get("final", True):
                    break

        content = "".join(full_text)
        response = {
            "id": stream_id,
            "object": "chat.completion",
            "created": created,
            "model": display_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
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
