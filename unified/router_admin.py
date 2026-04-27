"""FastAPI router for /api/* admin endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .auth_middleware import verify_admin
from .models import AccountCreate, ApiKeyCreate, BatchLoginRequest
from .account_manager import (
    refresh_credits, auto_trash, refresh_all_credits as _refresh_all_credits,
    refresh_kiro_credits, refresh_cb_credits, check_account_health,
)
from .batch_runner import batch_state, start_batch, cancel_batch, retry_account
from .config import MODEL_TIER, Tier, LISTEN_PORT, WAVESPEED_MODELS, MAX_GL_MODELS, ALL_MODELS, _HIDDEN_ALIASES
from . import database as db

log = logging.getLogger("unified.router_admin")

router = APIRouter(prefix="/api", tags=["admin"])


class TestModelRequest(BaseModel):
    model: str = "claude-sonnet-4.5"


# ---------------------------------------------------------------------------
# Models endpoint (admin, no API key needed)
# ---------------------------------------------------------------------------

@router.get("/models")
async def admin_list_models(_: bool = Depends(verify_admin)):
    """Return combined model list (admin endpoint, no API key needed)."""
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
            "tier": tier.value,
        })
    return {"object": "list", "data": models}


# ---------------------------------------------------------------------------
# Account endpoints
# ---------------------------------------------------------------------------

@router.get("/accounts")
async def list_accounts(request: Request, _: bool = Depends(verify_admin)):
    """List all accounts grouped by status."""
    all_accounts = await db.get_accounts()

    grouped: dict[str, list] = {"active": [], "failed": [], "trash": [], "banned": []}
    for acc in all_accounts:
        info = {
            "id": acc["id"],
            "email": acc["email"],
            "status": acc["status"],
            "kiro_status": acc["kiro_status"],
            "cb_status": acc["cb_status"],
            "kiro_credits": acc.get("kiro_credits", 0),
            "kiro_credits_total": acc.get("kiro_credits_total", 0),
            "kiro_credits_used": acc.get("kiro_credits_used", 0),
            "cb_credits": acc.get("cb_credits", 0),
            "kiro_error": acc["kiro_error"],
            "cb_error": acc["cb_error"],
            "kiro_error_count": acc["kiro_error_count"],
            "cb_error_count": acc["cb_error_count"],
            "last_used_kiro": acc["last_used_kiro"],
            "last_used_cb": acc["last_used_cb"],
            "created_at": acc["created_at"],
            "cb_expires_at": acc.get("cb_expires_at", ""),
            "kiro_expires_at": acc.get("kiro_expires_at", ""),
            "ws_status": acc.get("ws_status", "none"),
            "ws_api_key": acc.get("ws_api_key", ""),
            "ws_credits": acc.get("ws_credits", 0),
            "ws_error": acc.get("ws_error", ""),
            "last_used_ws": acc.get("last_used_ws", ""),
            "kiro_verified": acc.get("kiro_verified", 0),
            "cb_verified": acc.get("cb_verified", 0),
            "ws_verified": acc.get("ws_verified", 0),
            "kiro_test_error": acc.get("kiro_test_error", ""),
            "cb_test_error": acc.get("cb_test_error", ""),
            "ws_test_error": acc.get("ws_test_error", ""),
            "gl_status": acc.get("gl_status", "none"),
            "gl_gummie_id": acc.get("gl_gummie_id", ""),
            "gl_credits": acc.get("gl_credits", 0),
            "gl_error": acc.get("gl_error", ""),
            "gl_error_count": acc.get("gl_error_count", 0),
            "last_used_gl": acc.get("last_used_gl", ""),
            "gl_verified": acc.get("gl_verified", 0),
            "gl_test_error": acc.get("gl_test_error", ""),
        }
        bucket = acc["status"] if acc["status"] in grouped else "active"
        grouped[bucket].append(info)

    sticky = {
        "standard": await db.get_sticky_account_id("standard"),
        "max": await db.get_sticky_account_id("max"),
        "wavespeed": await db.get_sticky_account_id("wavespeed"),
        "max_gl": await db.get_sticky_account_id("max_gl"),
    }

    return {
        "accounts": grouped,
        "total": len(all_accounts),
        "active": len(grouped["active"]),
        "failed": len(grouped["failed"]),
        "trash": len(grouped["trash"]),
        "sticky": sticky,
    }


@router.post("/accounts/add")
async def add_accounts(req: AccountCreate, request: Request, _: bool = Depends(verify_admin)):
    """Add accounts from email:password lines."""
    added = 0
    skipped = 0
    errors: list[str] = []

    for line in req.accounts:
        line = line.strip()
        if not line or ":" not in line:
            continue
        parts = line.split(":", 1)
        email = parts[0].strip()
        password = parts[1].strip()
        if not email or not password:
            continue

        existing = await db.get_account_by_email(email)
        if existing:
            skipped += 1
            continue

        try:
            await db.create_account(email, password)
            added += 1
        except Exception as e:
            errors.append(f"{email}: {e}")

    return {"added": added, "skipped": skipped, "errors": errors}


@router.post("/accounts/import-gumloop")
async def import_gumloop_account(request: Request, _: bool = Depends(verify_admin)):
    """Import a Gumloop account with pre-obtained tokens from browser signup.

    Body: {
        email: str,              # Account email
        refresh_token: str,      # Firebase refresh token (from browser signup)
        user_id: str,            # Firebase user ID
        gummie_id: str,          # Gumloop gummie ID
        password?: str,          # Optional password (for DB record)
    }
    """
    body = await request.json()
    email = str(body.get("email", "")).strip()
    refresh_token = str(body.get("refresh_token", "")).strip()
    user_id = str(body.get("user_id", "")).strip()
    gummie_id = str(body.get("gummie_id", "")).strip()
    password = str(body.get("password", "gumloop")).strip()

    if not email or not refresh_token or not user_id:
        return JSONResponse(
            {"error": "email, refresh_token, and user_id are required"},
            status_code=400,
        )

    # Find or create account
    existing = await db.get_account_by_email(email)
    if existing:
        account_id = existing["id"]
    else:
        account_id = await db.create_account(email, password)

    # Verify token works by refreshing
    from .gumloop.auth import GumloopAuth
    auth = GumloopAuth(refresh_token=refresh_token, user_id=user_id)
    try:
        id_token = await auth.get_token()
        updated = auth.get_updated_tokens()
    except Exception as e:
        return JSONResponse(
            {"error": f"Token refresh failed: {e}"},
            status_code=400,
        )

    # Update account with GL data
    await db.update_account(
        account_id,
        gl_status="ok",
        gl_refresh_token=updated.get("gl_refresh_token", refresh_token),
        gl_user_id=updated.get("gl_user_id", user_id),
        gl_gummie_id=gummie_id,
        gl_id_token=updated.get("gl_id_token", ""),
        gl_error="",
        gl_error_count=0,
    )

    return {
        "ok": True,
        "account_id": account_id,
        "email": email,
        "gl_status": "ok",
        "gummie_id": gummie_id,
        "message": "Gumloop account imported and token verified",
    }


@router.post("/accounts/{account_id}/sticky/{tier}")
async def set_sticky_account_endpoint(account_id: int, tier: str, _: bool = Depends(verify_admin)):
    """Set an account as the sticky (pinned) account for a tier.

    Pinned accounts won't be auto-cleared on errors — they stay selected
    until manually cleared by the user.
    """
    valid_tiers = {"standard", "max", "wavespeed", "max_gl"}
    if tier not in valid_tiers:
        return JSONResponse({"error": f"Invalid tier: {tier}"}, status_code=400)
    account = await db.get_account(account_id)
    if not account:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    await db.set_sticky_account(tier, account_id, pinned=True)
    return {"ok": True, "tier": tier, "account_id": account_id, "pinned": True}


@router.delete("/accounts/{account_id}/sticky/{tier}")
async def clear_sticky_account_endpoint(account_id: int, tier: str, _: bool = Depends(verify_admin)):
    """Clear sticky account for a tier (force-clear, even if pinned)."""
    await db.force_clear_sticky_account(tier)
    return {"ok": True, "tier": tier}


@router.delete("/accounts/delete-fix")
async def delete_fix_accounts(_: bool = Depends(verify_admin)):
    """Per-provider fix cleanup: clear dead provider credentials.

    For each account, check each provider:
    - If provider is verified-fix (exhausted/banned/failed + verified=1), clear its credentials.
    - If ALL providers end up dead after cleanup, delete the whole account.

    This allows keeping accounts that still have working providers.
    """
    accounts = await db.get_accounts()
    cleared = 0
    deleted = 0

    for acc in accounts:
        acct_id = acc["id"]
        any_cleared = False

        # Kiro: fix = verified + dead status
        if acc.get("kiro_verified", 0) == 1 and acc.get("kiro_status") in ("failed", "exhausted", "banned"):
            await db.update_account(acct_id, kiro_status="none", kiro_access_token="",
                                    kiro_refresh_token="", kiro_error="", kiro_error_count=0,
                                    kiro_credits=0, kiro_credits_total=0, kiro_credits_used=0,
                                    kiro_verified=0)
            any_cleared = True
            cleared += 1

        # CodeBuddy: fix = verified + dead status (including rate_limited)
        if acc.get("cb_verified", 0) == 1 and acc.get("cb_status") in ("failed", "exhausted", "banned", "rate_limited"):
            await db.update_account(acct_id, cb_status="none", cb_api_key="",
                                    cb_error="", cb_error_count=0, cb_credits=0, cb_verified=0)
            any_cleared = True
            cleared += 1

        # WaveSpeed: fix = verified + dead status
        if acc.get("ws_verified", 0) == 1 and acc.get("ws_status") in ("failed", "exhausted", "banned"):
            await db.update_account(acct_id, ws_status="none", ws_api_key="",
                                    ws_error="", ws_error_count=0, ws_credits=0, ws_verified=0)
            any_cleared = True
            cleared += 1

        # Gumloop: fix = verified + dead status
        if acc.get("gl_verified", 0) == 1 and acc.get("gl_status") in ("failed", "exhausted", "banned"):
            await db.update_account(acct_id, gl_status="none", gl_refresh_token="", gl_id_token="",
                                    gl_user_id="", gl_gummie_id="", gl_error="", gl_error_count=0,
                                    gl_verified=0)
            any_cleared = True
            cleared += 1

        # After cleanup, check if account has ANY alive provider left
        if any_cleared:
            refreshed = await db.get_account(acct_id)
            if refreshed:
                has_any = (
                    (refreshed.get("kiro_status") == "ok" and refreshed.get("kiro_access_token"))
                    or (refreshed.get("cb_status") == "ok" and refreshed.get("cb_api_key"))
                    or (refreshed.get("ws_status") == "ok" and refreshed.get("ws_api_key"))
                    or (refreshed.get("gl_status") == "ok" and refreshed.get("gl_refresh_token"))
                )
                # Also check if any provider is still pending/none (could be re-logged)
                has_pending = (
                    refreshed.get("kiro_status") in ("none", "pending")
                    or refreshed.get("cb_status") in ("none", "pending")
                    or refreshed.get("ws_status") in ("none", "pending")
                    or refreshed.get("gl_status") in ("none", "pending")
                )
                if not has_any and not has_pending:
                    await db.delete_account(acct_id)
                    deleted += 1

    return {"ok": True, "cleared": cleared, "deleted": deleted}


@router.post("/accounts/{account_id}/retry")
async def retry_login(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Retry failed login for an account."""
    result = await retry_account(account_id)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=404)
    return result


