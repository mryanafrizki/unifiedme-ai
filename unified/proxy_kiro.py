"""Direct Kiro API proxy — replaces Kiro-Go binary.

Handles the full Kiro API flow:
1. Accept OpenAI-format request
2. Create KiroAuthManager from DB-stored tokens
3. Convert OpenAI -> Kiro format (converters_openai)
4. Send to AWS Q API (https://q.{region}.amazonaws.com)
5. Parse AWS event stream response
6. Convert back to OpenAI SSE format
7. Stream to client
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse, JSONResponse

from .kiro.auth import KiroAuthManager
from .kiro.http_client import KiroHttpClient
from .kiro.converters_openai import build_kiro_payload
from .kiro.models_openai import ChatCompletionRequest, ChatMessage
from .kiro.streaming_openai import collect_stream_response
from .kiro.utils import generate_conversation_id
from .kiro.config import DEFAULT_REGION

log = logging.getLogger("unified.proxy_kiro")

# Shared httpx client for non-streaming requests
_shared_client: Optional[httpx.AsyncClient] = None


async def _get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout=300.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _shared_client


async def close_all_clients() -> None:
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None


def _create_auth_manager(account: dict) -> KiroAuthManager:
    """Create a KiroAuthManager from DB account data."""
    # Extract region from profile_arn if available
    region = DEFAULT_REGION
    profile_arn = account.get("kiro_profile_arn", "")
    if profile_arn:
        parts = profile_arn.split(":")
        if len(parts) >= 4 and parts[3]:
            import re
            if re.match(r'^[a-z]+-[a-z]+-\d+$', parts[3]):
                region = parts[3]

    return KiroAuthManager(
        access_token=account.get("kiro_access_token", ""),
        refresh_token=account.get("kiro_refresh_token", ""),
        profile_arn=profile_arn,
        region=region,
    )


async def proxy_chat_completions(
    request: Request,
    body: bytes,
    account: dict,
    is_stream: bool = True,
    proxy_url: str | None = None,
) -> StreamingResponse | JSONResponse:
    """Handle /v1/chat/completions via direct Kiro API call.

    Args:
        request: FastAPI request
        body: Raw request body (JSON bytes, already filtered)
        account: DB account dict with kiro_access_token, kiro_refresh_token, kiro_profile_arn
        is_stream: Whether client wants streaming
        proxy_url: Optional outbound proxy URL
    """
    # Parse request body
    try:
        body_dict = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
            status_code=400,
        )

    # Create auth manager from account tokens
    auth_mgr = _create_auth_manager(account)

    # Build Kiro API payload from OpenAI request
    try:
        # Convert to pydantic model for the converter
        messages = [ChatMessage(**m) if isinstance(m, dict) else m for m in body_dict.get("messages", [])]
        chat_request = ChatCompletionRequest(
            model=body_dict.get("model", "auto"),
            messages=messages,
            stream=is_stream,
            tools=body_dict.get("tools"),
            temperature=body_dict.get("temperature"),
            max_tokens=body_dict.get("max_tokens"),
            max_completion_tokens=body_dict.get("max_completion_tokens"),
            reasoning_effort=body_dict.get("reasoning_effort"),
        )

        conversation_id = generate_conversation_id(body_dict.get("messages"))
        profile_arn = account.get("kiro_profile_arn", "")

        kiro_payload = build_kiro_payload(chat_request, conversation_id, profile_arn)
    except ValueError as e:
        return JSONResponse(
            {"error": {"message": str(e), "type": "invalid_request_error"}},
            status_code=400,
        )
    except Exception as e:
        log.error("Failed to build Kiro payload: %s", e)
        return JSONResponse(
            {"error": {"message": f"Payload conversion error: {e}", "type": "proxy_error"}},
            status_code=500,
        )

    # Build Kiro API URL
    api_url = f"{auth_mgr.api_host}/generateAssistantResponse"

    # Create HTTP client with retry logic
    http_client = KiroHttpClient(auth_manager=auth_mgr)

    try:
        if is_stream:
            # Streaming: DON'T close http_client here — the stream_sse generator
            # handles cleanup in its finally block after the stream is consumed.
            return await _handle_streaming(
                http_client, auth_mgr, api_url, kiro_payload,
                body_dict.get("model", "auto"), body_dict.get("messages"),
                body_dict.get("tools"),
            )
        else:
            return await _handle_non_streaming(
                http_client, auth_mgr, api_url, kiro_payload,
                body_dict.get("model", "auto"), body_dict.get("messages"),
                body_dict.get("tools"),
            )
    except httpx.RequestError as e:
        log.error("Kiro API request failed: %s", e)
        await http_client.close()
        return JSONResponse(
            {"error": {"message": f"Kiro API unavailable: {e}", "type": "proxy_error"}},
            status_code=502,
        )


async def _handle_streaming(
    http_client: KiroHttpClient,
    auth_mgr: KiroAuthManager,
    api_url: str,
    kiro_payload: dict,
    model: str,
    request_messages: list | None = None,
    request_tools: list | None = None,
) -> StreamingResponse:
    """Handle streaming response from Kiro API.

    Uses parse_kiro_stream directly for simplicity — converts AWS event
    stream to OpenAI SSE chunks inline.
    """
    import time as _time
    from .kiro.streaming_core import parse_kiro_stream, FirstTokenTimeoutError
    from .kiro.utils import generate_completion_id

    response = await http_client.request_with_retry(
        "POST", api_url, json_data=kiro_payload, stream=True
    )

    if response.status_code != 200:
        error_body = await response.aread()
        await response.aclose()
        error_text = error_body.decode("utf-8", errors="replace")
        log.error("Kiro API error: %d %s", response.status_code, error_text[:500])
        return JSONResponse(
            {"error": {"message": f"Kiro API error: {error_text[:500]}", "type": "upstream_error"}},
            status_code=response.status_code,
        )

    completion_id = generate_completion_id()
    created_time = int(_time.time())

    async def stream_sse() -> AsyncIterator[bytes]:
        first_chunk = True
        full_content = ""
        context_usage_pct = None

        try:
            async for event in parse_kiro_stream(response):
                if event.type == "content" and event.content:
                    full_content += event.content
                    delta: dict = {"content": event.content}
                    if first_chunk:
                        delta["role"] = "assistant"
                        first_chunk = False

                    chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

                elif event.type == "tool_use" and event.tool_use:
                    tc = event.tool_use
                    func = tc.get("function") or {}
                    indexed_tc = {
                        "index": 0,
                        "id": tc.get("id"),
                        "type": tc.get("type", "function"),
                        "function": {"name": func.get("name", ""), "arguments": func.get("arguments", "{}")},
                    }
                    chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"tool_calls": [indexed_tc]}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

                elif event.type == "context_usage" and event.context_usage_percentage is not None:
                    context_usage_pct = event.context_usage_percentage

            # Estimate tokens
            completion_tokens = len(full_content) // 4
            prompt_tokens = 0
            if context_usage_pct and context_usage_pct > 0:
                total = int((context_usage_pct / 100) * 200_000)
                prompt_tokens = max(0, total - completion_tokens)
            total_tokens = prompt_tokens + completion_tokens

            # Final chunk
            final = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total_tokens},
            }
            yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        except FirstTokenTimeoutError:
            log.warning("First token timeout during streaming")
            err = {"error": {"message": "Model did not respond in time", "type": "timeout"}}
            yield f"data: {json.dumps(err)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        except Exception as e:
            log.error("Streaming error [%s]: %s", type(e).__name__, e, exc_info=True)
            err = {"error": {"message": str(e) or "Stream error", "type": "proxy_error"}}
            yield f"data: {json.dumps(err)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        finally:
            try:
                await response.aclose()
            except Exception:
                pass
            await http_client.close()

    return StreamingResponse(
        stream_sse(),
        status_code=200,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _handle_non_streaming(
    http_client: KiroHttpClient,
    auth_mgr: KiroAuthManager,
    api_url: str,
    kiro_payload: dict,
    model: str,
    request_messages: list | None = None,
    request_tools: list | None = None,
) -> JSONResponse:
    """Handle non-streaming response from Kiro API."""
    response = await http_client.request_with_retry(
        "POST", api_url, json_data=kiro_payload, stream=True
    )

    if response.status_code != 200:
        error_body = await response.aread()
        await response.aclose()
        error_text = error_body.decode("utf-8", errors="replace")
        log.error("Kiro API error: %d %s", response.status_code, error_text[:500])
        return JSONResponse(
            {"error": {"message": f"Kiro API error: {error_text[:500]}", "type": "upstream_error"}},
            status_code=response.status_code,
        )

    client = await http_client._get_client(stream=True)

    try:
        result = await collect_stream_response(
            client=client,
            response=response,
            model=model,
            auth_manager=auth_mgr,
            request_messages=request_messages,
            request_tools=request_tools,
        )
        return JSONResponse(result, status_code=200)
    except Exception as e:
        log.error("Non-streaming collection error: %s", e)
        return JSONResponse(
            {"error": {"message": f"Response processing error: {e}", "type": "proxy_error"}},
            status_code=502,
        )


async def proxy_messages(
    request: Request,
    body: bytes,
    account: dict,
    proxy_url: str | None = None,
) -> StreamingResponse | JSONResponse:
    """Handle /v1/messages (Anthropic format) — convert to OpenAI and proxy.

    For now, forward as chat/completions since Kiro API only supports one format.
    """
    # Parse body to extract model
    try:
        body_dict = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
            status_code=400,
        )

    # Anthropic /v1/messages always streams by default
    is_stream = body_dict.get("stream", True)

    return await proxy_chat_completions(
        request=request,
        body=body,
        account=account,
        is_stream=is_stream,
        proxy_url=proxy_url,
    )
