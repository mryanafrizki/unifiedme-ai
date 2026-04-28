"""License client — connects to central D1 API for license validation, sync, and alerts.

Handles:
- Startup license activation + device binding
- Periodic sync (pull accounts/settings/watchwords, push usage/alerts)
- Watchword scanning on message content
- Device fingerprint generation
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import platform
import re
import time
import uuid
from typing import Any, Optional

import httpx

log = logging.getLogger("unified.license_client")

# ─── Configuration ───────────────────────────────────────────────────────────

CENTRAL_API_URL = os.getenv("CENTRAL_API_URL", "https://unified-api.roubot71.workers.dev")
LICENSE_KEY = os.getenv("LICENSE_KEY", "")
DEVICE_NAME = os.getenv("DEVICE_NAME", platform.node() or "unknown")
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "120"))  # 2 minutes heartbeat

# ─── State ───────────────────────────────────────────────────────────────────

_license_info: Optional[dict] = None
_device_id: Optional[int] = None
_device_fingerprint: str = ""
_watchwords: list[dict] = []
_watchword_cache_ts: float = 0
_global_filters: list[dict] = []

# Usage log buffer (accumulated between syncs)
_usage_buffer: list[dict] = []
_alert_buffer: list[dict] = []
_alert_dedup: dict[tuple, float] = {}
_ALERT_COOLDOWN = 300  # 5 min per keyword per license

_sync_task: Optional[asyncio.Task] = None


# ─── Device Fingerprint ─────────────────────────────────────────────────────

def _get_machine_id() -> str:
    """Get a machine-specific ID (best effort)."""
    try:
        if platform.system() == "Linux":
            for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
                if os.path.exists(path):
                    with open(path) as f:
                        return f.read().strip()
        elif platform.system() == "Windows":
            import subprocess
            # Try PowerShell first (wmic is deprecated)
            try:
                result = subprocess.run(
                    ["powershell", "-Command", "(Get-CimInstance Win32_ComputerSystemProduct).UUID"],
                    capture_output=True, text=True, timeout=10
                )
                uid = result.stdout.strip()
                if uid and uid != "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF":
                    return uid
            except Exception:
                pass
            # Fallback to wmic
            result = subprocess.run(
                ["wmic", "csproduct", "get", "UUID"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().split("\n"):
                line = line.strip().replace("\r", "")
                if line and line.upper() != "UUID":
                    return line
        elif platform.system() == "Darwin":
            import subprocess
            result = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split("\n"):
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
    except Exception:
        pass
    return ""


def _generate_fingerprint() -> str:
    """Generate a stable device fingerprint from hostname + OS + machine ID."""
    parts = [
        platform.node(),
        platform.system(),
        platform.machine(),
        platform.processor(),
    ]
    # Try to get a machine-specific ID
    machine_id = ""
    try:
        if platform.system() == "Linux":
            for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
                if os.path.exists(path):
                    with open(path) as f:
                        machine_id = f.read().strip()
                    break
        elif platform.system() == "Windows":
            import subprocess
            result = subprocess.run(
                ["wmic", "csproduct", "get", "UUID"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line and line != "UUID":
                    machine_id = line
                    break
        elif platform.system() == "Darwin":
            import subprocess
            result = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split("\n"):
                if "IOPlatformUUID" in line:
                    machine_id = line.split('"')[-2]
                    break
    except Exception:
        pass

    if machine_id:
        parts.append(machine_id)

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()


# ─── API Helpers ─────────────────────────────────────────────────────────────

async def _api_post(path: str, data: dict, timeout: int = 15) -> dict:
    """POST to central API. Returns response dict or {error: ...}."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{CENTRAL_API_URL}{path}",
                json=data,
                headers={"Content-Type": "application/json"},
            )
            return resp.json()
    except Exception as e:
        log.warning("Central API POST %s failed: %s", path, e)
        return {"error": str(e)}


