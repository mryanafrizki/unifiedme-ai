"""Account manager — rotation, error tracking, credit refresh."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx

from . import database as db
from .config import Tier, KIRO_UPSTREAM, CODEBUDDY_UPSTREAM, KIRO_ADMIN_PASSWORD

log = logging.getLogger("unified.account_manager")

MAX_CONSECUTIVE_ERRORS = 3


# ---------------------------------------------------------------------------
# Account rotation
# ---------------------------------------------------------------------------

async def get_next_account(tier: Tier, exclude_ids: list[int] | None = None) -> Optional[dict]:
    """Get the next available account for the given tier.

    Pass exclude_ids to skip already-tried accounts in retry loops.
    """
    return await db.get_next_account_for_tier(tier.value, exclude_ids=exclude_ids)


# ---------------------------------------------------------------------------
# Error / success tracking
# ---------------------------------------------------------------------------

async def mark_account_error(account_id: int, tier: Tier, error: str) -> None:
    """Increment error count for the tier. Auto-ban after MAX_CONSECUTIVE_ERRORS."""
    account = await db.get_account(account_id)
    if account is None:
        return

    tier_config = {
        Tier.STANDARD: ("kiro_error", "kiro_error_count", "kiro_status"),
        Tier.MAX: ("cb_error", "cb_error_count", "cb_status"),
        Tier.WAVESPEED: ("ws_error", "ws_error_count", "ws_status"),
        Tier.MAX_GL: ("gl_error", "gl_error_count", "gl_status"),
        Tier.CHATBAI: ("cbai_error", "cbai_error_count", "cbai_status"),
        Tier.SKILLBOSS: ("skboss_error", "skboss_error_count", "skboss_status"),
        Tier.WINDSURF: ("windsurf_error", "windsurf_error_count", "windsurf_status"),
    }
    err_col, cnt_col, status_col = tier_config.get(tier, ("kiro_error", "kiro_error_count", "kiro_status"))

    new_count = account.get(cnt_col, 0) + 1
    updates: dict = {err_col: error, cnt_col: new_count}
    if new_count >= MAX_CONSECUTIVE_ERRORS:
        updates[status_col] = "failed"
        log.warning("Account %s %s auto-failed after %d errors: %s", account["email"], tier.value, new_count, error)

    await db.update_account(account_id, **updates)

    # Clear sticky pointer so next request picks the next oldest account
    await db.clear_sticky_account(tier.value)

    # Instant push to D1 on auto-fail (critical change)
    if new_count >= MAX_CONSECUTIVE_ERRORS:
        try:
            from . import license_client
            updated = await db.get_account(account_id)
            if updated:
                await license_client.push_account_now(updated)
        except Exception:
            pass


async def mark_account_success(account_id: int, tier: Tier) -> None:
    """Reset error count on successful request."""
    tier_config = {
        Tier.STANDARD: {"kiro_error_count": 0, "kiro_error": ""},
        Tier.MAX: {"cb_error_count": 0, "cb_error": ""},
        Tier.WAVESPEED: {"ws_error_count": 0, "ws_error": ""},
        Tier.MAX_GL: {"gl_error_count": 0, "gl_error": ""},
        Tier.CHATBAI: {"cbai_error_count": 0, "cbai_error": ""},
        Tier.SKILLBOSS: {"skboss_error_count": 0, "skboss_error": ""},
        Tier.WINDSURF: {"windsurf_error_count": 0, "windsurf_error": ""},
    }
    updates = tier_config.get(tier, {})
    if updates:
        await db.update_account(account_id, **updates)


# ---------------------------------------------------------------------------
# Credit refresh
# ---------------------------------------------------------------------------

async def refresh_credits(account_id: int) -> dict:
    """Refresh credit/quota info for an account from upstream APIs.

    Returns dict with kiro_credits and cb_credits (or error messages).
    """
    account = await db.get_account(account_id)
    if account is None:
        return {"error": "Account not found"}

    result: dict = {}

    # Kiro credits
    kiro_result = await refresh_kiro_credits(account_id)
    result.update(kiro_result)

    # CodeBuddy credits
    cb_result = await refresh_cb_credits(account_id)
    result.update(cb_result)

    # Health check
    await check_account_health(account_id)

    return result


async def refresh_kiro_credits(account_id: int) -> dict:
    """Refresh Kiro credits by calling the Kiro getUsageLimits API directly.

    Uses the account's access_token to query
    https://q.{region}.amazonaws.com/getUsageLimits
    """
    account = await db.get_account(account_id)
    if account is None:
        return {"kiro_error": "Account not found"}

    if account["kiro_status"] not in ("ok", "exhausted", "pending"):
        return {"kiro_status": account["kiro_status"]}

    access_token = account.get("kiro_access_token", "")
    if not access_token:
        return {"kiro_status": account["kiro_status"], "kiro_error": "No access token"}

    KIRO_DEFAULT_CREDITS = 550.0
    result: dict = {}

    # Build usage URL
    from urllib.parse import quote
    profile_arn = account.get("kiro_profile_arn", "")
    region = "us-east-1"
    if profile_arn:
        parts = profile_arn.split(":")
        if len(parts) >= 4 and parts[3]:
            import re
            if re.match(r'^[a-z]+-[a-z]+-\d+$', parts[3]):
                region = parts[3]

    usage_url = f"https://q.{region}.amazonaws.com/getUsageLimits"
    params = ["origin=AI_EDITOR", "resourceType=AGENTIC_REQUEST"]
    if profile_arn:
        params.append(f"profileArn={quote(profile_arn, safe='')}")
    usage_url += "?" + "&".join(params)

    try:
        # Try with current token, refresh if 401/403
        for attempt in range(2):
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    usage_url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                        "User-Agent": "enowXGateway/1.0.0",
                    },
                )

                if resp.status_code == 200:
                    payload = resp.json()
                    break
                elif resp.status_code in (401, 403) and attempt == 0:
                    # Try refreshing token
                    from .kiro.auth import KiroAuthManager
                    auth_mgr = KiroAuthManager(
                        access_token=access_token,
                        refresh_token=account.get("kiro_refresh_token", ""),
                        profile_arn=profile_arn,
                        region=region,
                    )
                    try:
                        access_token = await auth_mgr.force_refresh()
                        # Persist refreshed tokens
                        updated = auth_mgr.get_updated_tokens()
                        if updated:
                            await db.update_account(account_id, **updated)
                        continue
                    except Exception as refresh_err:
                        log.warning("Token refresh failed for %s: %s", account["email"], refresh_err)
                        raise ValueError(f"Auth failed ({resp.status_code}) and refresh failed: {refresh_err}")
                else:
                    raise ValueError(f"getUsageLimits HTTP {resp.status_code}: {resp.text[:200]}")
        else:
            raise ValueError("getUsageLimits failed after retry")

        # Parse usage payload (same format as app/providers/kiro.py)
        usage_breakdown = payload.get("usageBreakdownList") or []
        if not usage_breakdown:
            raise ValueError("Empty usageBreakdownList")

        usage = usage_breakdown[0] or {}
        subscription_type = str(payload.get("subscriptionType") or "").strip()
        usage_limit = float(usage.get("usageLimit") or 0)
        current_usage = float(usage.get("currentUsage") or 0)

        total_credits = usage_limit
        total_usage = current_usage

        # Free trial
        free_trial = usage.get("freeTrialInfo") or {}
        free_trial_status = str(free_trial.get("freeTrialStatus") or "").strip()
        if free_trial_status.upper() == "ACTIVE":
            total_credits += float(free_trial.get("usageLimit") or 0)
            total_usage += float(free_trial.get("currentUsage") or 0)

        # Bonuses
        for bonus in usage.get("bonuses") or []:
            total_credits += float((bonus or {}).get("usageLimit") or 0)
            total_usage += float((bonus or {}).get("currentUsage") or 0)

        remaining = max(0.0, total_credits - total_usage)
        new_status = "exhausted" if remaining <= 0 else "ok"

        await db.update_account_credits(
            account_id,
            kiro_credits=remaining, kiro_credits_total=total_credits,
            kiro_credits_used=total_usage, kiro_status=new_status,
        )
        result.update(
            kiro_credits=remaining, kiro_credits_total=total_credits,
            kiro_credits_used=total_usage, kiro_status=new_status,
            kiro_subscription=subscription_type, kiro_trial_status=free_trial_status,
        )

    except Exception as e:
        log.warning("Kiro credit fetch failed for %s: %s — using default %d",
                    account["email"], e, KIRO_DEFAULT_CREDITS)
        current = float(account.get("kiro_credits", 0))
        total = float(account.get("kiro_credits_total", 0))
        if current <= 0 and account.get("kiro_access_token"):
            await db.update_account_credits(
                account_id,
                kiro_credits=KIRO_DEFAULT_CREDITS, kiro_credits_total=KIRO_DEFAULT_CREDITS,
                kiro_credits_used=0, kiro_status="ok",
            )
            result.update(kiro_credits=KIRO_DEFAULT_CREDITS, kiro_credits_total=KIRO_DEFAULT_CREDITS,
                          kiro_status="ok")
        else:
            result.update(kiro_credits=current, kiro_credits_total=total or KIRO_DEFAULT_CREDITS,
                          kiro_status=account.get("kiro_status", "ok"))

    return result


async def refresh_cb_credits(account_id: int) -> dict:
    """Refresh CodeBuddy credit status.

    CodeBuddy doesn't have a direct credit API. We check:
    - If cb_api_key exists → mark as 'ok'
    - If cb_expires_at has passed → mark as 'expired'
    """
    account = await db.get_account(account_id)
    if account is None:
        return {"cb_error": "Account not found"}

    result: dict = {}

    if not account.get("cb_api_key"):
        result["cb_status"] = account["cb_status"]
        return result

    CB_DEFAULT_CREDITS = 250.0
    new_status = "ok"
    cb_credits = float(account.get("cb_credits", 0))

    # Initialize credits to 250 if not set yet
    if cb_credits <= 0 and account.get("cb_api_key") and account.get("cb_status") in ("ok", "pending"):
        cb_credits = CB_DEFAULT_CREDITS
        await db.update_account(account_id, cb_credits=CB_DEFAULT_CREDITS)
        log.info("Account %s CB credits initialized to %.0f", account["email"], CB_DEFAULT_CREDITS)
    elif cb_credits <= 0 and account.get("cb_api_key"):
        new_status = "exhausted"
        log.info("Account %s CB credits exhausted", account["email"])

    # Check expiry
    cb_expires_at = account.get("cb_expires_at", "")
    if cb_expires_at:
        try:
            expires_dt = datetime.fromisoformat(cb_expires_at)
            if datetime.utcnow() > expires_dt:
                new_status = "expired"
                log.info("Account %s CB key expired at %s", account["email"], cb_expires_at)
        except (ValueError, TypeError):
            pass

    # Only update if status changed or was pending
    if account["cb_status"] in ("pending", "ok", "expired", "exhausted") or new_status != account["cb_status"]:
        await db.update_account_credits(account_id, cb_status=new_status)

    result["cb_status"] = new_status
    result["cb_credits"] = cb_credits
    result["cb_expires_at"] = cb_expires_at
    return result


async def refresh_all_credits() -> list[dict]:
    """Refresh credits for all active accounts."""
    accounts = await db.get_accounts(status="active")
    results: list[dict] = []
    for acc in accounts:
        r = await refresh_credits(acc["id"])
        results.append({"id": acc["id"], "email": acc["email"], **r})
    return results


async def check_account_health(account_id: int) -> dict:
    """Check account health and update statuses accordingly.

    - Kiro credits exhausted → mark exhausted
    - CB key expired → mark expired
    - Banned → mark banned
    """
    account = await db.get_account(account_id)
    if account is None:
        return {"error": "Account not found"}

    updates: dict = {}

    # Check Kiro
    if account.get("kiro_credits", 0) <= 0 and account["kiro_status"] == "ok":
        updates["kiro_status"] = "exhausted"

    # Check CB expiry
    cb_expires_at = account.get("cb_expires_at", "")
    if cb_expires_at:
        try:
            expires_dt = datetime.fromisoformat(cb_expires_at)
            if datetime.utcnow() > expires_dt and account["cb_status"] == "ok":
                updates["cb_status"] = "expired"
        except (ValueError, TypeError):
            pass

    # Apply updates
    if updates:
        await db.update_account(account_id, **updates)

    # Check if all dead → move to failed
    refreshed = await db.get_account(account_id)
    if refreshed:
        kiro_dead = refreshed["kiro_status"] in ("failed", "exhausted", "banned")
        cb_dead = refreshed["cb_status"] in ("failed", "exhausted", "expired", "banned")
        ws_dead = refreshed.get("ws_status", "none") in ("none", "failed", "exhausted", "banned")
        gl_dead = refreshed.get("gl_status", "none") in ("none", "failed", "exhausted", "banned")
        cbai_dead = refreshed.get("cbai_status", "none") in ("none", "failed", "exhausted", "banned")
        skboss_dead = refreshed.get("skboss_status", "none") in ("none", "failed", "exhausted", "banned")
        windsurf_dead = refreshed.get("windsurf_status", "none") in ("none", "failed", "exhausted", "banned")
        if kiro_dead and cb_dead and ws_dead and gl_dead and cbai_dead and skboss_dead and windsurf_dead:
            await db.update_account(account_id, status="failed")
            updates["status"] = "failed"

    return updates


async def auto_trash() -> int:
    """Move exhausted/banned accounts to trash. Returns count moved."""
    accounts = await db.get_accounts(status="active")
    moved = 0
    for acc in accounts:
        kiro_dead = acc["kiro_status"] in ("failed", "exhausted")
        cb_dead = acc["cb_status"] in ("failed", "exhausted")
        ws_dead = acc.get("ws_status", "none") in ("none", "failed", "exhausted")
        gl_dead = acc.get("gl_status", "none") in ("none", "failed", "exhausted")
        cbai_dead = acc.get("cbai_status", "none") in ("none", "failed", "exhausted")
        skboss_dead = acc.get("skboss_status", "none") in ("none", "failed", "exhausted")
        windsurf_dead = acc.get("windsurf_status", "none") in ("none", "failed", "exhausted")
        if kiro_dead and cb_dead and ws_dead and gl_dead and cbai_dead and skboss_dead and windsurf_dead:
            await db.move_to_trash(acc["id"])
            moved += 1
            log.info("Auto-trashed account %s (both tiers dead)", acc["email"])
    return moved
