"""Direct proxy to CodeBuddy — SSE streaming with stream:true enforcement."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import AsyncIterator

import httpx
from fastapi.responses import StreamingResponse, JSONResponse

from .config import CODEBUDDY_UPSTREAM

log = logging.getLogger("unified.proxy_codebuddy")

CODEBUDDY_CHAT_URL = f"{CODEBUDDY_UPSTREAM}/v2/chat/completions"

# Client pool keyed by proxy URL
_clients: dict[str, httpx.AsyncClient] = {}


async def get_client(proxy_url: str | None = None) -> httpx.AsyncClient:
    key = proxy_url or "__direct__"
    if key not in _clients:
        _clients[key] = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=300, write=30, pool=10),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            follow_redirects=True,
            proxy=proxy_url,
        )
    return _clients[key]


async def close_all_clients() -> None:
    global _clients
    for c in _clients.values():
        await c.aclose()
    _clients.clear()


# Model name mapping: our alias → CodeBuddy's actual model name
_CB_MODEL_MAP = {
    "deepseek-v3-2-volc": "deepseek-v3.2",
}


def _map_model(model: str) -> str:
    """Map our model alias to CodeBuddy's actual model name."""
    return _CB_MODEL_MAP.get(model, model)


def _build_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "CLI/1.0.7 CodeBuddy/1.0.7",
        "X-Domain": "www.codebuddy.ai",
        "X-Product": "SaaS",
        "X-IDE-Type": "CLI",
        "X-Agent-Intent": "craft",
        "Accept": "text/event-stream",
    }


# Shared state to capture credit and response data from streaming responses
_last_credit_used: dict[str, float] = {}  # keyed by request id
_last_stream_data: dict[str, dict] = {}  # keyed by request id: {body, tokens, credit}


