"""Direct proxy to ChatBAI (api.b.ai) — OpenAI-compatible passthrough.

ChatBAI's API at https://api.b.ai/v1 is OpenAI-compatible.
Auth: x-api-key header with the account's API key.
Credit tracking: estimate from token counts (no usage.cost from provider).
"""

from __future__ import annotations

import json
import logging
import re
from typing import AsyncIterator

import httpx
from fastapi.responses import StreamingResponse, JSONResponse

from ..config import CHATBAI_UPSTREAM

log = logging.getLogger("unified.chatbai.proxy")

CHATBAI_CHAT_URL = f"{CHATBAI_UPSTREAM}/v1/chat/completions"

# ChatBAI pricing per 1M tokens (input, output) in USD — estimated
_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-5.5": (10.0, 40.0),
    "gpt-5.4": (2.5, 15.0),
    "gpt-5.4-pro": (30.0, 180.0),
    # Anthropic
    "claude-opus-4.7": (5.0, 25.0),
    "claude-opus-4.6": (4.5, 22.5),
    "claude-opus-4.5": (4.5, 22.5),
    "claude-sonnet-4.6": (2.7, 13.5),
    "claude-sonnet-4.5": (2.7, 13.5),
    # DeepSeek
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": (0.9, 3.5),
}
_DEFAULT_PRICING = (3.0, 15.0)


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate USD cost from token counts."""
    input_rate, output_rate = _PRICING.get(model, _DEFAULT_PRICING)
    return round((prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000, 6)


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


def _build_headers(api_key: str) -> dict[str, str]:
    """ChatBAI uses x-api-key header (not Bearer)."""
    return {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }


def _map_model(model: str) -> str:
    """Strip bchatai- prefix to get the upstream model name."""
    if model.startswith("bchatai-"):
        return model[8:]  # len("bchatai-") == 8
    return model


_TOOL_ID_RE = re.compile(r'[^a-zA-Z0-9_-]')


def _sanitize_tool_ids(body: dict) -> None:
    """Sanitize tool_use_id / tool_call_id values to match ^[a-zA-Z0-9_-]+$."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if "tool_call_id" in msg and isinstance(msg["tool_call_id"], str):
            msg["tool_call_id"] = _TOOL_ID_RE.sub("_", msg["tool_call_id"])
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if "tool_use_id" in block and isinstance(block["tool_use_id"], str):
                    block["tool_use_id"] = _TOOL_ID_RE.sub("_", block["tool_use_id"])
                if "id" in block and block.get("type") in ("tool_use", "tool_result") and isinstance(block["id"], str):
                    block["id"] = _TOOL_ID_RE.sub("_", block["id"])
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict) and "id" in tc and isinstance(tc["id"], str):
                    tc["id"] = _TOOL_ID_RE.sub("_", tc["id"])


def _extract_cost(usage: dict, model: str) -> float:
    """Extract cost: use provider cost if present, else estimate from tokens."""
    cost = float(usage.get("cost", 0) or 0)
    if cost > 0:
        return cost
    pt = int(usage.get("prompt_tokens", 0) or 0)
    ct = int(usage.get("completion_tokens", 0) or 0)
    if pt > 0 or ct > 0:
        return _estimate_cost(model, pt, ct)
    return 0.0


async def proxy_chat_completions(
    body: dict,
    api_key: str,
    client_wants_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    """Proxy chat completion to ChatBAI (api.b.ai).

    Returns (response, cost) where cost is estimated from token counts.
    """
    client = await get_client(proxy_url)
    headers = _build_headers(api_key)

    if "model" in body:
        body["model"] = _map_model(body["model"])

    _sanitize_tool_ids(body)

    try:
        upstream_req = client.build_request(
            method="POST", url=CHATBAI_CHAT_URL, json=body, headers=headers,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.ConnectError:
        log.error("Cannot connect to ChatBAI at %s", CHATBAI_CHAT_URL)
        return JSONResponse(
            {"error": {"message": "ChatBAI upstream unavailable", "type": "proxy_error"}},
            status_code=502,
        ), 0.0
    except httpx.TimeoutException:
        log.error("Timeout connecting to ChatBAI")
        return JSONResponse(
            {"error": {"message": "ChatBAI upstream timeout", "type": "proxy_error"}},
            status_code=504,
        ), 0.0

    if upstream_resp.status_code >= 400:
        error_body = await upstream_resp.aread()
        await upstream_resp.aclose()
        try:
            error_json = json.loads(error_body)
        except (json.JSONDecodeError, ValueError):
            error_json = {"error": {"message": error_body.decode(errors="replace"), "type": "upstream_error"}}
        return JSONResponse(error_json, status_code=upstream_resp.status_code), 0.0

    mapped_model = body.get("model", "")
    if client_wants_stream:
        return _stream_passthrough(upstream_resp, mapped_model), 0.0
    else:
        return await _accumulate_response(upstream_resp, mapped_model)


def _stream_passthrough(upstream_resp: httpx.Response, model: str) -> StreamingResponse:
    """SSE passthrough — capture content + usage from chunks."""
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
            async for line in upstream_resp.aiter_lines():
                yield f"{line}\n".encode()
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload)
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            _stream_state["content"] += delta["content"]
                    usage = chunk.get("usage")
                    if usage:
                        _stream_state["prompt_tokens"] = int(usage.get("prompt_tokens", 0) or 0)
                        _stream_state["completion_tokens"] = int(usage.get("completion_tokens", 0) or 0)
                        _stream_state["total_tokens"] = int(usage.get("total_tokens", 0) or 0)
                        c = float(usage.get("cost", 0) or 0)
                        if c > 0:
                            _stream_state["cost"] = c
                except (json.JSONDecodeError, ValueError):
                    pass
        finally:
            if _stream_state["cost"] <= 0 and _stream_state["prompt_tokens"] > 0:
                _stream_state["cost"] = _estimate_cost(
                    model, _stream_state["prompt_tokens"], _stream_state["completion_tokens"]
                )
            _stream_state["done"] = True
            await upstream_resp.aclose()

    resp = StreamingResponse(
        stream_sse(),
        status_code=200,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
    resp._ws_stream_state = _stream_state
    return resp


async def _accumulate_response(
    upstream_resp: httpx.Response,
    model: str,
) -> tuple[JSONResponse, float]:
    """Read full response, extract/estimate cost, return as JSON."""
    body_bytes = await upstream_resp.aread()
    await upstream_resp.aclose()

    cost = 0.0
    try:
        data = json.loads(body_bytes)
        usage = data.get("usage") or {}
        cost = _extract_cost(usage, model)
        if "usage" in data:
            data["usage"]["cost"] = cost
    except (json.JSONDecodeError, ValueError):
        data = {"raw": body_bytes.decode("utf-8", errors="replace")[:2000]}

    if cost <= 0:
        cost = 0.001

    log.info("ChatBAI cost: $%.6f (model: %s)", cost, model)

    resp = JSONResponse(
        content=data,
        status_code=upstream_resp.status_code,
        media_type="application/json",
    )
    resp._ws_raw_body = json.dumps(data, ensure_ascii=False)[:2000]
    return resp, cost
