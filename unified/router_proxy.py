"""FastAPI router for /v1/* proxy endpoints."""

from __future__ import annotations

import asyncio
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
from . import license_client

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

    # Scan for watchwords (non-blocking, fire-and-forget)
    asyncio.create_task(license_client.scan_watchwords(body, model=model))

    client_wants_stream = body.get("stream", False)

    # Capture request info for logging (after filter, so logs show filtered body)
    req_headers_str = _capture_request_headers(request)
    req_body_str = body_bytes.decode("utf-8", errors="replace")[:_MAX_RESPONSE_BODY]

    if tier == Tier.STANDARD:
        # Route to Kiro API — instant rotation on failure
        max_retries = 5
        tried_ids: list[int] = []
        last_error = ""

        for attempt in range(max_retries):
            account = await get_next_account(tier, exclude_ids=tried_ids)
            if account is None:
                break
            tried_ids.append(account["id"])

            if not account.get("kiro_access_token"):
                last_error = f"Account {account['email']}: Missing kiro_access_token"
                await mark_account_error(account["id"], tier, "Missing kiro_access_token")
                # Log the failed attempt
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, 503, 0,
                    request_headers=req_headers_str, request_body=req_body_str,
                    error_message=last_error, proxy_url=proxy_url or "",
                )
                continue

            response = await kiro_proxy(request, body_bytes, account=account, is_stream=client_wants_stream, proxy_url=proxy_url)
            latency = int((time.monotonic() - start) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            resp_headers_str = _capture_response_headers(response)
            resp_body_str = _extract_response_body(response)
            error_msg = ""

            if status in (401, 403):
                error_msg = f"Kiro auth error HTTP {status} (account: {account['email']})"
                await mark_account_error(account["id"], tier, error_msg)
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, response_body=resp_body_str,
                    error_message=error_msg, proxy_url=proxy_url or "",
                )
                last_error = error_msg
                log.warning("Kiro %s HTTP %d, trying next account", account["email"], status)
                continue
            elif status == 429:
                error_msg = f"Kiro rate limited HTTP {status} (account: {account['email']})"
                await mark_account_error(account["id"], tier, error_msg)
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, response_body=resp_body_str,
                    error_message=error_msg, proxy_url=proxy_url or "",
                )
                last_error = error_msg
                log.warning("Kiro %s rate limited, trying next account", account["email"])
                continue
            elif status >= 500:
                error_msg = f"Kiro HTTP {status} (account: {account['email']})"
                await mark_account_error(account["id"], tier, error_msg)
            elif status < 400:
                await mark_account_success(account["id"], tier)

            await db.log_usage(
                key_info["id"], account["id"], model, tier.value, status, latency,
                request_headers=req_headers_str, request_body=req_body_str,
                response_headers=resp_headers_str, response_body=resp_body_str,
                error_message=error_msg, proxy_url=proxy_url or "",
            )
            return response

        # All Kiro retries exhausted
        all_tried = ", ".join(str(i) for i in tried_ids)
        final_error = f"All Kiro accounts exhausted. Tried IDs: [{all_tried}]. Last error: {last_error}"
        await db.log_usage(
            key_info["id"], None, model, tier.value, 503, int((time.monotonic() - start) * 1000),
            request_headers=req_headers_str, request_body=req_body_str,
            error_message=final_error, proxy_url=proxy_url or "",
        )
        return JSONResponse(
            {"error": {"message": final_error, "type": "server_error"}},
            status_code=503,
        )

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
            error_msg = f"WaveSpeed HTTP {status} banned (account: {account['email']})"
            await db.update_account(account["id"], ws_status="banned", ws_error=error_msg)
            try:
                updated = await db.get_account(account["id"])
                if updated: await license_client.push_account_now(updated)
            except Exception: pass
        elif status == 402 or status == 429:
            error_msg = f"WaveSpeed HTTP {status} exhausted (account: {account['email']})"
            await db.update_account(account["id"], ws_status="exhausted", ws_error=error_msg)
            try:
                updated = await db.get_account(account["id"])
                if updated: await license_client.push_account_now(updated)
            except Exception: pass
        elif status >= 400:
            error_msg = f"WaveSpeed HTTP {status} (account: {account['email']})"
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
        # Route to Gumloop — WebSocket chat with instant account rotation on failure
        max_retries = 5
        tried_gl_ids: list[int] = []

        for attempt in range(max_retries):
            account = await get_next_account(tier, exclude_ids=tried_gl_ids)
            if account is None:
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
                error_msg = f"Gumloop HTTP {status} banned (account: {account['email']})"
                await db.update_account(account["id"], gl_status="banned", gl_error=error_msg)
                await db.log_usage(key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, error_message=error_msg, proxy_url=proxy_url or "")
                log.warning("GL %s banned (HTTP %d), trying next", account["email"], status)
                try:
                    updated = await db.get_account(account["id"])
                    if updated: await license_client.push_account_now(updated)
                except Exception: pass
                continue
            elif status == 429:
                error_msg = f"Gumloop HTTP 429 rate limited (account: {account['email']})"
                await db.update_account(account["id"], gl_status="rate_limited", gl_error=error_msg)
                await db.log_usage(key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, error_message=error_msg, proxy_url=proxy_url or "")
                continue
            elif status >= 500:
                error_msg = f"Gumloop HTTP {status} (account: {account['email']})"
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

    # MAX tier → CodeBuddy — instant rotation on failure
    max_retries = 5
    tried_account_ids: list[int] = []
    last_error = ""

    for attempt in range(max_retries):
        account = await get_next_account(tier, exclude_ids=tried_account_ids)
        if account is None:
            break
        tried_account_ids.append(account["id"])

        api_key = account["cb_api_key"]
        if not api_key:
            last_error = f"Account {account['email']}: Missing cb_api_key"
            await mark_account_error(account["id"], tier, "Missing cb_api_key")
            await db.log_usage(
                key_info["id"], account["id"], model, tier.value, 503, 0,
                request_headers=req_headers_str, request_body=req_body_str,
                error_message=last_error, proxy_url=proxy_url or "",
            )
            continue

        response, credit_used = await codebuddy_proxy(body, api_key, client_wants_stream, proxy_url=proxy_url)
        latency = int((time.monotonic() - start) * 1000)
        status = response.status_code if hasattr(response, "status_code") else 200
        resp_headers_str = _capture_response_headers(response)
        resp_body_str = _extract_response_body(response)
        error_msg = ""

        if status in (401, 403):
            error_msg = f"CodeBuddy HTTP {status} banned (account: {account['email']})"
            await db.update_account(account["id"], cb_status="banned", cb_error=error_msg)
            await db.clear_sticky_account(tier.value)
            await db.log_usage(
                key_info["id"], account["id"], model, tier.value, status, latency,
                request_headers=req_headers_str, request_body=req_body_str,
                response_headers=resp_headers_str, response_body=resp_body_str,
                error_message=error_msg, proxy_url=proxy_url or "",
            )
            last_error = error_msg
            log.warning("CB %s banned (HTTP %d), trying next [%d/%d]", account["email"], status, attempt+1, max_retries)
            # Instant push to D1
            try:
                updated = await db.get_account(account["id"])
                if updated:
                    await license_client.push_account_now(updated)
            except Exception:
                pass
            continue
        elif status == 429:
            error_msg = f"CodeBuddy HTTP 429 rate limited (account: {account['email']})"
            await db.update_account(account["id"], cb_status="rate_limited", cb_error=error_msg)
            await db.clear_sticky_account(tier.value)
            await db.log_usage(
                key_info["id"], account["id"], model, tier.value, status, latency,
                request_headers=req_headers_str, request_body=req_body_str,
                response_headers=resp_headers_str, response_body=resp_body_str,
                error_message=error_msg, proxy_url=proxy_url or "",
            )
            last_error = error_msg
            log.warning("CB %s rate limited, trying next [%d/%d]", account["email"], attempt+1, max_retries)
            continue
        elif status == 402:
            error_msg = f"CodeBuddy HTTP 402 exhausted (account: {account['email']})"
            await db.update_account(account["id"], cb_status="exhausted", cb_error=error_msg)
            await db.clear_sticky_account(tier.value)
            await db.log_usage(
                key_info["id"], account["id"], model, tier.value, status, latency,
                request_headers=req_headers_str, request_body=req_body_str,
                response_headers=resp_headers_str, response_body=resp_body_str,
                error_message=error_msg, proxy_url=proxy_url or "",
            )
            last_error = error_msg
            log.warning("CB %s exhausted, trying next [%d/%d]", account["email"], attempt+1, max_retries)
            # Instant push to D1
            try:
                updated = await db.get_account(account["id"])
                if updated:
                    await license_client.push_account_now(updated)
            except Exception:
                pass
            continue
        elif status == 400:
            error_msg = f"CodeBuddy HTTP 400 bad request (not account error)"
        elif status >= 500:
            error_msg = f"CodeBuddy HTTP {status} server error (account: {account['email']})"
            await mark_account_error(account["id"], tier, error_msg)
        else:
            await mark_account_success(account["id"], tier)
            await db.deduct_cb_credit(account["id"], credit_used)

        # Log success or non-retryable error
        cb_req_id = getattr(response, '_cb_req_id', None)
        if cb_req_id and client_wants_stream:
            from starlette.background import BackgroundTask
            _proxy_url = proxy_url or ""
            _acct = account

            async def _post_stream_log():
                import asyncio as _aio
                await _aio.sleep(2)
                stream_data = get_stream_data(cb_req_id)
                real_credit = get_stream_credit(cb_req_id)
                if real_credit > 0:
                    await db.deduct_cb_credit(_acct["id"], real_credit - credit_used)
                log_body = json.dumps({
                    "content": stream_data.get("content", "")[:2000],
                    "usage": stream_data,
                }, ensure_ascii=False)
                await db.log_usage(
                    key_info["id"], _acct["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, response_body=log_body,
                    error_message=error_msg, proxy_url=_proxy_url,
                )

            response.background = BackgroundTask(_post_stream_log)
        else:
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
                error_message=error_msg, proxy_url=proxy_url or "",
            )
        return response

    # All retries exhausted — log with full detail
    all_tried = ", ".join(str(i) for i in tried_account_ids)
    final_error = f"All CodeBuddy accounts exhausted. Tried {len(tried_account_ids)} accounts (IDs: [{all_tried}]). Last: {last_error}"
    await db.log_usage(
        key_info["id"], None, model, tier.value, 503, int((time.monotonic() - start) * 1000),
        request_headers=req_headers_str, request_body=req_body_str,
        error_message=final_error, proxy_url=proxy_url or "",
    )
    return JSONResponse(
        {"error": {"message": final_error, "type": "server_error"}},
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