async def _api_get(path: str, params: dict | None = None, timeout: int = 15) -> dict:
    """GET from central API. Returns response dict or {error: ...}."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{CENTRAL_API_URL}{path}",
                params=params or {},
            )
            return resp.json()
    except Exception as e:
        log.warning("Central API GET %s failed: %s", path, e)
        return {"error": str(e)}


# ─── License Activation ─────────────────────────────────────────────────────

async def activate() -> bool:
    """Activate license on startup. Returns True if successful.

    Call this once during app lifespan startup.
    If LICENSE_KEY is empty, license system is disabled (local-only mode).
    """
    global _license_info, _device_id, _device_fingerprint

    # Re-read from env (CLI flow sets it after interactive prompt)
    global LICENSE_KEY
    LICENSE_KEY = os.getenv("LICENSE_KEY", LICENSE_KEY or "").strip()

    if not LICENSE_KEY:
        log.error("LICENSE_KEY is required. Run 'python -m unified.main' for interactive setup.")
        raise SystemExit("LICENSE_KEY is required to start the proxy.")

    _device_fingerprint = _generate_fingerprint()
    _os_name = f"{platform.system()} {platform.release()}"
    _pc_name = platform.node() or "unknown"
    _machine_id = _get_machine_id()

    log.info("Activating license %s... (device: %s, os: %s, fingerprint: %s...)",
             LICENSE_KEY[:10] + "...", _pc_name, _os_name, _device_fingerprint[:12])

    result = await _api_post("/api/auth/activate", {
        "license_key": LICENSE_KEY,
        "device_fingerprint": _device_fingerprint,
        "device_name": DEVICE_NAME,
        "os": _os_name,
        "pc_name": _pc_name,
        "machine_id": _machine_id,
    })

    if result.get("error"):
        log.error("License activation failed: %s", result["error"])
        return False

    if not result.get("ok"):
        log.error("License activation rejected: %s", result)
        return False

    _license_info = result.get("license", {})
    _device_id = result.get("device_id")
    log.info("License activated: %s (tier=%s, max_accounts=%d, device_id=%d, new=%s)",
             _license_info.get("id"), _license_info.get("tier"),
             _license_info.get("max_accounts", 0), _device_id,
             result.get("is_new", False))

    return True


def is_licensed() -> bool:
    """Check if license is active."""
    return _license_info is not None and _device_id is not None


def get_license_info() -> dict:
    """Return license info dict."""
    return _license_info or {}


# ─── Sync: Pull ─────────────────────────────────────────────────────────────

async def pull_sync() -> dict:
    """Pull accounts, settings, filters, watchwords, proxies from central DB.

    Returns the full sync payload or {error: ...}.
    """
    global _watchwords, _watchword_cache_ts

    if not is_licensed():
        return {"error": "Not licensed"}

    result = await _api_get("/api/sync/pull", {
        "license_key": LICENSE_KEY,
        "device_fingerprint": _device_fingerprint,
    })

    if result.get("error"):
        log.warning("Sync pull failed: %s", result["error"])
        return result

    # Update watchword + global filter cache
    _watchwords = result.get("watchwords", [])
    _watchword_cache_ts = time.monotonic()
    global _global_filters
    _global_filters = result.get("global_filters", [])

    log.info("Sync pull: %d accounts, %d settings, %d filters, %d watchwords, %d proxies",
             len(result.get("accounts", [])),
             len(result.get("settings", {})),
             len(result.get("filters", [])),
             len(_watchwords),
             len(result.get("proxies", [])))

    # Write pulled data to local SQLite
    await _write_to_local_db(result)

    return result


async def pull_and_merge() -> dict:
    """Pull from D1 and MERGE with local — never overwrite existing local data.

    Strategy:
    - Account exists in D1 but NOT local → ADD to local (new from other device)
    - Account exists in BOTH → keep local version (local is master)
    - Account exists in local but NOT D1 → keep local (will be pushed later)
    - Settings/filters/watchwords → always pull from D1

    Returns {new_accounts: N, updated_accounts: N}.
    """
    global _watchwords, _watchword_cache_ts, _global_filters

    if not is_licensed():
        return {"error": "Not licensed"}

    result = await _api_get("/api/sync/pull", {
        "license_key": LICENSE_KEY,
        "device_fingerprint": _device_fingerprint,
    })

    if result.get("error"):
        log.warning("Sync pull failed: %s", result["error"])
        return result

    # Update watchword + global filter cache
    _watchwords = result.get("watchwords", [])
    _watchword_cache_ts = time.monotonic()
    _global_filters = result.get("global_filters", [])

    from . import database as db

    # Merge accounts — only ADD new ones from D1
    new_accounts = 0
    d1_accounts = result.get("accounts", [])
    for acc in d1_accounts:
        email = acc.get("email", "")
        if not email:
            continue
        existing = await db.get_account_by_email(email)
        if not existing:
            # New account from D1 (added on another device) → add to local
            account_id = await db.create_account(email, acc.get("password", ""))
            fields = {}
            for key in [
                "status",
                "kiro_status", "kiro_access_token", "kiro_refresh_token", "kiro_profile_arn",
                "kiro_credits", "kiro_credits_total", "kiro_credits_used",
                "kiro_error", "kiro_error_count", "kiro_expires_at",
                "cb_status", "cb_api_key", "cb_credits", "cb_error", "cb_error_count", "cb_expires_at",
                "ws_status", "ws_api_key", "ws_credits", "ws_error", "ws_error_count",
                "gl_status", "gl_refresh_token", "gl_user_id", "gl_gummie_id", "gl_id_token",
                "gl_credits", "gl_error", "gl_error_count",
            ]:
                if key in acc and acc[key] is not None:
                    fields[key] = acc[key]
            if fields:
                # Direct DB update to skip auto-push (we'll push everything after)
                _db = await db.get_db()
                sets = [f"{k} = ?" for k in fields]
                vals = list(fields.values()) + [account_id]
                await _db.execute(
                    f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", vals
                )
                await _db.commit()
            new_accounts += 1
        # If exists locally → skip (local is master)

    # Pull settings, filters (always from D1)
    settings = result.get("settings", {})
    if isinstance(settings, dict):
        for key, value in settings.items():
            await db.set_setting(key, str(value))

    filters = result.get("filters", [])
    if filters:
        local_filters = await db.get_filters()
        for lf in local_filters:
            await db.delete_filter(lf["id"])
        for f in filters:
            await db.create_filter(
                find_text=f.get("find_text", ""),
                replace_text=f.get("replace_text", ""),
                is_regex=bool(f.get("is_regex", 0)),
                description=f.get("description", ""),
            )
        from .message_filter import invalidate_cache
        invalidate_cache()

    if new_accounts:
        log.info("Merged %d new accounts from D1", new_accounts)

    return {"ok": True, "new_accounts": new_accounts, "updated_accounts": 0}


async def pull_new_accounts_only() -> dict:
    """Pull from D1 but ONLY add accounts that don't exist locally.

    NEVER update or overwrite existing local accounts.
    Local is always master for accounts it already has.
    Also pulls settings, filters, watchwords.
    """
    global _watchwords, _watchword_cache_ts, _global_filters

    if not is_licensed():
        return {"error": "Not licensed"}

    result = await _api_get("/api/sync/pull", {
        "license_key": LICENSE_KEY,
        "device_fingerprint": _device_fingerprint,
    })

    if result.get("error"):
        return result

    _watchwords = result.get("watchwords", [])
    _watchword_cache_ts = time.monotonic()
    _global_filters = result.get("global_filters", [])

    from . import database as db

    new_accounts = 0
    d1_accounts = result.get("accounts", [])

    _SYNC_FIELDS = [
        "password", "status",
        "kiro_status", "kiro_access_token", "kiro_refresh_token", "kiro_profile_arn",
        "kiro_credits", "kiro_credits_total", "kiro_credits_used",
        "kiro_error", "kiro_error_count", "kiro_expires_at",
        "cb_status", "cb_api_key", "cb_credits", "cb_error", "cb_error_count", "cb_expires_at",
        "ws_status", "ws_api_key", "ws_credits", "ws_error", "ws_error_count",
        "gl_status", "gl_refresh_token", "gl_user_id", "gl_gummie_id", "gl_id_token",
        "gl_credits", "gl_error", "gl_error_count",
    ]

    for acc in d1_accounts:
        email = acc.get("email", "")
        if not email or acc.get("status") == "deleted":
            continue
        existing = await db.get_account_by_email(email)
        if not existing:
            # New account from other device — add to local
            account_id = await db.create_account(email, acc.get("password", ""))
            fields = {k: acc[k] for k in _SYNC_FIELDS if k in acc and acc[k] is not None}
            if fields:
                await db.update_account(account_id, **fields)
            new_accounts += 1
        # If exists locally → SKIP. Local is master.

    # Settings/filters always from D1
    settings = result.get("settings", {})
    if isinstance(settings, dict):
        for key, value in settings.items():
            await db.set_setting(key, str(value))

    filters = result.get("filters", [])
    if filters:
        local_filters = await db.get_filters()
        for lf in local_filters:
            await db.delete_filter(lf["id"])
        for f in filters:
            await db.create_filter(
                find_text=f.get("find_text", ""),
                replace_text=f.get("replace_text", ""),
                is_regex=bool(f.get("is_regex", 0)),
                description=f.get("description", ""),
            )
        from .message_filter import invalidate_cache
        invalidate_cache()

    if new_accounts:
        log.info("Added %d new accounts from D1 (other devices)", new_accounts)

    return {"ok": True, "new_accounts": new_accounts}


async def pull_settings_only() -> dict:
    """Pull only settings, filters, watchwords from D1 — NOT accounts.

    Local SQLite is the master for accounts. D1 only provides:
    - Settings (admin password, captcha key, etc.)
    - Filter rules
    - Watchwords
    - Proxies
    """
    global _watchwords, _watchword_cache_ts, _global_filters

    if not is_licensed():
        return {"error": "Not licensed"}

    result = await _api_get("/api/sync/pull", {
        "license_key": LICENSE_KEY,
        "device_fingerprint": _device_fingerprint,
    })

    if result.get("error"):
        return result

    # Update watchword + global filter cache
    _watchwords = result.get("watchwords", [])
    _watchword_cache_ts = time.monotonic()
    _global_filters = result.get("global_filters", [])

    # Write ONLY settings, filters, proxies — skip accounts
    from . import database as db

    settings = result.get("settings", {})
    if isinstance(settings, dict):
        for key, value in settings.items():
            await db.set_setting(key, str(value))

    filters = result.get("filters", [])
    if filters:
        local_filters = await db.get_filters()
        for lf in local_filters:
            await db.delete_filter(lf["id"])
        for f in filters:
            await db.create_filter(
                find_text=f.get("find_text", ""),
                replace_text=f.get("replace_text", ""),
                is_regex=bool(f.get("is_regex", 0)),
                description=f.get("description", ""),
            )
        from .message_filter import invalidate_cache
        invalidate_cache()

    return {"ok": True}


async def _write_to_local_db(data: dict) -> None:
    """Write pulled D1 data to local SQLite database.

    D1 is the source of truth. On pull, D1 data overwrites local.
    Every local change is pushed to D1 immediately (via update_account hook),
    so D1 should always have the latest data.
    """
    from . import database as db

    _SYNC_FIELDS = [
        "password", "status",
        "kiro_status", "kiro_access_token", "kiro_refresh_token", "kiro_profile_arn",
        "kiro_credits", "kiro_credits_total", "kiro_credits_used",
        "kiro_error", "kiro_error_count", "kiro_expires_at",
        "cb_status", "cb_api_key", "cb_credits", "cb_error", "cb_error_count", "cb_expires_at",
        "ws_status", "ws_api_key", "ws_credits", "ws_error", "ws_error_count",
        "gl_status", "gl_refresh_token", "gl_user_id", "gl_gummie_id", "gl_id_token",
        "gl_credits", "gl_error", "gl_error_count",
    ]

    # Upsert accounts — D1 overwrites local
    accounts = data.get("accounts", [])
    for acc in accounts:
        email = acc.get("email", "")
        if not email:
            continue
        existing = await db.get_account_by_email(email)
        if existing:
            fields = {}
            for key in _SYNC_FIELDS:
                if key in acc and acc[key] is not None:
                    fields[key] = acc[key]
            if fields:
                # Direct DB update — skip the auto-push hook to avoid push loop
                _db = await db.get_db()
                sets = [f"{k} = ?" for k in fields]
                vals = list(fields.values()) + [existing["id"]]
                await _db.execute(
                    f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", vals
                )
                await _db.commit()
        else:
            # Create new account from D1 — direct insert, skip push hook
            _db = await db.get_db()
            cur = await _db.execute(
                "INSERT INTO accounts (email, password) VALUES (?, ?)",
                (email, acc.get("password", "")),
            )
            await _db.commit()
            account_id = cur.lastrowid
            fields = {}
            for key in _SYNC_FIELDS:
                if key in acc and acc[key] is not None:
                    fields[key] = acc[key]
            if fields:
                sets = [f"{k} = ?" for k in fields]
                vals = list(fields.values()) + [account_id]
                await _db.execute(
                    f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", vals
                )
                await _db.commit()

    # Upsert settings
    settings = data.get("settings", {})
    if isinstance(settings, dict):
        for key, value in settings.items():
            await db.set_setting(key, str(value))

    # Sync filters (replace local with D1 filters)
    filters = data.get("filters", [])
    if filters:
        # Clear local filters and re-insert from D1
        local_filters = await db.get_filters()
        for lf in local_filters:
            await db.delete_filter(lf["id"])
        for f in filters:
            await db.create_filter(
                find_text=f.get("find_text", ""),
                replace_text=f.get("replace_text", ""),
                is_regex=bool(f.get("is_regex", 0)),
                description=f.get("description", ""),
            )
        from .message_filter import invalidate_cache
        invalidate_cache()

    if accounts:
        log.info("Synced %d accounts to local DB", len(accounts))


# ─── Sync: Push ──────────────────────────────────────────────────────────────

async def push_sync(
    accounts: list[dict] | None = None,
    settings: dict | None = None,
    proxies: list[dict] | None = None,
) -> dict:
    """Push local changes to central DB.

    Chunks accounts into batches of 50 to avoid D1 rate limits.
    Flushes accumulated usage_logs and alerts buffers.
    """
    global _usage_buffer, _alert_buffer

    if not is_licensed():
        return {"error": "Not licensed"}

    logs = _usage_buffer[:]
    alerts = _alert_buffer[:]
    _usage_buffer.clear()
    _alert_buffer.clear()

    total_upserted = 0
    total_deleted = 0

    # Push accounts in chunks of 30 to avoid D1 "Too many API requests" error
    CHUNK_SIZE = 30
    if accounts:
        for i in range(0, len(accounts), CHUNK_SIZE):
            chunk = accounts[i:i + CHUNK_SIZE]
            payload: dict[str, Any] = {
                "license_key": LICENSE_KEY,
                "device_fingerprint": _device_fingerprint,
                "accounts": chunk,
            }
            # Attach logs/alerts/settings only to first chunk
            if i == 0:
                if settings:
                    payload["settings"] = settings
                if logs:
                    payload["usage_logs"] = logs
                if alerts:
                    payload["alerts"] = alerts
                if proxies:
                    payload["proxies"] = proxies

            result = await _api_post("/api/sync/push", payload, timeout=60)
            if result.get("error"):
                if i == 0:
                    _usage_buffer.extend(logs)
                    _alert_buffer.extend(alerts)
                log.warning("Sync push chunk %d-%d failed: %s", i, i + len(chunk), result["error"])
                # Continue with next chunk instead of failing entirely
                continue

            total_upserted += result.get("accounts_upserted", 0)
            total_deleted += result.get("accounts_deleted", 0)

            # Delay between chunks
            if i + CHUNK_SIZE < len(accounts):
                import asyncio as _aio
                await _aio.sleep(1)
    else:
        # No accounts — just push logs/alerts
        payload = {
            "license_key": LICENSE_KEY,
            "device_fingerprint": _device_fingerprint,
        }
        if settings:
            payload["settings"] = settings
        if logs:
            payload["usage_logs"] = logs
        if alerts:
            payload["alerts"] = alerts
        if proxies:
            payload["proxies"] = proxies

        result = await _api_post("/api/sync/push", payload, timeout=60)
        if result.get("error"):
            _usage_buffer.extend(logs)
            _alert_buffer.extend(alerts)
            log.warning("Sync push failed: %s", result["error"])
            return result

    result = {"ok": True, "accounts_upserted": total_upserted, "accounts_deleted": total_deleted}
    log.info("Sync push: %d accounts (%d chunks), %d logs, %d alerts",
             total_upserted, (len(accounts) + CHUNK_SIZE - 1) // CHUNK_SIZE if accounts else 0,
             len(logs), len(alerts))

    return result


# ─── Usage Log Buffer ────────────────────────────────────────────────────────

async def push_account_now(account: dict) -> bool:
    """Push a single account to D1. Returns True if success."""
    if not is_licensed():
        return False
    try:
        result = await _api_post("/api/sync/push", {
            "license_key": LICENSE_KEY,
            "device_fingerprint": _device_fingerprint,
            "accounts": [account],
        }, timeout=10)
        return not result.get("error")
    except Exception as e:
        log.warning("D1 push failed: %s", e)
        return False


async def d1_sync_account(account_id: int) -> bool:
    """Read account from local DB and push to D1. Call after any local update."""
    from . import database as db
    account = await db.get_account(account_id)
    if not account:
        return False
    return await push_account_now(account)


async def d1_delete_account(email: str) -> bool:
    """Delete account from D1 by email."""
    if not is_licensed():
        return False
    try:
        result = await _api_post("/api/sync/push", {
            "license_key": LICENSE_KEY,
            "device_fingerprint": _device_fingerprint,
            "accounts": [{"email": email, "status": "deleted"}],
        }, timeout=10)
        return not result.get("error")
    except Exception as e:
        log.warning("D1 delete failed for %s: %s", email, e)
        return False


async def full_pull_replace_local() -> dict:
    """FULL PULL from D1 → completely replace local accounts.

    D1 = source of truth. Local DB is wiped and rebuilt from D1 data.
    Also pulls settings, filters, watchwords.
    """
    global _watchwords, _watchword_cache_ts, _global_filters

    if not is_licensed():
        return {"error": "Not licensed"}

    result = await _api_get("/api/sync/pull", {
        "license_key": LICENSE_KEY,
        "device_fingerprint": _device_fingerprint,
    })

    if result.get("error"):
        log.warning("D1 full pull failed: %s", result["error"])
        return result

    # Update caches
    _watchwords = result.get("watchwords", [])
    _watchword_cache_ts = time.monotonic()
    _global_filters = result.get("global_filters", [])

    from . import database as db

    d1_accounts = result.get("accounts", [])
    d1_emails = {acc.get("email", "") for acc in d1_accounts if acc.get("email")}

    # Get local accounts
    local_accounts = await db.get_accounts()
    local_by_email = {acc["email"]: acc for acc in local_accounts}

    added = 0
    updated = 0
    deleted = 0

    _SYNC_FIELDS = [
        "password", "status",
        "kiro_status", "kiro_access_token", "kiro_refresh_token", "kiro_profile_arn",
        "kiro_credits", "kiro_credits_total", "kiro_credits_used",
        "kiro_error", "kiro_error_count", "kiro_expires_at",
        "cb_status", "cb_api_key", "cb_credits", "cb_error", "cb_error_count", "cb_expires_at",
        "ws_status", "ws_api_key", "ws_credits", "ws_error", "ws_error_count",
        "gl_status", "gl_refresh_token", "gl_user_id", "gl_gummie_id", "gl_id_token",
        "gl_credits", "gl_error", "gl_error_count",
    ]

    # Upsert D1 accounts to local
    for acc in d1_accounts:
        email = acc.get("email", "")
        if not email:
            continue

        # Skip deleted accounts from D1
        if acc.get("status") == "deleted":
            if email in local_by_email:
                await db.delete_account(local_by_email[email]["id"])
                deleted += 1
            continue

        existing = local_by_email.get(email)
        if existing:
            # Update local with D1 data
            fields = {}
            for key in _SYNC_FIELDS:
                if key in acc and acc[key] is not None:
                    fields[key] = acc[key]
            if fields:
                await db.update_account(existing["id"], **fields)
                updated += 1
        else:
            # New account from D1
            account_id = await db.create_account(email, acc.get("password", ""))
            fields = {}
            for key in _SYNC_FIELDS:
                if key in acc and acc[key] is not None:
                    fields[key] = acc[key]
            if fields:
                await db.update_account(account_id, **fields)
            added += 1

    # Delete local accounts that don't exist in D1
    for email, local_acc in local_by_email.items():
        if email not in d1_emails:
            await db.delete_account(local_acc["id"])
            deleted += 1

    # Sync settings
    settings = result.get("settings", {})
    if isinstance(settings, dict):
        for key, value in settings.items():
            await db.set_setting(key, str(value))

    # Sync filters
    filters = result.get("filters", [])
    if filters:
        local_filters = await db.get_filters()
        for lf in local_filters:
            await db.delete_filter(lf["id"])
        for f in filters:
            await db.create_filter(
                find_text=f.get("find_text", ""),
                replace_text=f.get("replace_text", ""),
                is_regex=bool(f.get("is_regex", 0)),
                description=f.get("description", ""),
            )
        from .message_filter import invalidate_cache
        invalidate_cache()

    log.info("D1 full pull: +%d new, ~%d updated, -%d deleted, %d total",
             added, updated, deleted, len(d1_accounts))

    return {"ok": True, "added": added, "updated": updated, "deleted": deleted, "total": len(d1_accounts)}


def buffer_usage_log(
    model: str,
    tier: str,
    status_code: int = 200,
    latency_ms: int = 0,
    proxy_url: str = "",
    error_message: str = "",
    account_email: str = "",
    tokens: int = 0,
) -> None:
    """Buffer a usage log entry for next sync push."""
    if not is_licensed():
        return

    _usage_buffer.append({
        "model": model,
        "tier": tier,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "proxy_url": proxy_url,
        "error_message": error_message[:500],
        "account_email": account_email,
        "tokens": tokens,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


# ─── Watchword Scanner ──────────────────────────────────────────────────────

def _extract_text(msg: dict) -> str:
    """Extract text content from a message dict (handles string + list content)."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block.get("text"), str):
                    parts.append(block["text"])
        return "\n".join(parts)
    return ""


