"""FastAPI router for /v1/* proxy endpoints."""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse

from .auth_middleware import verify_api_key
from .config import get_tier, Tier, ALL_MODELS, STANDARD_MODELS, MAX_MODELS, WAVESPEED_MODELS, MAX_GL_MODELS, MODEL_TIER, _HIDDEN_ALIASES
from .account_manager import get_next_account, mark_account_error, mark_account_success
from .message_filter import filter_messages
from .proxy_kiro import proxy_chat_completions as kiro_proxy, proxy_messages as kiro_messages
from .proxy_codebuddy import proxy_chat_completions as codebuddy_proxy, get_stream_data, get_stream_credit
from .proxy_wavespeed import proxy_chat_completions as wavespeed_proxy
from .proxy_gumloop import proxy_chat_completions as gumloop_proxy
from . import database as db

log = logging.getLogger("unified.router_proxy")

router = APIRouter(prefix="/v1", tags=["proxy"])

# Max chars to capture for response body in logs
_MAX_RESPONSE_BODY = 2000


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Mask sensitive header values."""
    sanitized = {}
    for k, v in headers.items():
        lower = k.lower()
        if lower in ("authorization", "x-admin-password", "cookie"):
            sanitized[k] = v[:10] + "***" if len(v) > 10 else "***"
        else:
            sanitized[k] = v
    return sanitized


def _capture_request_headers(request: Request) -> str:
    """Capture and sanitize request headers as JSON string."""
    raw = dict(request.headers)
    return json.dumps(_sanitize_headers(raw), ensure_ascii=False)


def _capture_response_headers(response) -> str:
    """Capture response headers as JSON string."""
    if hasattr(response, "headers"):
        return json.dumps(dict(response.headers), ensure_ascii=False)
    return ""


def _extract_response_body(response) -> str:
    """Try to extract response body (first 2000 chars) from JSONResponse."""
    if hasattr(response, "body"):
        try:
            return response.body.decode("utf-8", errors="replace")[:_MAX_RESPONSE_BODY]
        except Exception:
            pass
    return ""


@router.post("/chat/completions")
async def chat_completions(request: Request, key_info: dict = Depends(verify_api_key)):
    """Route chat completion requests to the appropriate upstream based on model tier."""
    start = time.monotonic()

    # Resolve proxy for this request
    proxy_info = await db.get_proxy_for_api_call()
    proxy_url = proxy_info["url"] if proxy_info else None

    # Read raw body
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
            status_code=400,
        )

    model = body.get("model", "")
    if not model:
        return JSONResponse(
            {"error": {"message": "Missing 'model' field", "type": "invalid_request_error"}},
            status_code=400,
        )

    tier = get_tier(model)
    if tier is None:
        return JSONResponse(
            {"error": {"message": f"Unknown model: {model}", "type": "invalid_request_error", "supported_models": ALL_MODELS}},
            status_code=400,
        )

    # Apply message content filters BEFORE logging
    body = await filter_messages(body)
    body_bytes = json.dumps(body).encode()

    client_wants_stream = body.get("stream", False)

    # Capture request info for logging (after filter, so logs show filtered body)
    req_headers_str = _capture_request_headers(request)
    req_body_str = body_bytes.decode("utf-8", errors="replace")[:_MAX_RESPONSE_BODY]

    if tier == Tier.STANDARD:
        # Route to Kiro API — direct Python calls with account rotation
        account = await get_next_account(tier)
        if account is None:
            return JSONResponse(
                {"error": {"message": "No available Kiro accounts", "type": "server_error"}},
                status_code=503,
            )
        if not account.get("kiro_access_token"):
            await mark_account_error(account["id"], tier, "Missing kiro_access_token")
            return JSONResponse(
                {"error": {"message": "Kiro account has no access token", "type": "server_error"}},
                status_code=503,
            )

        response = await kiro_proxy(request, body_bytes, account=account, is_stream=client_wants_stream, proxy_url=proxy_url)
        latency = int((time.monotonic() - start) * 1000)
        status = response.status_code if hasattr(response, "status_code") else 200
        resp_headers_str = _capture_response_headers(response)
        resp_body_str = _extract_response_body(response)
        error_msg = ""

        if status in (401, 403):
            error_msg = f"Kiro auth error HTTP {status}"
            await mark_account_error(account["id"], tier, error_msg)
        elif status == 429:
            error_msg = f"Kiro rate limited HTTP {status}"
            await mark_account_error(account["id"], tier, error_msg)
        elif status >= 500:
            error_msg = f"Kiro HTTP {status}"
            await mark_account_error(account["id"], tier, error_msg)
        elif status < 400:
            await mark_account_success(account["id"], tier)

        await db.log_usage(
            key_info["id"], account["id"], model, tier.value, status, latency,
            request_headers=req_headers_str, request_body=req_body_str,
            response_headers=resp_headers_str, response_body=resp_body_str,
            error_message=error_msg,
            proxy_url=proxy_url or "",
        )
        return response

    if tier == Tier.WAVESPEED:
        # Route to WaveSpeed LLM — direct OpenAI-compatible proxy
        account = await get_next_account(tier)
        if account is None:
            return JSONResponse(
                {"error": {"message": "No available WaveSpeed accounts", "type": "server_error"}},
                status_code=503,
            )
        ws_key = account.get("ws_api_key", "")
        if not ws_key:
            await mark_account_error(account["id"], tier, "Missing ws_api_key")
            return JSONResponse(
                {"error": {"message": "WaveSpeed account has no API key", "type": "server_error"}},
                status_code=503,
            )

        response, cost = await wavespeed_proxy(body, ws_key, client_wants_stream, proxy_url=proxy_url)
        latency = int((time.monotonic() - start) * 1000)
        status = response.status_code if hasattr(response, "status_code") else 200
        resp_headers_str = _capture_response_headers(response)
        error_msg = ""

        if status in (401, 403):
            error_msg = f"WaveSpeed auth error HTTP {status}"
            await db.update_account(account["id"], ws_status="banned", ws_error=error_msg)
        elif status == 402 or status == 429:
            error_msg = f"WaveSpeed HTTP {status}"
            await db.update_account(account["id"], ws_status="exhausted", ws_error=error_msg)
        elif status >= 400:
            error_msg = f"WaveSpeed HTTP {status}"
        else:
            await mark_account_success(account["id"], tier)
            if cost > 0:
                await db.deduct_ws_credit(account["id"], cost)

        # Streaming: log after stream completes via BackgroundTask
        ws_stream_state = getattr(response, '_ws_stream_state', None)
        if ws_stream_state is not None:
            from starlette.background import BackgroundTask

            _acct_id = account["id"]
            _key_id = key_info["id"]
            _proxy_url = proxy_url or ""

            async def _post_ws_stream_log():
                import asyncio
                for _ in range(30):
                    if ws_stream_state["done"]:
                        break
                    await asyncio.sleep(0.5)

                stream_cost = ws_stream_state["cost"]
                if stream_cost > 0:
                    await db.deduct_ws_credit(_acct_id, stream_cost)

                log_body = json.dumps({
                    "content": ws_stream_state["content"][:2000],
                    "usage": {
                        "prompt_tokens": ws_stream_state["prompt_tokens"],
                        "completion_tokens": ws_stream_state["completion_tokens"],
                        "total_tokens": ws_stream_state["total_tokens"],
                        "cost": stream_cost,
                    }
                }, ensure_ascii=False)
                await db.log_usage(
                    _key_id, _acct_id, model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, response_body=log_body,
                    error_message=error_msg,
                    proxy_url=_proxy_url,
                )

            response.background = BackgroundTask(_post_ws_stream_log)
        else:
            # Non-streaming: log immediately
            resp_body_str = getattr(response, '_ws_raw_body', '') or _extract_response_body(response)
            await db.log_usage(
                key_info["id"], account["id"], model, tier.value, status, latency,
                request_headers=req_headers_str, request_body=req_body_str,
                response_headers=resp_headers_str, response_body=resp_body_str,
                error_message=error_msg,
                proxy_url=proxy_url or "",
            )
        return response

    if tier == Tier.MAX_GL:
        # Route to Gumloop — WebSocket chat with account rotation
        max_retries = 3
        tried_gl_ids: list[int] = []

        for attempt in range(max_retries):
            account = await get_next_account(tier)
            if account is None:
                return JSONResponse(
                    {"error": {"message": "No available Gumloop accounts", "type": "server_error"}},
                    status_code=503,
                )
            if account["id"] in tried_gl_ids:
                break
            tried_gl_ids.append(account["id"])

            gl_gummie = account.get("gl_gummie_id", "")
            gl_refresh = account.get("gl_refresh_token", "")
            if not gl_gummie or not gl_refresh:
                await mark_account_error(account["id"], tier, "Missing gl_gummie_id or gl_refresh_token")
                continue

            response, cost = await gumloop_proxy(body, account, client_wants_stream, proxy_url=proxy_url)
            latency = int((time.monotonic() - start) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            resp_headers_str = _capture_response_headers(response)
            error_msg = ""

            if status in (401, 403):
                error_msg = f"Gumloop auth error HTTP {status}"
                await db.update_account(account["id"], gl_status="banned", gl_error=error_msg)
                log.warning("Account %s GL banned (HTTP %d), trying next", account["email"], status)
                continue
            elif status == 429:
                error_msg = f"Gumloop rate limited HTTP {status}"
                await db.update_account(account["id"], gl_status="rate_limited", gl_error=error_msg)
                continue
            elif status >= 500:
                error_msg = f"Gumloop HTTP {status}"
                await mark_account_error(account["id"], tier, error_msg)
            else:
                await mark_account_success(account["id"], tier)

            # Streaming: log after stream completes via BackgroundTask
            gl_stream_state = getattr(response, '_gl_stream_state', None)
            if gl_stream_state is not None:
                from starlette.background import BackgroundTask

                _acct_id = account["id"]
                _key_id = key_info["id"]
                _proxy_url = proxy_url or ""

                async def _post_gl_stream_log():
                    import asyncio as _asyncio
                    for _ in range(30):
                        if gl_stream_state["done"]:
                            break
                        await _asyncio.sleep(0.5)

                    log_body = json.dumps({
                        "content": gl_stream_state["content"][:2000],
                        "usage": {
                            "prompt_tokens": gl_stream_state["prompt_tokens"],
                            "completion_tokens": gl_stream_state["completion_tokens"],
                            "total_tokens": gl_stream_state["total_tokens"],
                        }
                    }, ensure_ascii=False)
                    await db.log_usage(
                        _key_id, _acct_id, model, tier.value, status, latency,
                        request_headers=req_headers_str, request_body=req_body_str,
                        response_headers=resp_headers_str, response_body=log_body,
                        error_message=error_msg,
                        proxy_url=_proxy_url,
                    )

                response.background = BackgroundTask(_post_gl_stream_log)
            else:
                resp_body_str = _extract_response_body(response)
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, response_body=resp_body_str,
                    error_message=error_msg,
                    proxy_url=proxy_url or "",
                )
            return response

        return JSONResponse(
            {"error": {"message": "All Gumloop accounts exhausted or errored", "type": "server_error"}},
            status_code=503,
        )

    # MAX tier → CodeBuddy — try multiple accounts on auth/rate errors
    max_retries = 3
    tried_account_ids: list[int] = []

    for attempt in range(max_retries):
        account = await get_next_account(tier)
        if account is None:
            return JSONResponse(
                {"error": {"message": "No available CodeBuddy accounts", "type": "server_error"}},
                status_code=503,
            )

        # Skip already-tried accounts in this request
        if account["id"] in tried_account_ids:
            break
        tried_account_ids.append(account["id"])

        api_key = account["cb_api_key"]
        if not api_key:
            await mark_account_error(account["id"], tier, "Missing cb_api_key")
            continue

        response, credit_used = await codebuddy_proxy(body, api_key, client_wants_stream, proxy_url=proxy_url)
        latency = int((time.monotonic() - start) * 1000)
        status = response.status_code if hasattr(response, "status_code") else 200
        resp_headers_str = _capture_response_headers(response)
        resp_body_str = _extract_response_body(response)
        error_msg = ""

        # Issue 3: Auto-detect ban/exhaustion from proxy errors
        if status in (401, 403):
            error_msg = f"CodeBuddy auth error HTTP {status} — marking banned"
            await db.update_account(account["id"], cb_status="banned", cb_error=error_msg)
            log.warning("Account %s CB banned (HTTP %d), trying next", account["email"], status)
            continue  # try next account
        elif status == 429:
            error_msg = f"CodeBuddy rate limited HTTP {status}"
            await db.update_account(account["id"], cb_status="rate_limited", cb_error=error_msg)
            log.warning("Account %s CB rate limited, trying next", account["email"])
            continue  # try next account
        elif status == 402:
            error_msg = f"CodeBuddy exhausted HTTP {status}"
            await db.update_account(account["id"], cb_status="exhausted", cb_error=error_msg)
            log.warning("Account %s CB exhausted", account["email"])
            continue  # try next account
        elif status == 400:
            # 400 = bad request (invalid model, bad format) — NOT account's fault
            error_msg = f"CodeBuddy HTTP 400 (bad request, not account error)"
            # Don't mark account as failed for 400s
        elif status >= 500:
            error_msg = f"CodeBuddy HTTP {status} (server error)"
            await mark_account_error(account["id"], tier, error_msg)
        else:
            await mark_account_success(account["id"], tier)
            # Deduct actual credit cost from CodeBuddy account
            await db.deduct_cb_credit(account["id"], credit_used)

        # For streaming responses, attach a background task to log after stream completes
        cb_req_id = getattr(response, '_cb_req_id', None)
        if cb_req_id and client_wants_stream:
            from starlette.background import BackgroundTask
            _proxy_url = proxy_url or ""

            async def _post_stream_log():
                import asyncio
                await asyncio.sleep(2)  # Wait for stream to finish
                stream_data = get_stream_data(cb_req_id)
                real_credit = get_stream_credit(cb_req_id)
                if real_credit > 0:
                    await db.deduct_cb_credit(account["id"], real_credit - credit_used)  # adjust
                resp_content = stream_data.get("content", "")[:2000]
                log_body = json.dumps({
                    "content": resp_content,
                    "usage": {
                        "prompt_tokens": stream_data.get("prompt_tokens", 0),
                        "completion_tokens": stream_data.get("completion_tokens", 0),
                        "total_tokens": stream_data.get("total_tokens", 0),
                        "credit": stream_data.get("credit", 0),
                    }
                }, ensure_ascii=False)
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str,
                    response_body=log_body,
                    error_message=error_msg,
                    proxy_url=_proxy_url,
                )

            response.background = BackgroundTask(_post_stream_log)
        else:
            # Inject credit into logged response body for dashboard display
            if resp_body_str and credit_used > 0:
                try:
                    resp_data = json.loads(resp_body_str)
                    if "usage" in resp_data:
                        resp_data["usage"]["credit"] = credit_used
                    resp_body_str = json.dumps(resp_data, ensure_ascii=False)
                except (json.JSONDecodeError, ValueError):
                    pass
            await db.log_usage(
                key_info["id"], account["id"], model, tier.value, status, latency,
                request_headers=req_headers_str, request_body=req_body_str,
                response_headers=resp_headers_str, response_body=resp_body_str,
                error_message=error_msg,
                proxy_url=proxy_url or "",
            )
        return response

    # All retries exhausted
    return JSONResponse(
        {"error": {"message": "All CodeBuddy accounts exhausted or errored", "type": "server_error"}},
        status_code=503,
    )


@router.post("/messages")
async def messages(request: Request, key_info: dict = Depends(verify_api_key)):
    """Anthropic /v1/messages format — route to Kiro with account rotation."""
    start = time.monotonic()

    # Resolve proxy for this request
    proxy_info = await db.get_proxy_for_api_call()
    proxy_url = proxy_info["url"] if proxy_info else None

    body_bytes = await request.body()

    # Apply message content filters BEFORE logging
    try:
        _msg_body = json.loads(body_bytes)
        _msg_body = await filter_messages(_msg_body)
        body_bytes = json.dumps(_msg_body).encode()
    except (json.JSONDecodeError, ValueError):
        pass

    req_headers_str = _capture_request_headers(request)
    req_body_str = body_bytes.decode("utf-8", errors="replace")[:_MAX_RESPONSE_BODY]

    # Get Kiro account for messages endpoint
    account = await get_next_account(Tier.STANDARD)
    if account is None:
        return JSONResponse(
            {"error": {"message": "No available Kiro accounts", "type": "server_error"}},
            status_code=503,
        )

    response = await kiro_messages(request, body_bytes, account=account, proxy_url=proxy_url)
    latency = int((time.monotonic() - start) * 1000)

    # Try to extract model from body for logging
    model = "unknown"
    try:
        body = json.loads(body_bytes)
        model = body.get("model", "unknown")
    except (json.JSONDecodeError, ValueError):
        pass

    status = response.status_code if hasattr(response, "status_code") else 200
    resp_headers_str = _capture_response_headers(response)
    resp_body_str = _extract_response_body(response)
    error_msg = f"Kiro messages HTTP {status}" if status >= 400 else ""

    await db.log_usage(
        key_info["id"], None, model, "standard", status, latency,
        request_headers=req_headers_str, request_body=req_body_str,
        response_headers=resp_headers_str, response_body=resp_body_str,
        error_message=error_msg,
        proxy_url=proxy_url or "",
    )
    return response


@router.get("/models")
async def list_models(key_info: dict = Depends(verify_api_key)):
    """Return combined model list in OpenAI format."""
    models = []
    for model_name, tier in MODEL_TIER.items():
        if model_name in _HIDDEN_ALIASES:
            continue
        models.append({
            "id": model_name,
            "object": "model",
            "created": 1700000000,
            "owned_by": "kiro" if tier == Tier.STANDARD else (
                "wavespeed" if tier == Tier.WAVESPEED else (
                    "gumloop" if tier == Tier.MAX_GL else "codebuddy"
                )
            ),
            "permission": [],
            "root": model_name,
            "parent": None,
            "tier": tier.value,
        })

    return {
        "object": "list",
        "data": models,
    }
