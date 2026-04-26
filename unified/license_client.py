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
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "300"))  # 5 minutes

# ─── State ───────────────────────────────────────────────────────────────────

_license_info: Optional[dict] = None
_device_id: Optional[int] = None
_device_fingerprint: str = ""
_watchwords: list[dict] = []
_watchword_cache_ts: float = 0

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

    # Update watchword cache
    _watchwords = result.get("watchwords", [])
    _watchword_cache_ts = time.monotonic()

    log.info("Sync pull: %d accounts, %d settings, %d filters, %d watchwords, %d proxies",
             len(result.get("accounts", [])),
             len(result.get("settings", {})),
             len(result.get("filters", [])),
             len(_watchwords),
             len(result.get("proxies", [])))

    return result


# ─── Sync: Push ──────────────────────────────────────────────────────────────

async def push_sync(
    accounts: list[dict] | None = None,
    settings: dict | None = None,
    proxies: list[dict] | None = None,
) -> dict:
    """Push local changes to central DB.

    Flushes accumulated usage_logs and alerts buffers.
    Optionally pushes account updates, settings, and proxies.
    """
    global _usage_buffer, _alert_buffer

    if not is_licensed():
        return {"error": "Not licensed"}

    # Grab and clear buffers atomically
    logs = _usage_buffer[:]
    alerts = _alert_buffer[:]
    _usage_buffer.clear()
    _alert_buffer.clear()

    payload: dict[str, Any] = {
        "license_key": LICENSE_KEY,
        "device_fingerprint": _device_fingerprint,
    }

    if accounts:
        payload["accounts"] = accounts
    if settings:
        payload["settings"] = settings
    if logs:
        payload["usage_logs"] = logs
    if alerts:
        payload["alerts"] = alerts
    if proxies:
        payload["proxies"] = proxies

    result = await _api_post("/api/sync/push", payload, timeout=30)

    if result.get("error"):
        # Put logs/alerts back on failure so they're retried next sync
        _usage_buffer.extend(logs)
        _alert_buffer.extend(alerts)
        log.warning("Sync push failed: %s (buffered %d logs, %d alerts for retry)",
                     result["error"], len(logs), len(alerts))
        return result

    log.info("Sync push: %d logs, %d alerts, %d accounts, %d proxies",
             result.get("logs_inserted", 0), result.get("alerts_inserted", 0),
             result.get("accounts_upserted", 0), result.get("proxies_upserted", 0))

    return result


# ─── Usage Log Buffer ────────────────────────────────────────────────────────

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
    """Background loop: pull + push every SYNC_INTERVAL seconds."""
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

            # Push buffered data
            await push_sync()

            # Pull latest watchwords (accounts/settings pulled on demand by database.py)
            result = await _api_get("/api/sync/pull", {
                "license_key": LICENSE_KEY,
                "device_fingerprint": _device_fingerprint,
            })
            if result.get("ok"):
                global _watchwords, _watchword_cache_ts
                _watchwords = result.get("watchwords", [])
                _watchword_cache_ts = time.monotonic()

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