async def proxy_chat_completions(
    body: dict,
    api_key: str,
    client_wants_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    """Proxy a chat completion request to CodeBuddy.

    Returns (response, credit_used) tuple.
    credit_used is the actual credit cost from CodeBuddy's usage.credit field.

    IMPORTANT: CodeBuddy only supports stream:true.
    If the client sends stream:false, we set stream:true internally,
    accumulate the full response, then return it as a non-streaming JSON response.
    """
    client = await get_client(proxy_url)
    headers = _build_headers(api_key)

    # Map model name to CodeBuddy's actual name
    if "model" in body:
        body["model"] = _map_model(body["model"])

    # Always send stream:true to CodeBuddy
    body["stream"] = True

    # CodeBuddy requires max_output_tokens >= 100 (rejects lower values)
    for key in ("max_tokens", "max_output_tokens"):
        if key in body and isinstance(body[key], (int, float)) and body[key] < 100:
            body[key] = 100

    req_id = str(uuid.uuid4().hex[:12])

    try:
        upstream_req = client.build_request(
            method="POST",
            url=CODEBUDDY_CHAT_URL,
            json=body,
            headers=headers,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.ConnectError:
        log.error("Cannot connect to CodeBuddy at %s", CODEBUDDY_CHAT_URL)
        return JSONResponse(
            {"error": {"message": "CodeBuddy upstream unavailable", "type": "proxy_error"}},
            status_code=502,
        ), 0.0
    except httpx.TimeoutException:
        log.error("Timeout connecting to CodeBuddy")
        return JSONResponse(
            {"error": {"message": "CodeBuddy upstream timeout", "type": "proxy_error"}},
            status_code=504,
        ), 0.0

    # Check for upstream error (non-2xx)
    if upstream_resp.status_code >= 400:
        error_body = await upstream_resp.aread()
        await upstream_resp.aclose()
        try:
            error_json = json.loads(error_body)
        except (json.JSONDecodeError, ValueError):
            error_json = {"error": {"message": error_body.decode(errors="replace"), "type": "upstream_error"}}
        return JSONResponse(error_json, status_code=upstream_resp.status_code), 0.0

    if client_wants_stream:
        # SSE passthrough — credit and content captured async in _last_stream_data[req_id]
        _last_credit_used[req_id] = 0.0
        _last_stream_data[req_id] = {}
        resp = _stream_passthrough(upstream_resp, req_id)
        resp._cb_req_id = req_id  # attach req_id for post-stream logging
        return resp, 1.0  # fallback; real credit in _last_stream_data after stream ends
    else:
        # Accumulate SSE → single JSON response, extract exact credit
        resp, credit = await _accumulate_response(upstream_resp, body.get("model", ""))
        return resp, credit


def get_stream_data(req_id: str) -> dict:
    """Get captured stream data (content, tokens, credit) after stream completes."""
    return _last_stream_data.pop(req_id, {})


def get_stream_credit(req_id: str) -> float:
    """Get captured credit from a completed stream."""
    return _last_credit_used.pop(req_id, 0.0)


def _stream_passthrough(upstream_resp: httpx.Response, req_id: str = "") -> StreamingResponse:
    """Pass SSE stream through to the client, capturing credit and content from final chunk."""

    async def stream_sse() -> AsyncIterator[bytes]:
        collected_content = []
        last_usage = {}
        try:
            async for line in upstream_resp.aiter_lines():
                yield f"{line}\n".encode()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data_str)
                    # Capture content deltas
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            collected_content.append(content)
                    # Capture usage from final chunk
                    usage = chunk.get("usage") or {}
                    if usage.get("total_tokens", 0) > 0:
                        last_usage = usage
                        credit = float(usage.get("credit", 0) or 0)
                        if credit > 0 and req_id:
                            _last_credit_used[req_id] = credit
                except (json.JSONDecodeError, ValueError):
                    pass
        finally:
            await upstream_resp.aclose()
            # Store captured data for logging
            if req_id:
                full_text = "".join(collected_content)
                _last_stream_data[req_id] = {
                    "content": full_text[:2000],
                    "prompt_tokens": last_usage.get("prompt_tokens", 0),
                    "completion_tokens": last_usage.get("completion_tokens", 0),
                    "total_tokens": last_usage.get("total_tokens", 0),
                    "credit": float(last_usage.get("credit", 0) or 0),
                }

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


async def _accumulate_response(
    upstream_resp: httpx.Response,
    model: str,
) -> tuple[JSONResponse, float]:
    """Accumulate SSE chunks into a single ChatCompletion JSON response.
    Returns (response, credit_used) tuple.
    """
    collected_content = []
    finish_reason = "stop"
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    role = "assistant"
    credit_used = 0.0
    usage_data = {}

    try:
        async for line in upstream_resp.aiter_lines():
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    if "content" in delta and delta["content"]:
                        collected_content.append(delta["content"])
                    if "role" in delta:
                        role = delta["role"]
                    fr = choices[0].get("finish_reason")
                    if fr:
                        finish_reason = fr
                # Capture id if present
                if "id" in chunk:
                    completion_id = chunk["id"]
                # Extract credit from usage (last chunk has the real values)
                usage = chunk.get("usage") or {}
                if usage.get("total_tokens", 0) > 0:
                    usage_data = usage
                    credit_used = float(usage.get("credit", 0) or 0)
            except (json.JSONDecodeError, KeyError):
                continue
    finally:
        await upstream_resp.aclose()

    full_content = "".join(collected_content)

    response = {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": role,
                    "content": full_content,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": usage_data.get("prompt_tokens", 0),
            "completion_tokens": usage_data.get("completion_tokens", 0),
            "total_tokens": usage_data.get("total_tokens", 0),
        },
    }

    if credit_used <= 0:
        credit_used = 1.0  # fallback: 1 credit if not reported

    log.info("CB credit used: %.4f (tokens: %d)", credit_used, usage_data.get("total_tokens", 0))
    return JSONResponse(response, status_code=200), credit_used