async def scan_watchwords(
    body: dict,
    model: str = "",
    account_email: str = "",
    proxy_url: str = "",
) -> list[dict]:
    """Scan message content for watchword matches. Non-blocking.

    Returns list of alert dicts (also buffered for next sync push).
    """
    if not is_licensed() or not _watchwords:
        return []

    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        return []

    alerts = []
    for msg in messages:
        text = _extract_text(msg)
        if not text:
            continue

        role = msg.get("role", "")
        text_lower = text.lower()

        for ww in _watchwords:
            if not ww.get("enabled", True):
                continue

            keyword = ww["keyword"]
            matched = False

            if ww.get("is_regex"):
                try:
                    if re.search(keyword, text, re.IGNORECASE):
                        matched = True
                except re.error:
                    continue
            else:
                if keyword.lower() in text_lower:
                    matched = True

            if not matched:
                continue

            # Rate suppression
            dedup_key = (LICENSE_KEY, keyword)
            last_alert = _alert_dedup.get(dedup_key, 0)
            if time.time() - last_alert < _ALERT_COOLDOWN:
                continue
            _alert_dedup[dedup_key] = time.time()

            # Extract snippet (100 chars before + after match)
            idx = text_lower.find(keyword.lower()) if not ww.get("is_regex") else 0
            if ww.get("is_regex"):
                m = re.search(keyword, text, re.IGNORECASE)
                idx = m.start() if m else 0
            start = max(0, idx - 100)
            end = min(len(text), idx + len(keyword) + 100)
            snippet = text[start:end]

            alert = {
                "watchword_id": ww["id"],
                "keyword_matched": keyword,
                "severity": ww.get("severity", "warning"),
                "message_snippet": snippet[:300],
                "message_role": role,
                "model": model,
                "account_email": account_email,
                "proxy_url": proxy_url,
            }
            alerts.append(alert)
            _alert_buffer.append(alert)

            log.warning("Watchword alert: keyword=%s severity=%s role=%s model=%s",
                        keyword, ww.get("severity"), role, model)

    return alerts


