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
from .proxy_skillboss import proxy_chat_completions as skillboss_proxy
from .proxy_wavespeed import proxy_chat_completions as wavespeed_proxy
from .proxy_gumloop import proxy_chat_completions as gumloop_proxy
from .proxy_windsurf import proxy_chat_completions as windsurf_proxy
from .chatbai.proxy import proxy_chat_completions as chatbai_proxy
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
        # Route to WaveSpeed LLM — instant rotation on failure
        max_retries = 5
        tried_ws_ids: list[int] = []
        last_ws_error = ""

        for attempt in range(max_retries):
            account = await get_next_account(tier, exclude_ids=tried_ws_ids)
            if account is None:
                break
            tried_ws_ids.append(account["id"])

            ws_key = account.get("ws_api_key", "")
            if not ws_key:
                last_ws_error = f"Account {account['email']}: Missing ws_api_key"
                await mark_account_error(account["id"], tier, "Missing ws_api_key")
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, 503, 0,
                    request_headers=req_headers_str, request_body=req_body_str,
                    error_message=last_ws_error, proxy_url=proxy_url or "",
                )
                continue

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
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, error_message=error_msg, proxy_url=proxy_url or "",
                )
                last_ws_error = error_msg
                log.warning("WaveSpeed %s HTTP %d, trying next account", account["email"], status)
                continue
            elif status == 402 or status == 429:
                error_msg = f"WaveSpeed HTTP {status} exhausted (account: {account['email']})"
                await db.update_account(account["id"], ws_status="exhausted", ws_error=error_msg)
                try:
                    updated = await db.get_account(account["id"])
                    if updated: await license_client.push_account_now(updated)
                except Exception: pass
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, error_message=error_msg, proxy_url=proxy_url or "",
                )
                last_ws_error = error_msg
                log.warning("WaveSpeed %s exhausted, trying next account", account["email"])
                continue
            elif status >= 500:
                error_msg = f"WaveSpeed HTTP {status} (account: {account['email']})"
                await mark_account_error(account["id"], tier, error_msg)
            elif status < 400:
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
                resp_body_str = getattr(response, '_ws_raw_body', '') or _extract_response_body(response)
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, response_body=resp_body_str,
                    error_message=error_msg,
                    proxy_url=proxy_url or "",
                )
            return response

        # All WaveSpeed retries exhausted
        all_tried = ", ".join(str(i) for i in tried_ws_ids)
        final_error = f"All WaveSpeed accounts exhausted. Tried IDs: [{all_tried}]. Last error: {last_ws_error}"
        await db.log_usage(
            key_info["id"], None, model, tier.value, 503, int((time.monotonic() - start) * 1000),
            request_headers=req_headers_str, request_body=req_body_str,
            error_message=final_error, proxy_url=proxy_url or "",
        )
        return JSONResponse(
            {"error": {"message": final_error, "type": "server_error"}},
            status_code=503,
        )

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
                # For streaming: don't mark success yet — stream might fail mid-flight
                # BackgroundTask will handle final status after stream completes
                if not client_wants_stream:
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
                    for _ in range(60):
                        if gl_stream_state["done"]:
                            break
                        await _asyncio.sleep(0.5)

                    # Check if stream encountered credit/error — mark account
                    stream_error = gl_stream_state.get("error", "")
                    if stream_error:
                        acct_id = gl_stream_state.get("_account_id", _acct_id)
                        if "CREDIT_EXHAUSTED" in stream_error or "credit" in stream_error.lower():
                            await db.update_account(acct_id, gl_status="exhausted",
                                                    gl_error=stream_error[:200])
                            await db.clear_sticky_account("max_gl")
                            log.warning("[GL post-stream] Account %s marked exhausted: %s",
                                        gl_stream_state.get("_account_email", "?"), stream_error[:100])

                    # Use error status code if stream had errors
                    log_status = 529 if stream_error else status  # 529 = custom "stream error"

                    log_body = json.dumps({
                        "content": gl_stream_state["content"][:2000],
                        "usage": {
                            "prompt_tokens": gl_stream_state["prompt_tokens"],
                            "completion_tokens": gl_stream_state["completion_tokens"],
                            "total_tokens": gl_stream_state["total_tokens"],
                        },
                        "error": stream_error or None,
                    }, ensure_ascii=False)
                    await db.log_usage(
                        _key_id, _acct_id, model, tier.value, log_status, latency,
                        request_headers=req_headers_str, request_body=req_body_str,
                        response_headers=resp_headers_str, response_body=log_body,
                        error_message=stream_error or error_msg,
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

    if tier == Tier.CHATBAI:
        # Route to ChatBAI (api.b.ai) — instant rotation on failure
        max_retries = 5
        tried_cbai_ids: list[int] = []
        last_cbai_error = ""

        for attempt in range(max_retries):
            account = await get_next_account(tier, exclude_ids=tried_cbai_ids)
            if account is None:
                break
            tried_cbai_ids.append(account["id"])

            cbai_key = account.get("cbai_api_key", "")
            if not cbai_key:
                last_cbai_error = f"Account {account['email']}: Missing cbai_api_key"
                await mark_account_error(account["id"], tier, "Missing cbai_api_key")
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, 503, 0,
                    request_headers=req_headers_str, request_body=req_body_str,
                    error_message=last_cbai_error, proxy_url=proxy_url or "",
                )
                continue

            response, cost = await chatbai_proxy(body, cbai_key, client_wants_stream, proxy_url=proxy_url)
            latency = int((time.monotonic() - start) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            resp_headers_str = _capture_response_headers(response)
            error_msg = ""

            if status in (401, 403):
                # Retry same account once after 4s — might be transient
                if not hasattr(chat_completions, '_cbai_auth_retries'):
                    chat_completions._cbai_auth_retries = {}
                rkey = f"{account['id']}_{model}"
                chat_completions._cbai_auth_retries.setdefault(rkey, 0)
                chat_completions._cbai_auth_retries[rkey] += 1
                if chat_completions._cbai_auth_retries[rkey] <= 3:
                    log.warning("ChatBAI %s HTTP %d, retry %d/3 in 4s", account["email"], status, chat_completions._cbai_auth_retries[rkey])
                    await asyncio.sleep(4)
                    tried_cbai_ids.pop()
                    continue
                chat_completions._cbai_auth_retries[rkey] = 0
                error_msg = f"ChatBAI HTTP {status} (account: {account['email']})"
                resp_body_str = getattr(response, '_ws_raw_body', '') or _extract_response_body(response)
                await db.update_account(account["id"], cbai_status="banned", cbai_error=error_msg)
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, response_body=resp_body_str,
                    error_message=error_msg, proxy_url=proxy_url or "",
                )
                last_cbai_error = error_msg
                log.warning("ChatBAI %s HTTP %d after retries, marked banned", account["email"], status)
                continue
            elif status == 402 or status == 429:
                error_msg = f"ChatBAI HTTP {status} exhausted (account: {account['email']})"
                resp_body_str = getattr(response, '_ws_raw_body', '') or _extract_response_body(response)
                await db.update_account(account["id"], cbai_status="exhausted", cbai_error=error_msg)
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, response_body=resp_body_str,
                    error_message=error_msg, proxy_url=proxy_url or "",
                )
                last_cbai_error = error_msg
                log.warning("ChatBAI %s exhausted, trying next account", account["email"])
                continue
            elif status >= 500:
                error_msg = f"ChatBAI HTTP {status} (account: {account['email']})"
                await mark_account_error(account["id"], tier, error_msg)
            elif status < 400:
                await mark_account_success(account["id"], tier)
                if cost > 0:
                    await db.deduct_cbai_credit(account["id"], cost)

            # Streaming: log after stream completes via BackgroundTask
            cbai_stream_state = getattr(response, '_ws_stream_state', None)
            if cbai_stream_state is not None:
                from starlette.background import BackgroundTask

                _acct_id = account["id"]
                _key_id = key_info["id"]
                _proxy_url = proxy_url or ""

                async def _post_cbai_stream_log():
                    import asyncio
                    for _ in range(30):
                        if cbai_stream_state["done"]:
                            break
                        await asyncio.sleep(0.5)

                    stream_cost = cbai_stream_state["cost"]
                    if stream_cost > 0:
                        await db.deduct_cbai_credit(_acct_id, stream_cost)

                    log_body = json.dumps({
                        "content": cbai_stream_state["content"][:2000],
                        "usage": {
                            "prompt_tokens": cbai_stream_state["prompt_tokens"],
                            "completion_tokens": cbai_stream_state["completion_tokens"],
                            "total_tokens": cbai_stream_state["total_tokens"],
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

                response.background = BackgroundTask(_post_cbai_stream_log)
            else:
                resp_body_str = getattr(response, '_ws_raw_body', '') or _extract_response_body(response)
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, response_body=resp_body_str,
                    error_message=error_msg,
                    proxy_url=proxy_url or "",
                )
            return response

        # All ChatBAI retries exhausted
        all_tried = ", ".join(str(i) for i in tried_cbai_ids)
        final_error = f"All ChatBAI accounts exhausted. Tried IDs: [{all_tried}]. Last error: {last_cbai_error}"
        await db.log_usage(
            key_info["id"], None, model, tier.value, 503, int((time.monotonic() - start) * 1000),
            request_headers=req_headers_str, request_body=req_body_str,
            error_message=final_error, proxy_url=proxy_url or "",
        )
        return JSONResponse(
            {"error": {"message": final_error, "type": "server_error"}},
            status_code=503,
        )

    if tier == Tier.SKILLBOSS:
        # Route to SkillBoss — rotate accounts on 429 with 3s cooldown
        max_retries = 10
        tried_skboss_ids: list[int] = []
        last_skboss_error = ""

        for attempt in range(max_retries):
            account = await get_next_account(tier, exclude_ids=tried_skboss_ids)
            if account is None:
                break
            tried_skboss_ids.append(account["id"])

            skboss_key = account.get("skboss_api_key", "")
            if not skboss_key:
                last_skboss_error = f"Account {account['email']}: Missing skboss_api_key"
                await mark_account_error(account["id"], tier, "Missing skboss_api_key")
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, 503, 0,
                    request_headers=req_headers_str, request_body=req_body_str,
                    error_message=last_skboss_error, proxy_url=proxy_url or "",
                )
                continue

            response, cost = await skillboss_proxy(body, skboss_key, client_wants_stream, proxy_url=proxy_url)
            latency = int((time.monotonic() - start) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            resp_headers_str = _capture_response_headers(response)
            error_msg = ""

            if status in (401, 403):
                error_msg = f"SkillBoss HTTP {status} banned (account: {account['email']})"
                await db.update_account(account["id"], skboss_status="banned", skboss_error=error_msg)
                try:
                    updated = await db.get_account(account["id"])
                    if updated: await license_client.push_account_now(updated)
                except Exception: pass
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, error_message=error_msg, proxy_url=proxy_url or "",
                )
                last_skboss_error = error_msg
                log.warning("SkillBoss %s HTTP %d, trying next account", account["email"], status)
                continue
            elif status == 429:
                error_msg = f"SkillBoss HTTP 429 rate limited (account: {account['email']})"
                # Don't mark exhausted on 429 — just rotate with cooldown
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, error_message=error_msg, proxy_url=proxy_url or "",
                )
                last_skboss_error = error_msg
                log.warning("SkillBoss %s rate limited, cooldown 3s then next account", account["email"])
                await asyncio.sleep(3)
                continue
            elif status == 402:
                error_msg = f"SkillBoss HTTP 402 exhausted (account: {account['email']})"
                await db.update_account(account["id"], skboss_status="exhausted", skboss_error=error_msg)
                try:
                    updated = await db.get_account(account["id"])
                    if updated: await license_client.push_account_now(updated)
                except Exception: pass
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, error_message=error_msg, proxy_url=proxy_url or "",
                )
                last_skboss_error = error_msg
                log.warning("SkillBoss %s exhausted, trying next account", account["email"])
                continue
            elif status >= 500:
                error_msg = f"SkillBoss HTTP {status} (account: {account['email']})"
                await mark_account_error(account["id"], tier, error_msg)
            elif status < 400:
                await mark_account_success(account["id"], tier)
                if cost > 0:
                    await db.deduct_skboss_credit(account["id"], cost)

            resp_body_str = _extract_response_body(response)
            await db.log_usage(
                key_info["id"], account["id"], model, tier.value, status, latency,
                request_headers=req_headers_str, request_body=req_body_str,
                response_headers=resp_headers_str, response_body=resp_body_str,
                error_message=error_msg, proxy_url=proxy_url or "",
            )
            return response

        # All SkillBoss retries exhausted
        all_tried = ", ".join(str(i) for i in tried_skboss_ids)
        final_error = f"All SkillBoss accounts exhausted. Tried IDs: [{all_tried}]. Last error: {last_skboss_error}"
        await db.log_usage(
            key_info["id"], None, model, tier.value, 503, int((time.monotonic() - start) * 1000),
            request_headers=req_headers_str, request_body=req_body_str,
            error_message=final_error, proxy_url=proxy_url or "",
        )
        return JSONResponse(
            {"error": {"message": final_error, "type": "server_error"}},
            status_code=503,
        )

    if tier == Tier.WINDSURF:
        # Route to Windsurf sidecar — instant rotation on failure
        from .windsurf_manager import windsurf_sidecar

        max_retries = 5
        tried_windsurf_ids: list[int] = []
        last_windsurf_error = ""

        # Ensure sidecar is running (health check first, start if needed)
        try:
            sidecar_ok = await windsurf_sidecar.ensure_running()
        except Exception as e:
            log.warning("Windsurf sidecar manager error: %s — trying health check directly", e)
            sidecar_ok = await windsurf_sidecar.health()
        if not sidecar_ok:
            await db.log_usage(
                key_info["id"], None, model, tier.value, 503, 0,
                request_headers=req_headers_str, request_body=req_body_str,
                error_message="Windsurf sidecar not available", proxy_url=proxy_url or "",
            )
            return JSONResponse(
                {"error": {"message": "Windsurf sidecar not available", "type": "server_error"}},
                status_code=503,
            )

        for attempt in range(max_retries):
            account = await get_next_account(tier, exclude_ids=tried_windsurf_ids)
            if account is None:
                break
            tried_windsurf_ids.append(account["id"])

            windsurf_key = account.get("windsurf_api_key", "")
            if not windsurf_key:
                last_windsurf_error = f"Account {account['email']}: Missing windsurf_api_key"
                await mark_account_error(account["id"], tier, "Missing windsurf_api_key")
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, 503, 0,
                    request_headers=req_headers_str, request_body=req_body_str,
                    error_message=last_windsurf_error, proxy_url=proxy_url or "",
                )
                continue

            response, cost = await windsurf_proxy(body, windsurf_key, client_wants_stream, proxy_url=proxy_url)
            latency = int((time.monotonic() - start) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            resp_headers_str = _capture_response_headers(response)
            error_msg = ""

            if status in (401, 403):
                error_msg = f"Windsurf HTTP {status} banned (account: {account['email']})"
                await db.update_account(account["id"], windsurf_status="banned", windsurf_error=error_msg)
                try:
                    updated = await db.get_account(account["id"])
                    if updated: await license_client.push_account_now(updated)
                except Exception: pass
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, error_message=error_msg, proxy_url=proxy_url or "",
                )
                last_windsurf_error = error_msg
                log.warning("Windsurf %s HTTP %d, trying next account", account["email"], status)
                continue
            elif status == 429:
                error_msg = f"Windsurf HTTP 429 rate limited (account: {account['email']})"
                await db.update_account(account["id"], windsurf_status="rate_limited", windsurf_error=error_msg)
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, error_message=error_msg, proxy_url=proxy_url or "",
                )
                last_windsurf_error = error_msg
                log.warning("Windsurf %s rate limited, trying next account", account["email"])
                continue
            elif status == 402:
                error_msg = f"Windsurf HTTP 402 exhausted (account: {account['email']})"
                await db.update_account(account["id"], windsurf_status="exhausted", windsurf_error=error_msg)
                try:
                    updated = await db.get_account(account["id"])
                    if updated: await license_client.push_account_now(updated)
                except Exception: pass
                await db.log_usage(
                    key_info["id"], account["id"], model, tier.value, status, latency,
                    request_headers=req_headers_str, request_body=req_body_str,
                    response_headers=resp_headers_str, error_message=error_msg, proxy_url=proxy_url or "",
                )
                last_windsurf_error = error_msg
                log.warning("Windsurf %s exhausted, trying next account", account["email"])
                continue
            elif status >= 500:
                error_msg = f"Windsurf HTTP {status} (account: {account['email']})"
                await mark_account_error(account["id"], tier, error_msg)
            elif status < 400:
                await mark_account_success(account["id"], tier)
                if cost > 0:
                    await db.deduct_windsurf_credit(account["id"], cost)

            # Streaming: log after stream completes via BackgroundTask
            windsurf_stream_state = getattr(response, '_ws_stream_state', None)
            if windsurf_stream_state is not None:
                from starlette.background import BackgroundTask

                _acct_id = account["id"]
                _key_id = key_info["id"]
                _proxy_url = proxy_url or ""

                async def _post_windsurf_stream_log():
                    import asyncio
                    for _ in range(60):
                        if windsurf_stream_state["done"]:
                            break
                        await asyncio.sleep(0.5)

                    stream_cost = windsurf_stream_state["cost"]
                    if stream_cost > 0:
                        await db.deduct_windsurf_credit(_acct_id, stream_cost)

                    log_body = json.dumps({
                        "content": windsurf_stream_state["content"][:2000],
                        "usage": {
                            "prompt_tokens": windsurf_stream_state["prompt_tokens"],
                            "completion_tokens": windsurf_stream_state["completion_tokens"],
                            "total_tokens": windsurf_stream_state["total_tokens"],
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

                response.background = BackgroundTask(_post_windsurf_stream_log)
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

        # All Windsurf retries exhausted
        all_tried = ", ".join(str(i) for i in tried_windsurf_ids)
        final_error = f"All Windsurf accounts exhausted. Tried IDs: [{all_tried}]. Last error: {last_windsurf_error}"
        await db.log_usage(
            key_info["id"], None, model, tier.value, 503, int((time.monotonic() - start) * 1000),
            request_headers=req_headers_str, request_body=req_body_str,
            error_message=final_error, proxy_url=proxy_url or "",
        )
        return JSONResponse(
            {"error": {"message": final_error, "type": "server_error"}},
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
            # CB sometimes returns 400 transiently (code 11133 "invalid request parameters")
            # Retry with same account up to 10 times with 4s delay
            resp_body_str = _extract_response_body(response)
            is_transient_400 = "11133" in resp_body_str or "11101" in resp_body_str
            if is_transient_400:
                _cb_400_retries = getattr(response, '_cb_400_retries', 0)
                if not hasattr(chat_completions, '_cb_400_count'):
                    chat_completions._cb_400_count = {}
                retry_key = f"{account['id']}_{model}"
                chat_completions._cb_400_count.setdefault(retry_key, 0)
                chat_completions._cb_400_count[retry_key] += 1
                if chat_completions._cb_400_count[retry_key] <= 10:
                    log.warning("CB %s transient 400 (attempt %d/10), retry in 4s",
                                account["email"], chat_completions._cb_400_count[retry_key])
                    await asyncio.sleep(4)
                    # Don't increment tried_account_ids — retry same account
                    tried_account_ids.pop()
                    continue
                else:
                    chat_completions._cb_400_count[retry_key] = 0
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
            "owned_by": (
                "kiro" if tier == Tier.STANDARD else
                "wavespeed" if tier == Tier.WAVESPEED else
                "gumloop" if tier == Tier.MAX_GL else
                "chatbai" if tier == Tier.CHATBAI else
                "skillboss" if tier == Tier.SKILLBOSS else
                "windsurf" if tier == Tier.WINDSURF else
                "codebuddy"
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


# ─── Gumloop file download (for MCP server gl:// URLs) ──────────────────────

@router.post("/gl-download")
async def gl_download(request: Request, _: bool = Depends(verify_api_key)):
    """Download a file from Gumloop storage using the correct account auth.

    Body: {"gl_url": "gl://uid-{user_id}/path/to/file"}
    Returns: raw file bytes with appropriate content-type.
    """
    import httpx
    from fastapi.responses import Response

    body = await request.json()
    gl_url = body.get("gl_url", "")
    if not gl_url or not gl_url.startswith("gl://"):
        return JSONResponse({"error": "gl_url is required and must start with gl://"}, status_code=400)

    # Parse gl:// URL → extract user_id and file_path
    gl_path = gl_url[5:]  # strip "gl://"
    url_user_id = ""
    file_path = gl_path
    if gl_path.startswith("uid-"):
        parts = gl_path.split("/", 1)
        if len(parts) == 2:
            url_user_id = parts[0][4:]  # strip "uid-"
            file_path = parts[1]

    if not file_path:
        return JSONResponse({"error": "Invalid gl:// URL"}, status_code=400)

    # Find account with matching gl_user_id
    account = None
    all_accounts = await db.get_accounts()
    if url_user_id:
        for acct in all_accounts:
            if acct.get("gl_user_id") == url_user_id and acct.get("gl_refresh_token"):
                account = acct
                break

    # Fallback: try any GL account with valid auth
    if not account:
        for acct in all_accounts:
            if acct.get("gl_refresh_token") and acct.get("gl_status") == "ok":
                account = acct
                break

    if not account:
        return JSONResponse({"error": "No Gumloop account available for download"}, status_code=503)

    # Get fresh auth
    from .proxy_gumloop import _get_auth
    auth = _get_auth(account)
    id_token = await auth.get_token()
    user_id = auth.user_id or url_user_id

    # Download via Gumloop API
    download_body = {"file_name": file_path, "user_id": user_id}
    headers = {
        "Authorization": f"Bearer {id_token}",
        "x-auth-key": user_id,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.gumloop.com/download_file",
                json=download_body,
                headers=headers,
            )
            if resp.status_code != 200:
                log.warning("[gl-download] Failed: HTTP %s — %s", resp.status_code, resp.text[:200])
                return JSONResponse(
                    {"error": f"Gumloop download failed: HTTP {resp.status_code}"},
                    status_code=resp.status_code,
                )

            # Detect content type from file extension
            import mimetypes
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

            return Response(
                content=resp.content,
                media_type=content_type,
                headers={"Content-Length": str(len(resp.content))},
            )
    except Exception as e:
        log.error("[gl-download] Error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