@router.put("/accounts/{account_id}")
async def update_account_fields(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Update account fields directly (for manual status changes)."""
    body = await request.json()
    allowed = {
        "status", "kiro_status", "kiro_error", "kiro_error_count",
        "cb_status", "cb_error", "cb_error_count",
        "ws_status", "ws_error", "ws_error_count",
        "gl_status", "gl_error", "gl_error_count",
    }
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        return JSONResponse({"error": "No valid fields"}, status_code=400)
    ok = await db.update_account(account_id, **fields)
    if not ok:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    return {"ok": True}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Permanently delete an account. Pushes deletion to D1 first."""
    account = await db.get_account(account_id)
    if not account:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    # Delete from D1 first (D1 = source of truth)
    try:
        from . import license_client
        await license_client.d1_delete_account(account["email"])
    except Exception:
        pass
    # Then delete locally
    await db.delete_account(account_id)
    return {"ok": True}


@router.post("/accounts/{account_id}/trash")
async def trash_account(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Move account to trash."""
    ok = await db.move_to_trash(account_id)
    if not ok:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    return {"ok": True}


@router.post("/accounts/{account_id}/restore")
async def restore_account(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Restore account from trash."""
    ok = await db.restore_account(account_id)
    if not ok:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    return {"ok": True}


@router.get("/accounts/refresh-credits")
async def refresh_all_credits_get(request: Request, _: bool = Depends(verify_admin)):
    """Refresh credits for all active accounts (GET)."""
    results = await _refresh_all_credits()
    trashed = await auto_trash()
    return {"results": results, "auto_trashed": trashed}


@router.post("/accounts/refresh-credits")
async def refresh_all_credits_post(request: Request, _: bool = Depends(verify_admin)):
    """Refresh credits for all active accounts (POST)."""
    results = await _refresh_all_credits()
    trashed = await auto_trash()
    return {"results": results, "auto_trashed": trashed}


@router.post("/accounts/{account_id}/refresh")
async def refresh_single_account(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Refresh credits for a single account."""
    account = await db.get_account(account_id)
    if account is None:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    result = await refresh_credits(account_id)
    return {"id": account_id, "email": account["email"], **result}


# ---------------------------------------------------------------------------
# Usage logs endpoints
# ---------------------------------------------------------------------------

@router.get("/logs")
async def get_logs(request: Request, _: bool = Depends(verify_admin), limit: int = 50):
    """Return recent usage logs with headers+body."""
    logs = await db.get_usage_logs(limit=min(limit, 500))
    return {"logs": logs, "count": len(logs)}


@router.get("/logs/{log_id}")
async def get_log_detail(log_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Return a single usage log with full detail."""
    entry = await db.get_usage_log(log_id)
    if entry is None:
        return JSONResponse({"error": "Log not found"}, status_code=404)
    return entry


# ---------------------------------------------------------------------------
# API key endpoints
# ---------------------------------------------------------------------------

@router.get("/keys")
async def list_keys(request: Request, _: bool = Depends(verify_admin)):
    """List all API keys (without full key values)."""
    keys = await db.get_api_keys()
    return {
        "keys": [
            {
                "id": k["id"],
                "key_prefix": k["key_prefix"],
                "name": k["name"],
                "active": bool(k["active"]),
                "created_at": k["created_at"],
                "last_used": k["last_used"],
                "usage_count": k["usage_count"],
            }
            for k in keys
        ]
    }


@router.post("/keys/generate")
async def generate_key(req: ApiKeyCreate, request: Request, _: bool = Depends(verify_admin)):
    """Generate a new API key. Returns the full key only once."""
    key_id, full_key = await db.create_api_key(req.name)
    return {
        "id": key_id,
        "key": full_key,
        "name": req.name,
        "message": "Save this key — it will not be shown again.",
    }


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Revoke (delete) an API key."""
    ok = await db.revoke_api_key(key_id)
    if not ok:
        return JSONResponse({"error": "Key not found"}, status_code=404)
    return {"ok": True}


@router.post("/keys/{key_id}/regenerate")
async def regenerate_key(key_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Regenerate an API key. Returns the new full key only once."""
    new_key = await db.regenerate_api_key(key_id)
    if new_key is None:
        return JSONResponse({"error": "Key not found"}, status_code=404)
    return {
        "id": key_id,
        "key": new_key,
        "message": "Save this key — it will not be shown again.",
    }


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@router.get("/settings/captcha")
async def get_captcha_settings(_: bool = Depends(verify_admin)):
    """Get captcha API key (masked) and stats."""
    key = await db.get_setting("captcha_api_key", "")
    from .proxy_gumloop import get_captcha_stats
    stats = get_captcha_stats()
    return {
        "has_key": bool(key),
        "key_preview": key[:6] + "***" + key[-4:] if len(key) > 10 else ("***" if key else ""),
        **stats,
    }


@router.post("/settings/captcha")
async def set_captcha_settings(request: Request, _: bool = Depends(verify_admin)):
    """Set captcha API key. Body: {api_key: str}."""
    body = await request.json()
    api_key = str(body.get("api_key", "")).strip()
    if not api_key:
        return JSONResponse({"error": "api_key required"}, status_code=400)
    await db.set_setting("captcha_api_key", api_key)
    # Update the live turnstile solver
    from .proxy_gumloop import _get_turnstile
    ts = _get_turnstile()
    ts.update_api_key(api_key)
    return {"ok": True, "has_key": True, "key_preview": api_key[:6] + "***" + api_key[-4:]}


@router.delete("/settings/captcha")
async def clear_captcha_settings(_: bool = Depends(verify_admin)):
    """Clear captcha API key."""
    await db.set_setting("captcha_api_key", "")
    from .proxy_gumloop import _get_turnstile
    ts = _get_turnstile()
    ts.update_api_key("")
    return {"ok": True}


@router.get("/stats")
async def get_stats(request: Request, _: bool = Depends(verify_admin)):
    """Usage statistics."""
    stats = await db.get_usage_stats()
    # Add captcha stats
    from .proxy_gumloop import get_captcha_stats
    stats["captcha"] = get_captcha_stats()
    return stats


# ---------------------------------------------------------------------------
# SSE events (batch progress)
# ---------------------------------------------------------------------------

@router.get("/events")
async def sse_events(request: Request, _: bool = Depends(verify_admin)):
    """SSE stream for batch login progress."""
    queue = batch_state.subscribe()

    async def event_stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                except asyncio.CancelledError:
                    break
        finally:
            batch_state.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Batch control
# ---------------------------------------------------------------------------

@router.post("/batch/start")
async def start_batch_endpoint(req: BatchLoginRequest, request: Request, _: bool = Depends(verify_admin)):
    """Start batch login."""
    accounts: list[tuple[str, str]] = []
    for line in req.accounts:
        line = line.strip()
        if not line or ":" not in line:
            continue
        parts = line.split(":", 1)
        accounts.append((parts[0].strip(), parts[1].strip()))

    if not accounts:
        return JSONResponse({"error": "No valid accounts"}, status_code=400)

    providers = [p for p in req.providers if p in ("kiro", "codebuddy", "wavespeed", "gumloop")]
    if not providers:
        return JSONResponse({"error": "No valid providers"}, status_code=400)

    try:
        count = await start_batch(accounts, providers, headless=req.headless,
                                  concurrency=max(1, req.concurrency))
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)

    return {"ok": True, "count": count}


@router.get("/batch/status")
async def batch_status_endpoint(request: Request, _: bool = Depends(verify_admin)):
    """Get batch status including failed jobs (not saved to DB)."""
    jobs = []
    failed_jobs = []
    for j in batch_state.jobs:
        job_info = {
            "id": j.id,
            "email": j.email,
            "providers": j.providers,
            "status": j.status,
            "logs_count": len(j.logs),
            "in_db": j.account_id is not None,
            "proxy_used": getattr(j, '_proxy_used', ''),
        }
        jobs.append(job_info)
        if j.status == "failed" and j.account_id is None:
            errors = {}
            if j.result:
                for prov in j.providers:
                    prov_result = j.result.get(prov, {})
                    if not prov_result.get("success"):
                        errors[prov] = prov_result.get("error", "Unknown error")
            failed_jobs.append({
                "email": j.email,
                "errors": errors,
                "providers": j.providers,
            })
    return {
        "running": batch_state.running,
        "jobs": jobs,
        "failed_jobs": failed_jobs,
    }


@router.post("/batch/cancel")
async def cancel_batch_endpoint(request: Request, _: bool = Depends(verify_admin)):
    """Cancel running batch."""
    cancel_batch()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Test model endpoints
# ---------------------------------------------------------------------------

async def _test_single_model(model: str, admin_password: str) -> dict:
    """Send a minimal test request to the proxy for a given model."""
    tier_enum = MODEL_TIER.get(model)
    tier_str = tier_enum.value if tier_enum else "unknown"

    # We need an API key to call the proxy. Get the first active one.
    keys = await db.get_api_keys()
    if not keys:
        return {
            "success": False, "model": model, "tier": tier_str,
            "latency_ms": 0, "response_headers": {}, "response_body": "",
            "error": "No API keys available for testing",
        }

    # Use the first active key's hash to find the actual key — we can't recover it.
    # Instead, generate a temporary key for testing.
    test_key_id, test_key = await db.create_api_key("_test_temp")

    start = time.monotonic()
    result: dict = {
        "success": False, "model": model, "tier": tier_str,
        "latency_ms": 0, "response_headers": {}, "response_body": "", "error": "",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"http://127.0.0.1:{LISTEN_PORT}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "Say OK"}],
                    "stream": False,
                    "max_tokens": 100,
                },
                headers={
                    "Authorization": f"Bearer {test_key}",
                    "Content-Type": "application/json",
                },
            )
            latency = int((time.monotonic() - start) * 1000)
            result["latency_ms"] = latency
            result["response_headers"] = dict(resp.headers)
            body_text = resp.text[:2000]
            result["response_body"] = body_text

            if resp.status_code == 200:
                result["success"] = True
            else:
                result["error"] = f"HTTP {resp.status_code}: {body_text[:500]}"

    except Exception as e:
        latency = int((time.monotonic() - start) * 1000)
        result["latency_ms"] = latency
        result["error"] = str(e)
    finally:
        # Clean up temp key
        await db.revoke_api_key(test_key_id)

    return result


@router.post("/test-model")
async def test_model(req: TestModelRequest, request: Request, _: bool = Depends(verify_admin)):
    """Test a single model by sending a minimal request through the proxy."""
    admin_pw = (
        request.headers.get("X-Admin-Password", "")
        or request.cookies.get("admin_password", "")
        or request.query_params.get("password", "")
    )
    result = await _test_single_model(req.model, admin_pw)
    return result


@router.post("/test-all-models")
async def test_all_models(request: Request, _: bool = Depends(verify_admin)):
    """Test all models sequentially."""
    admin_pw = (
        request.headers.get("X-Admin-Password", "")
        or request.cookies.get("admin_password", "")
        or request.query_params.get("password", "")
    )
    results: list[dict] = []
    for model_name in MODEL_TIER:
        if model_name in _HIDDEN_ALIASES:
            continue
        result = await _test_single_model(model_name, admin_pw)
        results.append(result)
    return {"results": results, "total": len(results), "success": sum(1 for r in results if r["success"])}


# ---------------------------------------------------------------------------
# Proxy config
# ---------------------------------------------------------------------------

@router.get("/proxy")
async def get_proxy(purpose: str = "api", _: bool = Depends(verify_admin)):
    """Get proxy config + all proxies, optionally filtered by purpose."""
    proxies = await db.get_proxies(purpose=purpose)
    if purpose == "batch":
        enabled = (await db.get_setting("batch_proxy_enabled", "false")).lower() == "true"
        smart_rotate = (await db.get_setting("batch_smart_rotate", "true")).lower() == "true"
    else:
        enabled = (await db.get_setting("proxy_enabled", "false")).lower() == "true"
        smart_rotate = (await db.get_setting("api_proxy_smart_rotate", "true")).lower() == "true"
    return {"proxies": proxies, "enabled": enabled, "smart_rotate": smart_rotate, "purpose": purpose}


@router.post("/proxy/toggle")
async def toggle_proxy_mode(request: Request, _: bool = Depends(verify_admin)):
    """Enable/disable proxy mode. Body: {enabled: bool}."""
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    await db.set_proxy_config(enabled)

    from .proxy_kiro import close_all_clients as close_kiro
    from .proxy_codebuddy import close_all_clients as close_cb
    from .proxy_wavespeed import close_all_clients as close_ws
    await close_kiro()
    await close_cb()
    await close_ws()

    return {"ok": True, "enabled": enabled}


def _detect_proxy_type(url: str) -> str:
    """Auto-detect proxy type from URL scheme."""
    lower = url.lower()
    if lower.startswith("socks5://") or lower.startswith("socks5h://"):
        return "socks5"
    if lower.startswith("socks4://"):
        return "socks4"
    return "http"


def _normalize_proxy_url(raw: str) -> str:
    """Normalize proxy URL format.

    Accepts:  scheme://host:port:user:pass
    Converts: scheme://user:pass@host:port

    Also accepts standard format (scheme://user:pass@host:port) as-is.
    """
    raw = raw.strip()
    if not raw:
        return raw
    # Already has @ → standard format, leave as-is
    if "@" in raw:
        return raw
    # Split scheme from rest
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
    else:
        scheme, rest = "http", raw
    # Split by colon: host:port or host:port:user:pass
    parts = rest.split(":")
    if len(parts) == 4:
        # host:port:user:pass → user:pass@host:port
        host, port, user, passwd = parts
        return f"{scheme}://{user}:{passwd}@{host}:{port}"
    elif len(parts) == 2:
        # host:port (no auth)
        return f"{scheme}://{rest}"
    else:
        # Unknown format, return as-is
        return raw


async def _test_proxy_url(url: str, timeout: int = 10) -> tuple[bool, int, str]:
    """Test a proxy URL. Returns (ok, latency_ms, error)."""
    import time as _time
    try:
        async with httpx.AsyncClient(proxy=url, timeout=timeout) as client:
            t0 = _time.monotonic()
            resp = await client.get("https://httpbin.org/ip")
            latency = int((_time.monotonic() - t0) * 1000)
        return True, latency, ""
    except Exception as e:
        return False, -1, str(e)[:200]


@router.post("/proxy/add")
async def add_proxy(request: Request, _: bool = Depends(verify_admin)):
    """Add proxy(s) with auto-test. Failed proxies are skipped.

    Body: {url, label?, type?, purpose?} or {urls: 'line\\nline', purpose?} for bulk.
    """
    body = await request.json()
    purpose = str(body.get("purpose", "api")).strip()
    skip_test = bool(body.get("skip_test", False))

    # Bulk mode: urls field with newline-separated proxies
    urls_raw = str(body.get("urls", "")).strip()
    if urls_raw:
        lines = [l.strip() for l in urls_raw.splitlines() if l.strip()]
        if not lines:
            return JSONResponse({"error": "No valid proxy URLs"}, status_code=400)
        added = []
        failed = []
        for line in lines:
            line = _normalize_proxy_url(line)
            ptype = _detect_proxy_type(line)
            if not skip_test:
                ok, latency, err = await _test_proxy_url(line)
                if not ok:
                    failed.append({"url": line, "type": ptype, "error": err})
                    continue
                pid = await db.add_proxy(line, "", ptype, purpose=purpose)
                await db.update_proxy_test(pid, latency)
                added.append({"id": pid, "url": line, "type": ptype, "latency_ms": latency})
            else:
                pid = await db.add_proxy(line, "", ptype, purpose=purpose)
                added.append({"id": pid, "url": line, "type": ptype})
        return {"ok": True, "added": added, "failed": failed,
                "count": len(added), "failed_count": len(failed)}

    # Single mode (backward compat)
    url = _normalize_proxy_url(str(body.get("url", "")).strip())
    if not url:
        return JSONResponse({"error": "url required"}, status_code=400)
    label = str(body.get("label", "")).strip()
    ptype = str(body.get("type", "")).strip() or _detect_proxy_type(url)
    if not skip_test:
        ok, latency, err = await _test_proxy_url(url)
        if not ok:
            return {"ok": False, "error": err, "url": url}
        pid = await db.add_proxy(url, label, ptype, purpose=purpose)
        await db.update_proxy_test(pid, latency)
        return {"ok": True, "id": pid, "latency_ms": latency}
    pid = await db.add_proxy(url, label, ptype, purpose=purpose)
    return {"ok": True, "id": pid}


@router.delete("/proxy/{proxy_id}")
async def delete_proxy(proxy_id: int, _: bool = Depends(verify_admin)):
    ok = await db.delete_proxy(proxy_id)
    return {"ok": ok}


@router.post("/proxy/delete-failed")
async def delete_failed_proxies(request: Request, _: bool = Depends(verify_admin)):
    """Delete all proxies with failed tests (latency = -1) for a given purpose."""
    body = await request.json()
    purpose = str(body.get("purpose", "api")).strip()
    proxies = await db.get_proxies(purpose=purpose)
    deleted = 0
    for p in proxies:
        if p.get("last_latency_ms", 0) == -1 and p.get("last_tested", ""):
            await db.delete_proxy(p["id"])
            deleted += 1
    return {"ok": True, "deleted": deleted}


@router.post("/proxy/edit-bulk")
async def edit_bulk_proxies(request: Request, _: bool = Depends(verify_admin)):
    """Replace all proxies for a purpose. Tests new/changed ones.

    Body: {urls: "line\\nline", purpose: "api"|"batch"}
    Deletes proxies not in the new list, adds new ones (with test), keeps unchanged.
    """
    body = await request.json()
    purpose = str(body.get("purpose", "api")).strip()
    urls_raw = str(body.get("urls", "")).strip()
    new_lines = [_normalize_proxy_url(l.strip()) for l in urls_raw.splitlines() if l.strip()]

    existing = await db.get_proxies(purpose=purpose)
    existing_urls = {p["url"]: p for p in existing}
    new_url_set = set(new_lines)

    # Delete proxies not in new list
    deleted = 0
    for p in existing:
        if p["url"] not in new_url_set:
            await db.delete_proxy(p["id"])
            deleted += 1

    # Add new proxies (test first)
    added = []
    failed = []
    for url in new_lines:
        if url in existing_urls:
            continue  # Already exists, skip
        ptype = _detect_proxy_type(url)
        ok, latency, err = await _test_proxy_url(url)
        if ok:
            pid = await db.add_proxy(url, "", ptype, purpose=purpose)
            await db.update_proxy_test(pid, latency)
            added.append({"id": pid, "url": url, "type": ptype, "latency_ms": latency})
        else:
            failed.append({"url": url, "error": err})

    return {
        "ok": True,
        "deleted": deleted,
        "added": added,
        "failed": failed,
        "kept": len(new_lines) - len(added) - len(failed),
        "count_added": len(added),
        "count_failed": len(failed),
    }


@router.post("/proxy/test-purpose")
async def test_proxies_by_purpose(request: Request, _: bool = Depends(verify_admin)):
    """Test all proxies for a specific purpose."""
    import time as _time
    body = await request.json()
    purpose = str(body.get("purpose", "api")).strip()
    proxies = await db.get_proxies(purpose=purpose)
    results = []
    for p in proxies:
        if not p.get("active", True):
            results.append({"id": p["id"], "url": p["url"], "ok": False, "error": "inactive", "latency_ms": -1})
            continue
        try:
            async with httpx.AsyncClient(proxy=p["url"], timeout=10) as client:
                t0 = _time.monotonic()
                resp = await client.get("https://httpbin.org/ip")
                latency = int((_time.monotonic() - t0) * 1000)
            await db.update_proxy_test(p["id"], latency)
            results.append({"id": p["id"], "url": p["url"], "ok": True, "latency_ms": latency})
        except Exception as e:
            await db.update_proxy_test(p["id"], -1, str(e)[:200])
            results.append({"id": p["id"], "url": p["url"], "ok": False, "error": str(e)[:200], "latency_ms": -1})
    return {"results": results}


@router.post("/proxy/delete-bulk")
async def delete_bulk_proxies(request: Request, _: bool = Depends(verify_admin)):
    """Delete multiple proxies by ID. Body: {ids: [1, 2, 3]}."""
    body = await request.json()
    ids = body.get("ids", [])
    deleted = 0
    for pid in ids:
        if await db.delete_proxy(int(pid)):
            deleted += 1
    return {"ok": True, "deleted": deleted}


@router.post("/proxy/{proxy_id}/toggle")
async def toggle_proxy(proxy_id: int, request: Request, _: bool = Depends(verify_admin)):
    body = await request.json()
    active = bool(body.get("active", True))
    ok = await db.toggle_proxy(proxy_id, active)

    from .proxy_kiro import close_all_clients as close_kiro
    from .proxy_codebuddy import close_all_clients as close_cb
    from .proxy_wavespeed import close_all_clients as close_ws
    await close_kiro()
    await close_cb()
    await close_ws()

    return {"ok": ok}


@router.post("/proxy/{proxy_id}/test")
async def test_single_proxy(proxy_id: int, _: bool = Depends(verify_admin)):
    """Test a single proxy by connecting to httpbin."""
    proxies = await db.get_proxies()
    proxy = next((p for p in proxies if p["id"] == proxy_id), None)
    if not proxy:
        return JSONResponse({"error": "Proxy not found"}, status_code=404)

    import time as _time
    try:
        async with httpx.AsyncClient(proxy=proxy["url"], timeout=10) as client:
            t0 = _time.monotonic()
            resp = await client.get("https://httpbin.org/ip")
            latency = int((_time.monotonic() - t0) * 1000)
        await db.update_proxy_test(proxy_id, latency)
        return {"ok": True, "latency_ms": latency, "status": resp.status_code, "ip": resp.json().get("origin", "")}
    except Exception as e:
        await db.update_proxy_test(proxy_id, -1, str(e)[:200])
        return {"ok": False, "error": str(e)[:200]}


@router.post("/proxy/test-all")
async def test_all_proxies(_: bool = Depends(verify_admin)):
    """Test all active proxies."""
    import time as _time
    proxies = await db.get_proxies()
    results = []
    for p in proxies:
        if not p["active"]:
            results.append({"id": p["id"], "url": p["url"], "ok": False, "error": "inactive", "latency_ms": -1})
            continue
        try:
            async with httpx.AsyncClient(proxy=p["url"], timeout=10) as client:
                t0 = _time.monotonic()
                resp = await client.get("https://httpbin.org/ip")
                latency = int((_time.monotonic() - t0) * 1000)
            await db.update_proxy_test(p["id"], latency)
            results.append({"id": p["id"], "url": p["url"], "ok": True, "latency_ms": latency})
        except Exception as e:
            await db.update_proxy_test(p["id"], -1, str(e)[:200])
            results.append({"id": p["id"], "url": p["url"], "ok": False, "error": str(e)[:200], "latency_ms": -1})
    return {"results": results}


@router.post("/proxy/{proxy_id}/check")
async def check_proxy(proxy_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Toggle checked state of a proxy. Body: {checked: bool, purpose?: str}."""
    body = await request.json()
    checked = bool(body.get("checked", True))
    purpose = str(body.get("purpose", "api")).strip()
    await db.toggle_proxy_checked(proxy_id, checked, purpose)
    return {"ok": True}


@router.post("/proxy/smart-rotate")
async def set_smart_rotate(request: Request, _: bool = Depends(verify_admin)):
    """Enable/disable smart-rotate for a purpose. Body: {purpose, enabled}."""
    body = await request.json()
    purpose = str(body.get("purpose", "api")).strip()
    enabled = bool(body.get("enabled", True))

    if purpose == "batch":
        await db.set_setting("batch_smart_rotate", "true" if enabled else "false")
    else:
        await db.set_setting("api_proxy_smart_rotate", "true" if enabled else "false")

    # Just save the mode — don't touch checkbox state
    return {"ok": True, "smart_rotate": enabled}


@router.post("/proxy/batch-toggle")
async def batch_proxy_toggle(request: Request, _: bool = Depends(verify_admin)):
    """Enable/disable the batch proxy pool. Body: {enabled: bool}."""
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    await db.set_setting("batch_proxy_enabled", "true" if enabled else "false")
    return {"ok": True, "enabled": enabled}


# ---------------------------------------------------------------------------
# Account verification (temporary → fix)
# ---------------------------------------------------------------------------

@router.post("/accounts/test-batch")
async def test_batch_accounts(request: Request, _: bool = Depends(verify_admin)):
    """Test all temporary exhausted/banned accounts with a small request.

    Sends a tiny prompt to each provider. If error → show error log for review.
    Uses smart proxy rotation if enabled.
    """
    import time as _time

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    providers_filter = body.get("providers", ["kiro", "codebuddy", "wavespeed"])

    accounts = await db.get_accounts()
    results = []

    for acc in accounts:
        acct_id = acc["id"]
        email = acc["email"]

        # Test each provider that is exhausted/banned and NOT yet verified
        for provider, status_col, verified_col, test_err_col in [
            ("kiro", "kiro_status", "kiro_verified", "kiro_test_error"),
            ("codebuddy", "cb_status", "cb_verified", "cb_test_error"),
            ("wavespeed", "ws_status", "ws_verified", "ws_test_error"),
            ("gumloop", "gl_status", "gl_verified", "gl_test_error"),
        ]:
            if provider not in providers_filter:
                continue
            status = acc.get(status_col, "")
            verified = acc.get(verified_col, 0)
            if status not in ("exhausted", "banned", "failed", "rate_limited") or verified == 1:
                continue

            # Get proxy for this test (smart rotate)
            proxy_url = await db.get_next_proxy_url()

            # Send test request
            test_result = await _test_account_provider(acc, provider, proxy_url)
            error_msg = test_result.get("error", "")

            # Store test error for review
            await db.update_account(acct_id, **{test_err_col: error_msg})

            # Auto-restore on success: set status back to ok, clear errors
            auto_approved = False
            if test_result["ok"]:
                restore_fields = {status_col: "ok", verified_col: 0, test_err_col: ""}
                if provider == "kiro":
                    restore_fields.update(kiro_error="", kiro_error_count=0)
                elif provider == "codebuddy":
                    restore_fields.update(cb_error="", cb_error_count=0)
                elif provider == "wavespeed":
                    restore_fields.update(ws_error="", ws_error_count=0)
                elif provider == "gumloop":
                    restore_fields.update(gl_error="", gl_error_count=0)
                await db.update_account(acct_id, **restore_fields)
            else:
                # Auto-approve (mark as verified fix) if response clearly indicates exhausted/banned
                exhaust_signals = [
                    "credits exhausted", "credit exhausted", "quota exceeded",
                    "code: 14018", "14018", "account suspended", "account banned",
                    "account disabled", "no remaining", "limit reached",
                ]
                error_lower = error_msg.lower()
                if any(sig in error_lower for sig in exhaust_signals):
                    await db.update_account(acct_id, **{verified_col: 1, test_err_col: error_msg})
                    auto_approved = True

            results.append({
                "account_id": acct_id,
                "email": email,
                "provider": provider,
                "status": status,
                "test_ok": test_result["ok"],
                "restored": test_result["ok"],
                "auto_approved": auto_approved,
                "error": error_msg,
                "proxy_used": proxy_url or "direct",
                "latency_ms": test_result.get("latency_ms", 0),
            })

    return {"results": results, "total": len(results)}


async def _test_account_provider(acc: dict, provider: str, proxy_url: str | None) -> dict:
    """Test account by calling the provider proxy directly (bypasses routing).

    Calls the provider-specific proxy function with the exact account,
    then logs the result to usage_logs so it appears in the Logs tab.
    """
    import time as _time

    acct_id = acc["id"]
    email = acc.get("email", "?")

    if provider == "codebuddy":
        cb_key = acc.get("cb_api_key", "")
        if not cb_key:
            return {"ok": False, "error": "No CB API key"}
        from .proxy_codebuddy import proxy_chat_completions as cb_proxy
        body = {"model": "claude-opus-4.6", "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 100, "stream": False}
        proxy_info = await db.get_proxy_for_api_call()
        px = proxy_info["url"] if proxy_info else None
        t0 = _time.monotonic()
        try:
            response, credit = await cb_proxy(body, cb_key, False, proxy_url=px)
            latency = int((_time.monotonic() - t0) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            # Log it
            resp_body = ""
            if hasattr(response, "body"):
                try:
                    resp_body = response.body.decode("utf-8", errors="replace")[:2000]
                except Exception:
                    pass
            await db.log_usage(None, acct_id, "claude-opus-4.6", "max", status, latency,
                               request_body='{"test":"batch"}', response_body=resp_body, proxy_url=px or "")
            if status == 200:
                return {"ok": True, "latency_ms": latency}
            error = f"HTTP {status}"
            try:
                err_data = json.loads(resp_body)
                error = err_data.get("error", {}).get("message", error) if isinstance(err_data, dict) else error
            except Exception:
                pass
            return {"ok": False, "error": error[:300], "latency_ms": latency}
        except Exception as e:
            latency = int((_time.monotonic() - t0) * 1000)
            await db.log_usage(None, acct_id, "claude-opus-4.6", "max", 502, latency,
                               request_body='{"test":"batch"}', error_message=str(e)[:200], proxy_url=proxy_url or "")
            return {"ok": False, "error": str(e)[:300], "latency_ms": latency}

    elif provider == "kiro":
        if not acc.get("kiro_access_token"):
            return {"ok": False, "error": "No kiro access token"}
        from .proxy_kiro import proxy_chat_completions as kiro_proxy
        from fastapi import Request as _Req
        body = json.dumps({"model": "auto", "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 16, "stream": False}).encode()
        proxy_info = await db.get_proxy_for_api_call()
        px = proxy_info["url"] if proxy_info else None
        t0 = _time.monotonic()
        try:
            # kiro_proxy needs a Request object — create a minimal mock
            response = await kiro_proxy(None, body, account=acc, is_stream=False, proxy_url=px)
            latency = int((_time.monotonic() - t0) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            await db.log_usage(None, acct_id, "auto", "standard", status, latency,
                               request_body='{"test":"batch"}', proxy_url=px or "")
            if status == 200:
                return {"ok": True, "latency_ms": latency}
            return {"ok": False, "error": f"HTTP {status}", "latency_ms": latency}
        except Exception as e:
            latency = int((_time.monotonic() - t0) * 1000)
            return {"ok": False, "error": str(e)[:300], "latency_ms": latency}

    elif provider == "wavespeed":
        ws_key = acc.get("ws_api_key", "")
        if not ws_key:
            return {"ok": False, "error": "No WS API key"}
        from .proxy_wavespeed import proxy_chat_completions as ws_proxy
        body = {"model": "new-claude-sonnet-4", "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 16, "stream": False}
        proxy_info = await db.get_proxy_for_api_call()
        px = proxy_info["url"] if proxy_info else None
        t0 = _time.monotonic()
        try:
            response, cost = await ws_proxy(body, ws_key, False, proxy_url=px)
            latency = int((_time.monotonic() - t0) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            await db.log_usage(None, acct_id, "new-claude-sonnet-4", "wavespeed", status, latency,
                               request_body='{"test":"batch"}', proxy_url=px or "")
            if status == 200:
                return {"ok": True, "latency_ms": latency}
            return {"ok": False, "error": f"HTTP {status}", "latency_ms": latency}
        except Exception as e:
            latency = int((_time.monotonic() - t0) * 1000)
            return {"ok": False, "error": str(e)[:300], "latency_ms": latency}

    elif provider == "gumloop":
        if not acc.get("gl_refresh_token") or not acc.get("gl_gummie_id"):
            return {"ok": False, "error": "No GL credentials"}
        from .proxy_gumloop import proxy_chat_completions as gl_proxy
        body = {"model": "gl-claude-sonnet-4-5", "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 50, "stream": False}
        t0 = _time.monotonic()
        try:
            response, cost = await gl_proxy(body, acc, False)
            latency = int((_time.monotonic() - t0) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            await db.log_usage(None, acct_id, "gl-claude-sonnet-4-5", "max_gl", status, latency,
                               request_body='{"test":"batch"}')
            if status == 200:
                return {"ok": True, "latency_ms": latency}
            return {"ok": False, "error": f"HTTP {status}", "latency_ms": latency}
        except Exception as e:
            latency = int((_time.monotonic() - t0) * 1000)
            return {"ok": False, "error": str(e)[:300], "latency_ms": latency}

    return {"ok": False, "error": f"Unknown provider: {provider}"}


@router.post("/accounts/{account_id}/approve/{provider}")
async def approve_account(account_id: int, provider: str, _: bool = Depends(verify_admin)):
    """Approve a temporary exhausted/banned account as fix (confirmed dead)."""
    col_map = {
        "kiro": "kiro_verified",
        "codebuddy": "cb_verified",
        "wavespeed": "ws_verified",
        "gumloop": "gl_verified",
    }
    col = col_map.get(provider)
    if not col:
        return JSONResponse({"error": f"Unknown provider: {provider}"}, status_code=400)
    await db.update_account(account_id, **{col: 1})
    return {"ok": True}


@router.post("/accounts/approve-all")
async def approve_all_accounts(request: Request, _: bool = Depends(verify_admin)):
    """Bulk approve all temporary exhausted/banned as fix."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    provider = body.get("provider")  # optional: filter by provider

    accounts = await db.get_accounts()
    approved = 0
    for acc in accounts:
        for prov, status_col, verified_col in [
            ("kiro", "kiro_status", "kiro_verified"),
            ("codebuddy", "cb_status", "cb_verified"),
            ("wavespeed", "ws_status", "ws_verified"),
            ("gumloop", "gl_status", "gl_verified"),
        ]:
            if provider and prov != provider:
                continue
            status = acc.get(status_col, "")
            verified = acc.get(verified_col, 0)
            if status in ("exhausted", "banned", "failed", "rate_limited") and verified == 0:
                await db.update_account(acc["id"], **{verified_col: 1})
                approved += 1

    return {"ok": True, "approved": approved}


# ---------------------------------------------------------------------------
# Filter rules
# ---------------------------------------------------------------------------

@router.get("/global-filters")
async def list_global_filters(_: bool = Depends(verify_admin)):
    """List global filter rules from central admin (cached from last sync)."""
    from . import license_client
    # Return cached global filters from last sync pull
    gf = getattr(license_client, '_global_filters', [])
    return {"global_filters": gf, "count": len(gf)}


@router.get("/filters")
async def list_filters(_: bool = Depends(verify_admin)):
    """List all filter rules."""
    filters = await db.get_filters()
    return {"filters": filters, "count": len(filters)}


@router.post("/filters")
async def create_filter_endpoint(request: Request, _: bool = Depends(verify_admin)):
    """Create a new filter rule."""
    body = await request.json()
    find_text = str(body.get("find_text", "")).strip()
    if not find_text:
        return JSONResponse({"error": "find_text is required"}, status_code=400)
    replace_text = str(body.get("replace_text", ""))
    is_regex = bool(body.get("is_regex", False))
    description = str(body.get("description", "")).strip()

    if is_regex:
        try:
            re.compile(find_text)
        except re.error as e:
            return JSONResponse({"error": f"Invalid regex: {e}"}, status_code=400)

    filter_id = await db.create_filter(find_text, replace_text, is_regex, description)
    from .message_filter import invalidate_cache
    invalidate_cache()
    return {"ok": True, "id": filter_id}


@router.put("/filters/{filter_id}")
async def update_filter_endpoint(filter_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Update a filter rule."""
    body = await request.json()
    fields: dict = {}
    if "find_text" in body:
        fields["find_text"] = str(body["find_text"]).strip()
    if "replace_text" in body:
        fields["replace_text"] = str(body["replace_text"])
    if "is_regex" in body:
        fields["is_regex"] = 1 if body["is_regex"] else 0
    if "enabled" in body:
        fields["enabled"] = 1 if body["enabled"] else 0
    if "description" in body:
        fields["description"] = str(body["description"]).strip()

    if not fields:
        return JSONResponse({"error": "No fields to update"}, status_code=400)

    if fields.get("is_regex") and "find_text" in fields:
        try:
            re.compile(fields["find_text"])
        except re.error as e:
            return JSONResponse({"error": f"Invalid regex: {e}"}, status_code=400)

    ok = await db.update_filter(filter_id, **fields)
    if not ok:
        return JSONResponse({"error": "Filter not found"}, status_code=404)
    from .message_filter import invalidate_cache
    invalidate_cache()
    return {"ok": True}


@router.delete("/filters/{filter_id}")
async def delete_filter_endpoint(filter_id: int, _: bool = Depends(verify_admin)):
    """Delete a filter rule."""
    ok = await db.delete_filter(filter_id)
    if not ok:
        return JSONResponse({"error": "Filter not found"}, status_code=404)
    from .message_filter import invalidate_cache
    invalidate_cache()
    return {"ok": True}


@router.post("/filters/{filter_id}/toggle")
async def toggle_filter_endpoint(filter_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Toggle a filter rule on/off."""
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    ok = await db.toggle_filter(filter_id, enabled)
    if not ok:
        return JSONResponse({"error": "Filter not found"}, status_code=404)
    from .message_filter import invalidate_cache
    invalidate_cache()
    return {"ok": True}


@router.post("/filters/seed")
async def seed_filters_endpoint(request: Request, _: bool = Depends(verify_admin)):
    """Seed default filter rules. Send {force: true} to replace all existing rules."""
    force = False
    try:
        body = await request.json()
        force = bool(body.get("force", False))
    except Exception:
        pass
    count = await db.seed_default_filters(force=force)
    from .message_filter import invalidate_cache
    invalidate_cache()
    return {"ok": True, "seeded": count}