# ─── Periodic Sync Loop ─────────────────────────────────────────────────────

async def _sync_loop() -> None:
    """Heartbeat: every 2 minutes, pull ALL from D1 → replace local cache.

    D1 = pusat. Local = cache.
    Any changes from other devices appear within 2 minutes.
    """
    while True:
        try:
            await asyncio.sleep(SYNC_INTERVAL)

            if not is_licensed():
                continue

            # Heartbeat
            await _api_post("/api/auth/heartbeat", {
                "license_key": LICENSE_KEY,
                "device_fingerprint": _device_fingerprint,
            })

            # Push buffered usage logs only (accounts pushed instantly per-change)
            await push_sync()

            # Pull ALL from D1 → replace local cache
            await full_pull_replace_local()

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("Sync loop error: %s", e)


def start_sync_loop() -> None:
    """Start the background sync loop. Call after activate()."""
    global _sync_task
    if _sync_task is not None:
        return
    _sync_task = asyncio.create_task(_sync_loop())
    log.info("Sync loop started (interval=%ds)", SYNC_INTERVAL)


async def stop_sync_loop() -> None:
    """Stop the background sync loop. Call on shutdown."""
    global _sync_task
    if _sync_task is not None:
        _sync_task.cancel()
        try:
            await _sync_task
        except asyncio.CancelledError:
            pass
        _sync_task = None

    # Final push of any remaining buffered data
    if is_licensed() and (_usage_buffer or _alert_buffer):
        log.info("Final sync push: %d logs, %d alerts", len(_usage_buffer), len(_alert_buffer))
        await push_sync()

    log.info("Sync loop stopped")
