"""Windsurf proxy — forwards to WindsurfAPI sidecar (Node.js, localhost:3003)."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import AsyncIterator

import httpx
from fastapi.responses import StreamingResponse, JSONResponse

from .config import WINDSURF_UPSTREAM, WINDSURF_INTERNAL_KEY, WINDSURF_SIDECAR_PORT

log = logging.getLogger("unified.proxy_windsurf")

WINDSURF_CHAT_URL = f"{WINDSURF_UPSTREAM}/v1/chat/completions"

# Client pool keyed by proxy URL
_clients: dict[str, httpx.AsyncClient] = {}


async def get_client(proxy_url: str | None = None) -> httpx.AsyncClient:
    key = proxy_url or "__direct__"
    if key not in _clients:
        _clients[key] = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=600, write=30, pool=10),
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


def _strip_prefix(model: str) -> str:
    """Strip 'windsurf-' prefix from model name for sidecar."""
    if model.startswith("windsurf-"):
        return model[len("windsurf-"):]
    return model


def _build_headers(windsurf_api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {WINDSURF_INTERNAL_KEY}",
        "X-Windsurf-Account-Key": windsurf_api_key,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


async def proxy_chat_completions(
    body: dict,
    windsurf_api_key: str,
    client_wants_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    """Proxy a chat completion request to Windsurf sidecar.

    Returns (response, credit_used) tuple.
    NOTE: Always connects directly to localhost sidecar — never via outbound proxy.
    The sidecar handles its own upstream proxy if needed.
    """
    client = await get_client(None)  # Always direct — sidecar is localhost
    headers = _build_headers(windsurf_api_key)

    # Strip windsurf- prefix from model name
    if "model" in body:
        body["model"] = _strip_prefix(body["model"])

    # Ensure stream matches client preference
    body["stream"] = True if client_wants_stream else body.get("stream", False)

    try:
        upstream_req = client.build_request(
            method="POST",
            url=WINDSURF_CHAT_URL,
            json=body,
            headers=headers,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.ConnectError:
        log.error("Cannot connect to Windsurf sidecar at %s", WINDSURF_CHAT_URL)
        return JSONResponse(
            {"error": {"message": "Windsurf sidecar unavailable", "type": "proxy_error"}},
            status_code=502,
        ), 0.0
    except httpx.TimeoutException:
        log.error("Timeout connecting to Windsurf sidecar")
        return JSONResponse(
            {"error": {"message": "Windsurf sidecar timeout", "type": "proxy_error"}},
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
        stream_state = {
            "done": False,
            "content": "",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
        }
        resp = _stream_passthrough(upstream_resp, stream_state)
        resp._ws_stream_state = stream_state
        return resp, 0.0
    else:
        resp, credit = await _accumulate_response(upstream_resp, body.get("model", ""))
        return resp, credit


def _stream_passthrough(upstream_resp: httpx.Response, stream_state: dict) -> StreamingResponse:
    """Pass SSE stream through to the client, capturing content and usage."""

    async def stream_sse() -> AsyncIterator[bytes]:
        collected_content = []
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
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            collected_content.append(content)
                    usage = chunk.get("usage") or {}
                    if usage.get("total_tokens", 0) > 0:
                        stream_state["prompt_tokens"] = usage.get("prompt_tokens", 0)
                        stream_state["completion_tokens"] = usage.get("completion_tokens", 0)
                        stream_state["total_tokens"] = usage.get("total_tokens", 0)
                        stream_state["cost"] = float(usage.get("credit", 0) or 0)
                except (json.JSONDecodeError, ValueError):
                    pass
        finally:
            await upstream_resp.aclose()
            stream_state["content"] = "".join(collected_content)
            stream_state["done"] = True

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
    """Accumulate SSE chunks into a single ChatCompletion JSON response."""
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
                if "id" in chunk:
                    completion_id = chunk["id"]
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
        credit_used = 1.0  # fallback

    log.info("Windsurf credit used: %.4f (tokens: %d)", credit_used, usage_data.get("total_tokens", 0))
    return JSONResponse(response, status_code=200), credit_used
