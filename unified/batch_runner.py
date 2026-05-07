"""Batch login runner — subprocess-based login with SSE progress broadcasting."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import httpx

from . import database as db
from .config import (
    AUTH_SCRIPT,
    WAVESPEED_SCRIPT,
    GUMLOOP_SCRIPT,
    CHATBAI_SCRIPT,
    PYTHON_BIN,
    KIRO_UPSTREAM,
    KIRO_ADMIN_PASSWORD,
    WS_DEFAULT_CREDITS,
    CBAI_DEFAULT_CREDITS,
)
from .gumloop.auth import GumloopAuth

log = logging.getLogger("unified.batch_runner")


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class AccountJob:
    id: str
    email: str
    password: str
    providers: list[str]
    account_id: Optional[int] = None
    status: JobStatus = JobStatus.QUEUED
    logs: list[dict] = field(default_factory=list)
    result: Optional[dict] = None
    started_at: float = 0
    finished_at: float = 0
    mcp_urls: list[str] = field(default_factory=list)


class BatchState:
    """Global batch state with SSE broadcasting."""

    def __init__(self) -> None:
        self.jobs: list[AccountJob] = []
        self.running: bool = False
        self.cancelled: bool = False
        self.headless: bool = True
        self.concurrency: int = 1
        self._sse_queues: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()
        self._active_procs: list[asyncio.subprocess.Process] = []  # track running subprocesses

    def broadcast(self, event: dict) -> None:
        dead: list[asyncio.Queue] = []
        for q in self._sse_queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._sse_queues.remove(q)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._sse_queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._sse_queues:
            self._sse_queues.remove(q)


# Singleton
batch_state = BatchState()


# ---------------------------------------------------------------------------
# Global duplicate check (D1)
# ---------------------------------------------------------------------------

async def _check_global_duplicates(emails: list[str], providers: list[str]) -> dict[str, list[str]]:
    """Check D1 for emails already used globally across all licenses.

    Returns: {email: [provider, ...]} for emails that are duplicates.
    """
    from . import license_client

    if not emails or not license_client.is_licensed():
        return {}

    try:
        result = await license_client.check_emails_global(emails, providers)
        return result.get("duplicates", {})
    except Exception as e:
        log.warning("Global duplicate check failed (continuing without): %s", e)
        return {}


# ---------------------------------------------------------------------------
# Batch orchestrator
# ---------------------------------------------------------------------------

async def start_batch(accounts: list[tuple[str, str]], providers: list[str],
                      headless: bool = True, concurrency: int = 1,
                      mcp_urls: list[str] | None = None) -> int:
    """Start a batch login. Returns number of jobs queued.

    accounts: list of (email, password) tuples
    providers: list of provider names ("kiro", "codebuddy")
    concurrency: number of parallel browser instances (1 = sequential)
    mcp_urls: MCP server URLs to attach after Gumloop login
    """
    if batch_state.running:
        raise RuntimeError("Batch already running")

    batch_state.jobs.clear()
    batch_state.cancelled = False
    batch_state.headless = headless
    batch_state.concurrency = max(1, concurrency)
    batch_state._active_procs.clear()

    # ── Global duplicate check via D1 (cross-license) ──
    global_duplicates = await _check_global_duplicates(
        [email for email, _ in accounts], providers
    )

    skipped = 0
    for email, password in accounts:
        existing = await db.get_account_by_email(email)

        # Filter out providers that are already OK for this account (local check)
        needed_providers = list(providers)
        if existing:
            if "kiro" in needed_providers and existing.get("kiro_status") == "ok":
                needed_providers.remove("kiro")
            if "codebuddy" in needed_providers and existing.get("cb_status") == "ok":
                needed_providers.remove("codebuddy")
            if "chatbai" in needed_providers and existing.get("cbai_status") == "ok":
                needed_providers.remove("chatbai")
            if "wavespeed" in needed_providers and existing.get("ws_status") == "ok":
                needed_providers.remove("wavespeed")
            if "gumloop" in needed_providers and existing.get("gl_status") == "ok":
                needed_providers.remove("gumloop")
            if "skillboss" in needed_providers and existing.get("skboss_status") == "ok":
                needed_providers.remove("skillboss")

        # Filter out providers already used globally (D1 cross-license check)
        if email in global_duplicates:
            for prov in global_duplicates[email]:
                mapped = "codebuddy" if prov == "codebuddy" else prov
                if mapped in needed_providers:
                    needed_providers.remove(mapped)

        if not needed_providers:
            skipped += 1
            continue  # All requested providers already OK — full skip

        account_id = existing["id"] if existing else None

        job = AccountJob(
            id=str(uuid.uuid4())[:8],
            email=email,
            password=password,
            providers=needed_providers,  # Only providers that need login
            account_id=account_id,
            mcp_urls=mcp_urls or [],
        )
        batch_state.jobs.append(job)

    if skipped:
        batch_state.broadcast({"type": "batch_skipped", "count": skipped})

    asyncio.create_task(_run_batch())
    return len(batch_state.jobs)


def cancel_batch() -> None:
    """Cancel batch: set flag + force-kill all running subprocesses."""
    batch_state.cancelled = True
    for proc in batch_state._active_procs:
        try:
            proc.kill()
        except Exception:
            pass
    batch_state._active_procs.clear()


async def _run_single_job(job: AccountJob, index: int, proxy_info: dict | None) -> None:
    """Run a single login job. Called by _run_batch workers."""
    proxy_url_used = proxy_info["url"] if proxy_info else ""

    job.status = JobStatus.RUNNING
    job.started_at = time.time()
    batch_state.broadcast({
        "type": "job_start",
        "job_id": job.id,
        "email": job.email,
        "providers": job.providers,
        "index": index,
        "total": len(batch_state.jobs),
        "proxy_used": proxy_url_used,
    })

    try:
        result: dict = {}

        # Override proxy for this specific job
        if proxy_url_used:
            job._assigned_proxy = proxy_url_used  # type: ignore

        kiro_cb_providers = [p for p in job.providers if p in ("kiro", "codebuddy")]
        if kiro_cb_providers:
            result, _ = await _run_login(job, proxy_override=proxy_url_used or None)

        if "wavespeed" in job.providers:
            ws_result, _ = await _run_wavespeed_login(job, proxy_override=proxy_url_used or None)
            result.update(ws_result)

        if "gumloop" in job.providers:
            gl_result, _ = await _run_gumloop_login(job, proxy_override=proxy_url_used or None)
            result.update(gl_result)

        if "chatbai" in job.providers:
            # ChatBAI: use smart rotate per-job (get fresh proxy each time)
            cbai_proxy = proxy_url_used or None
            smart = (await db.get_setting("batch_smart_rotate", "false")).lower() in ("true", "1")
            if smart:
                fresh_proxy = await db.get_proxy_for_batch()
                if fresh_proxy:
                    cbai_proxy = fresh_proxy["url"]
            cbai_result, _ = await _run_chatbai_login(job, proxy_override=cbai_proxy)
            result.update(cbai_result)

        if "skillboss" in job.providers:
            skboss_result = await _run_skillboss_login(job)
            result.update(skboss_result)

        job.result = result
        imported: list[str] = []

        kiro_ok = "kiro" in job.providers and result.get("kiro", {}).get("success")
        cb_ok = "codebuddy" in job.providers and result.get("codebuddy", {}).get("success")
        ws_ok = "wavespeed" in job.providers and result.get("wavespeed", {}).get("success")
        gl_ok = "gumloop" in job.providers and result.get("gumloop", {}).get("success")
        cbai_ok = "chatbai" in job.providers and result.get("chatbai", {}).get("success")
        skboss_ok = "skillboss" in job.providers and result.get("skillboss", {}).get("success")
        any_login_success = kiro_ok or cb_ok or ws_ok or gl_ok or cbai_ok or skboss_ok

        if any_login_success and not job.account_id:
            job.account_id = await db.create_account(job.email, job.password)

        if kiro_ok:
            ok = await _import_kiro(job)
            if ok:
                imported.append("kiro")
            batch_state.broadcast({
                "type": "provider_done", "job_id": job.id, "email": job.email,
                "provider": "kiro", "success": ok,
            })

        if cb_ok:
            ok = await _import_codebuddy(job)
            if ok:
                imported.append("codebuddy")
            batch_state.broadcast({
                "type": "provider_done", "job_id": job.id, "email": job.email,
                "provider": "codebuddy", "success": ok,
            })

        if ws_ok:
            ok = await _import_wavespeed(job)
            if ok:
                imported.append("wavespeed")
            batch_state.broadcast({
                "type": "provider_done", "job_id": job.id, "email": job.email,
                "provider": "wavespeed", "success": ok,
            })

        if gl_ok:
            ok = await _import_gumloop(job)
            if ok:
                imported.append("gumloop")
                # Attach MCP servers if provided
                if job.mcp_urls:
                    try:
                        proxy_info = await db.get_proxy_for_api_call()
                        proxy_url = proxy_info["url"] if proxy_info else None
                        await _attach_mcp_servers(job, proxy_url=proxy_url)
                    except Exception as mcp_err:
                        log.warning("MCP attach failed for %s: %s", job.email, mcp_err)
                        batch_state.broadcast({
                            "type": "job_log", "job_id": job.id, "email": job.email,
                            "log_type": "warn", "provider": "gumloop",
                            "step": "mcp", "message": f"MCP attach failed: {mcp_err}",
                        })
            batch_state.broadcast({
                "type": "provider_done", "job_id": job.id, "email": job.email,
                "provider": "gumloop", "success": ok,
            })

        if cbai_ok:
            ok = await _import_chatbai(job)
            if ok:
                imported.append("chatbai")
            batch_state.broadcast({
                "type": "provider_done", "job_id": job.id, "email": job.email,
                "provider": "chatbai", "success": ok,
            })

        if skboss_ok:
            api_key = result.get("skillboss", {}).get("api_key", "")
            if api_key and job.account_id:
                from .config import SKBOSS_DEFAULT_CREDITS
                await db.update_account(job.account_id, skboss_status="ok", skboss_api_key=api_key, skboss_credits=SKBOSS_DEFAULT_CREDITS, skboss_error="")
                imported.append("skillboss")
                ok = True
            else:
                if job.account_id:
                    await db.update_account(job.account_id, skboss_status="failed", skboss_error="No API key")
                ok = False
            batch_state.broadcast({
                "type": "provider_done", "job_id": job.id, "email": job.email,
                "provider": "skillboss", "success": ok,
            })

        any_success = bool(imported)
        job.status = JobStatus.SUCCESS if any_success else JobStatus.FAILED
        job.finished_at = time.time()

        if job.account_id:
            if "kiro" in job.providers:
                if result.get("kiro", {}).get("success"):
                    if "kiro" not in imported:
                        await db.update_account(job.account_id, kiro_status="failed",
                                                kiro_error="Import failed")
                else:
                    error = result.get("kiro", {}).get("error", "Login failed")
                    await db.update_account(job.account_id, kiro_status="failed",
                                            kiro_error=error)

            if "codebuddy" in job.providers:
                if result.get("codebuddy", {}).get("success"):
                    if "codebuddy" not in imported:
                        await db.update_account(job.account_id, cb_status="failed",
                                                cb_error="Import failed")
                else:
                    error = result.get("codebuddy", {}).get("error", "Login failed")
                    await db.update_account(job.account_id, cb_status="failed",
                                            cb_error=error)

            if "wavespeed" in job.providers:
                if result.get("wavespeed", {}).get("success"):
                    if "wavespeed" not in imported:
                        await db.update_account(job.account_id, ws_status="failed",
                                                ws_error="Import failed")
                else:
                    error = result.get("wavespeed", {}).get("error", "Login failed")
                    await db.update_account(job.account_id, ws_status="failed",
                                            ws_error=error)

            if "gumloop" in job.providers:
                if result.get("gumloop", {}).get("success"):
                    if "gumloop" not in imported:
                        await db.update_account(job.account_id, gl_status="failed",
                                                gl_error="Import failed")
                else:
                    error = result.get("gumloop", {}).get("error", "Login failed")
                    await db.update_account(job.account_id, gl_status="failed",
                                            gl_error=error)

            if not any_success:
                refreshed = await db.get_account(job.account_id)
                has_any_ok = refreshed and (
                    refreshed.get("kiro_status") == "ok"
                    or refreshed.get("cb_status") == "ok"
                    or refreshed.get("ws_status") == "ok"
                    or refreshed.get("gl_status") == "ok"
                )
                if not has_any_ok:
                    await db.update_account(job.account_id, status="failed")

        # Push final account state to D1 (D1 = source of truth)
        if job.account_id:
            try:
                from . import license_client
                await license_client.d1_sync_account(job.account_id)
            except Exception:
                pass

        batch_state.broadcast({
            "type": "job_done",
            "job_id": job.id,
            "email": job.email,
            "status": job.status.value,
            "imported": imported,
            "duration": round(job.finished_at - job.started_at, 1),
            "kiro": result.get("kiro", {}).get("success", False),
            "codebuddy": result.get("codebuddy", {}).get("success", False),
            "gumloop": result.get("gumloop", {}).get("success", False),
            "proxy_used": proxy_url_used,
        })

    except Exception as exc:
        job.status = JobStatus.FAILED
        job.finished_at = time.time()
        log.exception("Job %s failed: %s", job.email, exc)
        if job.account_id:
            await db.update_account(job.account_id, status="failed")
        batch_state.broadcast({
            "type": "job_error",
            "job_id": job.id,
            "email": job.email,
            "error": str(exc),
            "proxy_used": proxy_url_used,
        })


async def _run_batch() -> None:
    batch_state.running = True
    concurrency = batch_state.concurrency
    total = len(batch_state.jobs)
    batch_state.broadcast({"type": "batch_start", "total": total, "concurrency": concurrency})

    # Get proxy pool for workers
    proxy_pool = await db.get_batch_proxies_for_workers(concurrency)
    # Semaphore to limit concurrent workers
    sem = asyncio.Semaphore(concurrency)
    # Track which proxies are in use
    proxy_lock = asyncio.Lock()
    available_proxies = list(proxy_pool)

    # Track consecutive failures for auto-stop
    consecutive_fails = 0
    _MAX_CONSECUTIVE_FAILS = 3
    fail_lock = asyncio.Lock()

    async def worker(i: int, job: AccountJob) -> None:
        nonlocal consecutive_fails

        # Check before acquiring semaphore (queued jobs)
        if batch_state.cancelled:
            job.status = JobStatus.CANCELLED
            batch_state.broadcast({
                "type": "job_cancelled", "job_id": job.id,
                "email": job.email, "index": i,
            })
            return

        async with sem:
            # Check again after acquiring semaphore (might have been cancelled while waiting)
            if batch_state.cancelled:
                job.status = JobStatus.CANCELLED
                batch_state.broadcast({
                    "type": "job_cancelled", "job_id": job.id,
                    "email": job.email, "index": i,
                })
                return
            # Grab a proxy from the available pool
            proxy_info = None
            async with proxy_lock:
                if available_proxies:
                    proxy_info = available_proxies.pop(0)

            try:
                await _run_single_job(job, i, proxy_info)
            finally:
                # Return proxy to pool
                if proxy_info:
                    async with proxy_lock:
                        available_proxies.append(proxy_info)

            # Cooldown between accounts on same proxy to avoid rate limits
            await asyncio.sleep(2)

            # Track consecutive failures — auto-stop if same error keeps happening
            async with fail_lock:
                if job.status == JobStatus.FAILED:
                    consecutive_fails += 1
                    if consecutive_fails >= _MAX_CONSECUTIVE_FAILS:
                        batch_state.cancelled = True
                        reason = "Auto-stopped: 3 consecutive failures (likely missing dependencies or config issue)"
                        log.warning("[batch] %s", reason)
                        batch_state.broadcast({
                            "type": "batch_auto_stop",
                            "reason": reason,
                            "consecutive_fails": consecutive_fails,
                        })
                elif job.status == JobStatus.SUCCESS:
                    consecutive_fails = 0  # Reset on success

    # Launch all jobs — semaphore controls concurrency
    tasks = [asyncio.create_task(worker(i, job)) for i, job in enumerate(batch_state.jobs)]
    await asyncio.gather(*tasks, return_exceptions=True)

    batch_state.running = False
    summary = {
        "success": sum(1 for j in batch_state.jobs if j.status == JobStatus.SUCCESS),
        "failed": sum(1 for j in batch_state.jobs if j.status == JobStatus.FAILED),
        "cancelled": sum(1 for j in batch_state.jobs if j.status == JobStatus.CANCELLED),
    }
    batch_state.broadcast({"type": "batch_done", **summary})


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------

async def _run_login(job: AccountJob, proxy_override: str | None = None) -> tuple[dict, str]:
    """Run login.py as subprocess, parse JSON stdout lines.

    Returns (result_data, proxy_url_used).
    proxy_override: if set, use this proxy instead of fetching from pool.
    """
    env = {
        **os.environ,
        "BATCHER_ENABLE_CAMOUFOX": "true",
        "BATCHER_CAMOUFOX_HEADLESS": "true" if batch_state.headless else "false",
        "BATCHER_KIRO_AUTH_DEBUG": "true",
        "BATCHER_CODEBUDDY_AUTH_DEBUG": "true",
    }

    if set(job.providers) == {"kiro"}:
        env["BATCHER_CONCURRENT"] = "1"
        env["BATCHER_PRIORITY"] = "standard"
    elif set(job.providers) == {"codebuddy"}:
        env["BATCHER_CONCURRENT"] = "1"
        env["BATCHER_PRIORITY"] = "max"
    else:
        env["BATCHER_CONCURRENT"] = "2"
        env["BATCHER_PRIORITY"] = "standard"

    # Use assigned proxy (from concurrent worker) or fallback to pool rotation
    proxy_url_used = ""
    if proxy_override:
        env["BATCHER_PROXY_URL"] = proxy_override
        proxy_url_used = proxy_override
    else:
        proxy_info = await db.get_proxy_for_batch()
        if proxy_info:
            env["BATCHER_PROXY_URL"] = proxy_info["url"]
            proxy_url_used = proxy_info["url"]

    python_bin = str(PYTHON_BIN)
    auth_script = str(AUTH_SCRIPT)

    proc = await asyncio.create_subprocess_exec(
        python_bin,
        auth_script,
        "--email", job.email,
        "--password", job.password,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    batch_state._active_procs.append(proc)

    result_data: dict = {}

    async def read_stream(stream: asyncio.StreamReader, is_stderr: bool = False) -> None:
        nonlocal result_data
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            log_entry = {"ts": time.time(), "raw": text}

            try:
                parsed = json.loads(text)
                log_entry["parsed"] = parsed

                if parsed.get("type") == "result":
                    result_data = parsed

                batch_state.broadcast({
                    "type": "job_log",
                    "job_id": job.id,
                    "email": job.email,
                    "log_type": parsed.get("type", "info"),
                    "provider": parsed.get("provider", ""),
                    "step": parsed.get("step", ""),
                    "message": parsed.get("message", parsed.get("error", text)),
                    "proxy_used": proxy_url_used,
                })
            except json.JSONDecodeError:
                log_type = "stderr" if is_stderr else "stdout"
                batch_state.broadcast({
                    "type": "job_log",
                    "job_id": job.id,
                    "email": job.email,
                    "log_type": log_type,
                    "message": text,
                    "proxy_used": proxy_url_used,
                })

            job.logs.append(log_entry)

    # Timeout: kill subprocess if it takes too long (3 minutes per job)
    JOB_TIMEOUT = 180
    try:
        await asyncio.wait_for(
            asyncio.gather(
                read_stream(proc.stdout, False),
                read_stream(proc.stderr, True),
            ),
            timeout=JOB_TIMEOUT,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        log.warning("Job %s login timed out after %ds, killing process", job.email, JOB_TIMEOUT)
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        batch_state.broadcast({
            "type": "job_log", "job_id": job.id, "email": job.email,
            "log_type": "stderr", "message": f"Login timed out after {JOB_TIMEOUT}s",
            "proxy_used": proxy_url_used,
        })
        if not result_data:
            for prov in job.providers:
                if prov in ("kiro", "codebuddy"):
                    result_data[prov] = {"success": False, "error": f"Timed out after {JOB_TIMEOUT}s"}
    finally:
        if proc in batch_state._active_procs:
            batch_state._active_procs.remove(proc)

    return result_data, proxy_url_used


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

async def _import_kiro(job: AccountJob) -> bool:
    """Import Kiro tokens directly into DB (no more Kiro-Go binary)."""
    creds = job.result.get("kiro", {}).get("credentials", {})
    if not creds.get("access_token"):
        return False

    try:
        if job.account_id:
            kiro_expires_at = ""
            token_expiry = creds.get("expires_at") or creds.get("expires_in")
            if token_expiry:
                # expires_in is seconds from now
                try:
                    seconds = int(token_expiry)
                    kiro_expires_at = (datetime.utcnow() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    kiro_expires_at = str(token_expiry)
            else:
                kiro_expires_at = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

            # Store Kiro credits from quota if available
            quota = job.result.get("kiro", {}).get("quota") or {}
            kiro_credits = float(quota.get("remaining", 0) or quota.get("remaining_credits", 0) or 550.0)
            kiro_credits_total = float(quota.get("total_credits", 0) or quota.get("limit", 0) or 550.0)
            kiro_credits_used = float(quota.get("total_usage", 0) or quota.get("current_usage", 0) or 0)

            await db.update_account(
                job.account_id,
                status="active",  # ensure account is active when kiro login succeeds
                kiro_status="ok",
                kiro_access_token=creds["access_token"],
                kiro_refresh_token=creds.get("refresh_token", ""),
                kiro_profile_arn=creds.get("profile_arn", ""),
                kiro_error="",
                kiro_error_count=0,
                kiro_expires_at=kiro_expires_at,
                kiro_credits=kiro_credits,
                kiro_credits_total=kiro_credits_total,
                kiro_credits_used=kiro_credits_used,
            )
        batch_state.broadcast({
            "type": "import_ok",
            "provider": "kiro",
            "email": job.email,
        })
        return True
    except Exception as exc:
        log.exception("Kiro import error for %s: %s", job.email, exc)
        batch_state.broadcast({
            "type": "import_error",
            "provider": "kiro",
            "email": job.email,
            "error": str(exc),
        })
        return False


async def _import_codebuddy(job: AccountJob) -> bool:
    """Store CodeBuddy API key in DB."""
    creds = job.result.get("codebuddy", {}).get("credentials", {})
    api_key = creds.get("api_key", "")
    if not api_key:
        return False

    try:
        if job.account_id:
            # CodeBuddy keys expire 2 weeks after creation
            cb_expires_at = (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
            await db.update_account(
                job.account_id,
                cb_status="ok",
                cb_api_key=api_key,
                cb_credits=250.0,  # Default 250 credits per account
                cb_error="",
                cb_error_count=0,
                cb_expires_at=cb_expires_at,
            )
        batch_state.broadcast({
            "type": "import_ok",
            "provider": "codebuddy",
            "email": job.email,
        })
        return True
    except Exception as exc:
        log.exception("CodeBuddy import error for %s: %s", job.email, exc)
        batch_state.broadcast({
            "type": "import_error",
            "provider": "codebuddy",
            "email": job.email,
            "error": str(exc),
        })
        return False


async def _run_wavespeed_login(job: AccountJob, proxy_override: str | None = None) -> tuple[dict, str]:
    """Run wavespeed/register.py as subprocess to create WaveSpeed account + API key.

    Returns (result_data, proxy_url_used).
    proxy_override: if set, use this proxy instead of fetching from pool.
    """
    env = {**os.environ, "BATCHER_CAMOUFOX_HEADLESS": "true" if batch_state.headless else "false"}
    python_bin = str(PYTHON_BIN)
    ws_script = str(WAVESPEED_SCRIPT)

    if not os.path.exists(ws_script):
        log.error("WaveSpeed script not found: %s", ws_script)
        return {"wavespeed": {"success": False, "error": f"Script not found: {ws_script}"}}, ""

    # Use assigned proxy or fallback to pool rotation
    proxy_url_used = ""
    cmd_args = [
        python_bin, ws_script,
        "--email", job.email,
        "--password", job.password,
    ]
    if batch_state.headless:
        cmd_args.append("--headless")
    if proxy_override:
        cmd_args.extend(["--proxy", proxy_override])
        proxy_url_used = proxy_override
    else:
        proxy_info = await db.get_proxy_for_batch()
        if proxy_info:
            cmd_args.extend(["--proxy", proxy_info["url"]])
            proxy_url_used = proxy_info["url"]

    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    batch_state._active_procs.append(proc)

    result_data: dict = {}

    async def read_stream(stream: asyncio.StreamReader, is_stderr: bool = False) -> None:
        nonlocal result_data
        while True:
            line_bytes = await stream.readline()
            if not line_bytes:
                break
            text = line_bytes.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
                if parsed.get("type") == "result":
                    result_data = {"wavespeed": parsed}
                batch_state.broadcast({
                    "type": "job_log", "job_id": job.id, "email": job.email,
                    "log_type": parsed.get("type", "info"),
                    "provider": "wavespeed",
                    "step": parsed.get("step", ""),
                    "message": parsed.get("message", parsed.get("error", text)),
                    "proxy_used": proxy_url_used,
                })
            except json.JSONDecodeError:
                batch_state.broadcast({
                    "type": "job_log", "job_id": job.id, "email": job.email,
                    "log_type": "stderr" if is_stderr else "stdout",
                    "message": text,
                    "proxy_used": proxy_url_used,
                })
            job.logs.append({"ts": time.time(), "raw": text})

    # Timeout: kill subprocess if it takes too long (3 minutes per job)
    WS_TIMEOUT = 180
    try:
        await asyncio.wait_for(
            asyncio.gather(
                read_stream(proc.stdout, False),
                read_stream(proc.stderr, True),
            ),
            timeout=WS_TIMEOUT,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        log.warning("Job %s wavespeed timed out after %ds, killing process", job.email, WS_TIMEOUT)
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        batch_state.broadcast({
            "type": "job_log", "job_id": job.id, "email": job.email,
            "log_type": "stderr", "message": f"WaveSpeed login timed out after {WS_TIMEOUT}s",
            "proxy_used": proxy_url_used,
        })
        if "wavespeed" not in result_data:
            result_data["wavespeed"] = {"success": False, "error": f"Timed out after {WS_TIMEOUT}s"}
    finally:
        if proc in batch_state._active_procs:
            batch_state._active_procs.remove(proc)

    return result_data, proxy_url_used


async def _import_wavespeed(job: AccountJob) -> bool:
    """Store WaveSpeed API key in DB with $1 default credits."""
    ws_data = job.result.get("wavespeed", {})
    api_key = ws_data.get("api_key", "")
    if not api_key:
        return False

    try:
        if job.account_id:
            await db.update_account(
                job.account_id,
                ws_status="ok",
                ws_api_key=api_key,
                ws_credits=WS_DEFAULT_CREDITS,
                ws_error="",
                ws_error_count=0,
            )
        batch_state.broadcast({
            "type": "import_ok",
            "provider": "wavespeed",
            "email": job.email,
        })
        return True
    except Exception as exc:
        log.exception("WaveSpeed import error for %s: %s", job.email, exc)
        batch_state.broadcast({
            "type": "import_error",
            "provider": "wavespeed",
            "email": job.email,
            "error": str(exc),
        })
        return False


async def _run_chatbai_login(job: AccountJob, proxy_override: str | None = None) -> tuple[dict, str]:
    """Run chatbai signup script as subprocess — Camoufox Google OAuth signup + claim.

    Returns (result_data, proxy_url_used).
    """
    env = {**os.environ, "BATCHER_CAMOUFOX_HEADLESS": "true" if batch_state.headless else "false"}
    python_bin = str(PYTHON_BIN)
    cbai_script = str(CHATBAI_SCRIPT)

    if not os.path.exists(cbai_script):
        log.error("ChatBAI script not found: %s", cbai_script)
        return {"chatbai": {"success": False, "error": f"Script not found: {cbai_script}"}}, ""

    proxy_url_used = ""
    cmd_args = [
        python_bin, cbai_script,
        "--email", job.email,
        "--password", job.password,
    ]
    if batch_state.headless:
        cmd_args.append("--headless")
    if proxy_override:
        cmd_args.extend(["--proxy", proxy_override])
        proxy_url_used = proxy_override
    else:
        proxy_info = await db.get_proxy_for_batch()
        if proxy_info:
            cmd_args.extend(["--proxy", proxy_info["url"]])
            proxy_url_used = proxy_info["url"]

    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    batch_state._active_procs.append(proc)

    result_data: dict = {}

    async def read_stream(stream: asyncio.StreamReader, is_stderr: bool = False) -> None:
        nonlocal result_data
        while True:
            line_bytes = await stream.readline()
            if not line_bytes:
                break
            text = line_bytes.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
                if not isinstance(parsed, dict):
                    continue  # Skip non-dict JSON (strings, arrays, etc.)
                if parsed.get("type") == "result":
                    result_data = {"chatbai": parsed}
                batch_state.broadcast({
                    "type": "job_log", "job_id": job.id, "email": job.email,
                    "log_type": parsed.get("type", "info"),
                    "provider": "chatbai",
                    "step": parsed.get("step", ""),
                    "message": parsed.get("message", text[:200]),
                })
            except (json.JSONDecodeError, ValueError):
                if is_stderr:
                    log.debug("[chatbai stderr] %s", text[:200])

    await asyncio.gather(
        read_stream(proc.stdout, is_stderr=False),
        read_stream(proc.stderr, is_stderr=True),
    )
    await proc.wait()

    if proc in batch_state._active_procs:
        batch_state._active_procs.remove(proc)

    if not result_data:
        result_data = {"chatbai": {"success": False, "error": "No result from chatbai script"}}

    return result_data, proxy_url_used


async def _import_chatbai(job: AccountJob) -> bool:
    """Store ChatBAI API key in DB with default credits."""
    cbai_data = job.result.get("chatbai", {})
    api_key = cbai_data.get("api_key", "")
    session_token = cbai_data.get("session_token", "")

    log.info("ChatBAI import for %s: api_key=%s, session_token=%s, account_id=%s",
             job.email, bool(api_key), bool(session_token), job.account_id)

    if not api_key and not session_token:
        batch_state.broadcast({
            "type": "import_error", "provider": "chatbai",
            "email": job.email, "error": "No api_key or session_token",
        })
        return False

    if not job.account_id:
        batch_state.broadcast({
            "type": "import_error", "provider": "chatbai",
            "email": job.email, "error": "No account_id (account not created in DB)",
        })
        return False

    try:
        await db.update_account(
            job.account_id,
            cbai_status="ok",
            cbai_api_key=api_key,
            cbai_session_token=session_token,
            cbai_credits=CBAI_DEFAULT_CREDITS,
            cbai_error="",
            cbai_error_count=0,
        )
        batch_state.broadcast({
            "type": "import_ok",
            "provider": "chatbai",
            "email": job.email,
        })
        log.info("ChatBAI import OK for %s (api_key=%s)", job.email, api_key[:20] if api_key else "none")
        return True
    except Exception as exc:
        log.exception("ChatBAI import FAILED for %s: %s", job.email, exc)
        batch_state.broadcast({
            "type": "import_error", "provider": "chatbai",
            "email": job.email, "error": str(exc),
        })
        return False


async def _run_gumloop_login(
    job: AccountJob, proxy_override: str | None = None
) -> tuple[dict, str]:
    """Run gumloop_login.py as subprocess — Camoufox Google OAuth signup.

    For existing accounts with gl_refresh_token, does a quick token refresh instead.
    For new accounts, launches browser automation.
    """
    proxy_url_used = proxy_override or ""
    if not proxy_url_used:
        proxy_info = await db.get_proxy_for_batch()
        if proxy_info:
            proxy_url_used = proxy_info["url"]

    # Fast path: if account already has refresh_token, just refresh it (no browser needed)
    account = await db.get_account(job.account_id) if job.account_id else None
    if account and account.get("gl_refresh_token"):
        refresh_token = account["gl_refresh_token"]
        user_id = account.get("gl_user_id", "")

        batch_state.broadcast({
            "type": "job_log", "job_id": job.id, "email": job.email,
            "log_type": "info", "provider": "gumloop",
            "step": "refresh", "message": "Refreshing Firebase token (existing account)...",
            "proxy_used": proxy_url_used,
        })

        try:
            auth = GumloopAuth(
                refresh_token=refresh_token,
                user_id=user_id,
                proxy_url=proxy_url_used or None,
            )
            token = await auth.get_token()
            updated = auth.get_updated_tokens()

            batch_state.broadcast({
                "type": "job_log", "job_id": job.id, "email": job.email,
                "log_type": "info", "provider": "gumloop",
                "step": "refresh", "message": "Token refreshed OK",
                "proxy_used": proxy_url_used,
            })

            return {
                "gumloop": {
                    "success": True,
                    "credentials": {
                        "id_token": token,
                        "refresh_token": updated.get("gl_refresh_token", refresh_token),
                        "user_id": updated.get("gl_user_id", user_id) or auth.user_id,
                        "gummie_id": account.get("gl_gummie_id", ""),
                    },
                }
            }, proxy_url_used
        except Exception as e:
            batch_state.broadcast({
                "type": "job_log", "job_id": job.id, "email": job.email,
                "log_type": "error", "provider": "gumloop",
                "step": "refresh", "message": f"Token refresh failed: {e}, falling back to browser signup",
                "proxy_used": proxy_url_used,
            })
            # Fall through to browser signup

    # Browser signup path: run gumloop_login.py as subprocess
    env = {**os.environ, "BATCHER_CAMOUFOX_HEADLESS": "true" if batch_state.headless else "false"}
    python_bin = str(PYTHON_BIN)
    gl_script = str(GUMLOOP_SCRIPT)

    if not os.path.exists(gl_script):
        log.error("Gumloop script not found: %s", gl_script)
        return {"gumloop": {"success": False, "error": f"Script not found: {gl_script}"}}, ""

    cmd_args = [python_bin, gl_script, "--email", job.email, "--password", job.password]

    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    batch_state._active_procs.append(proc)

    result_data: dict = {}

    async def read_stream(stream: asyncio.StreamReader, is_stderr: bool = False) -> None:
        nonlocal result_data
        while True:
            line_bytes = await stream.readline()
            if not line_bytes:
                break
            text = line_bytes.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
                if parsed.get("type") == "result":
                    result_data = parsed
                batch_state.broadcast({
                    "type": "job_log", "job_id": job.id, "email": job.email,
                    "log_type": parsed.get("type", "info"),
                    "provider": "gumloop",
                    "step": parsed.get("step", ""),
                    "message": parsed.get("message", parsed.get("error", text)),
                    "proxy_used": proxy_url_used,
                })
            except json.JSONDecodeError:
                batch_state.broadcast({
                    "type": "job_log", "job_id": job.id, "email": job.email,
                    "log_type": "stderr" if is_stderr else "stdout",
                    "message": text,
                    "proxy_used": proxy_url_used,
                })
            job.logs.append({"ts": time.time(), "raw": text})

    GL_TIMEOUT = 180
    try:
        await asyncio.wait_for(
            asyncio.gather(
                read_stream(proc.stdout, False),
                read_stream(proc.stderr, True),
            ),
            timeout=GL_TIMEOUT,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        log.warning("Job %s gumloop timed out after %ds, killing process", job.email, GL_TIMEOUT)
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        batch_state.broadcast({
            "type": "job_log", "job_id": job.id, "email": job.email,
            "log_type": "stderr", "message": f"Gumloop login timed out after {GL_TIMEOUT}s",
            "proxy_used": proxy_url_used,
        })
        if "gumloop" not in result_data:
            result_data["gumloop"] = {"success": False, "error": f"Timed out after {GL_TIMEOUT}s"}
    finally:
        if proc in batch_state._active_procs:
            batch_state._active_procs.remove(proc)

    # Normalize result: gumloop_login.py outputs {"type": "result", "gumloop": {...}}
    if "gumloop" not in result_data and result_data.get("type") == "result":
        result_data = {"gumloop": result_data.get("gumloop", result_data)}

    return result_data, proxy_url_used


async def _run_skillboss_login(job: AccountJob) -> dict:
    """Run skillboss/register.py for Google OAuth signup."""
    from pathlib import Path
    from .config import PYTHON_BIN, AUTH_DIR

    script = Path(__file__).parent.parent / "skillboss" / "register.py"
    if not script.exists():
        batch_state.broadcast({
            "type": "job_log", "job_id": job.id, "email": job.email,
            "provider": "skillboss", "step": "error",
            "message": "skillboss/register.py not found",
        })
        return {"skillboss": {"success": False, "error": "skillboss/register.py not found"}}

    # Use the same python binary as other providers (from .venv)
    _py = PYTHON_BIN
    if not _py.exists():
        _py = PYTHON_BIN.with_suffix(".exe")
    python_bin = str(_py) if _py.exists() else "python"

    headless = "true" if batch_state.headless else "false"
    cmd = [python_bin, str(script), "--email", job.email, "--secret", job.password]

    batch_state.broadcast({
        "type": "job_log", "job_id": job.id, "email": job.email,
        "provider": "skillboss", "step": "start",
        "message": f"Launching SkillBoss signup (headless={headless})...",
    })

    env = {**os.environ, "BATCHER_CAMOUFOX_HEADLESS": headless, "BATCHER_ENABLE_CAMOUFOX": "true"}

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(AUTH_DIR),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        batch_state.broadcast({
            "type": "job_log", "job_id": job.id, "email": job.email,
            "provider": "skillboss", "step": "timeout",
            "message": "Signup timed out (180s)",
        })
        return {"skillboss": {"success": False, "error": "Signup timed out (180s)"}}
    except Exception as exc:
        batch_state.broadcast({
            "type": "job_log", "job_id": job.id, "email": job.email,
            "provider": "skillboss", "step": "error",
            "message": f"Process error: {exc}",
        })
        return {"skillboss": {"success": False, "error": str(exc)}}

    # Parse JSON lines from stdout — broadcast progress
    result_data: dict = {"skillboss": {"success": False, "error": "No result"}}
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get("type") == "progress":
                batch_state.broadcast({
                    "type": "job_log", "job_id": job.id, "email": job.email,
                    "provider": "skillboss", "step": data.get("step", ""),
                    "message": data.get("message", ""),
                })
            elif data.get("type") == "result":
                result_data = {"skillboss": data}
                break
        except (json.JSONDecodeError, ValueError):
            continue

    # Log stderr if failed
    if not result_data.get("skillboss", {}).get("success") and stderr:
        err_text = stderr.decode(errors="replace")[:300]
        batch_state.broadcast({
            "type": "job_log", "job_id": job.id, "email": job.email,
            "provider": "skillboss", "step": "stderr",
            "message": err_text,
        })
        if result_data["skillboss"].get("error") == "No result":
            result_data["skillboss"]["error"] = f"Process failed: {err_text[:200]}"

    # Wait for camoufox to fully cleanup before next run
    await asyncio.sleep(3)

    return result_data


async def _import_gumloop(job: AccountJob) -> bool:
    """Store Gumloop tokens in DB (from browser signup or token refresh)."""
    gl_data = job.result.get("gumloop", {})
    creds = gl_data.get("credentials", {})
    if not creds.get("id_token") and not creds.get("refresh_token"):
        return False

    try:
        # Ensure account exists in DB — create if missing
        # Only create if we actually have valid credentials (refresh_token is essential)
        if not job.account_id:
            if not creds.get("refresh_token"):
                log.warning("Skipping account creation for %s — no refresh_token", job.email)
                return False
            job.account_id = await db.create_account(job.email, job.password)
            log.info("Created account %d for %s (gumloop import)", job.account_id, job.email)

        updates = {
            "gl_status": "ok",
            "gl_id_token": creds.get("id_token", ""),
            "gl_refresh_token": creds.get("refresh_token", ""),
            "gl_user_id": creds.get("user_id", ""),
            "gl_error": "",
            "gl_error_count": 0,
        }
        # Set gummie_id if provided (from browser signup)
        gummie_id = creds.get("gummie_id", "")
        if gummie_id:
            updates["gl_gummie_id"] = gummie_id

        # If no gummie_id yet, try to create one via API
        if not gummie_id and creds.get("refresh_token"):
            try:
                from .gumloop.auth import GumloopAuth
                from .gumloop.client import create_gummie
                auth = GumloopAuth(
                    refresh_token=creds["refresh_token"],
                    user_id=creds.get("user_id", ""),
                    id_token=creds.get("id_token", ""),
                )
                new_gummie = await create_gummie(auth)
                if new_gummie:
                    updates["gl_gummie_id"] = new_gummie
                    batch_state.broadcast({
                        "type": "job_log", "job_id": job.id, "email": job.email,
                        "log_type": "info", "provider": "gumloop",
                        "step": "gummie", "message": f"Created gummie: {new_gummie}",
                    })
            except Exception as e:
                log.warning("Failed to create gummie for %s: %s", job.email, e)
                batch_state.broadcast({
                    "type": "job_log", "job_id": job.id, "email": job.email,
                    "log_type": "warn", "provider": "gumloop",
                    "step": "gummie", "message": f"Gummie creation failed: {e} — set manually later",
                })

        await db.update_account(job.account_id, **updates)

        # Push to D1 immediately so data survives server restarts
        try:
            from . import license_client
            updated_account = await db.get_account(job.account_id)
            if updated_account:
                await license_client.push_account_now(updated_account)
                log.info("Pushed gumloop account %s to D1", job.email)
        except Exception as push_err:
            log.warning("D1 push failed for %s (will retry on next sync): %s", job.email, push_err)

        batch_state.broadcast({
            "type": "import_ok", "provider": "gumloop", "email": job.email,
        })
        return True
    except Exception as exc:
        log.exception("Gumloop import error for %s: %s", job.email, exc)
        batch_state.broadcast({
            "type": "import_error", "provider": "gumloop",
            "email": job.email, "error": str(exc),
        })
        return False


# ---------------------------------------------------------------------------
# MCP server attachment
# ---------------------------------------------------------------------------

_MCP_API_BASE = "https://api.gumloop.com"


def _mcp_headers(id_token: str, user_id: str) -> dict:
    return {
        "Authorization": f"Bearer {id_token}",
        "x-auth-key": user_id,
        "Content-Type": "application/json",
    }


async def _attach_mcp_servers(job: AccountJob, proxy_url: str | None = None) -> None:
    """Attach MCP servers to a Gumloop account's gummie after import."""
    if not job.mcp_urls or not job.account_id:
        return

    account = await db.get_account(job.account_id)
    if not account:
        return

    result = await attach_mcp_to_account(
        account, job.mcp_urls, proxy_url=proxy_url,
        broadcast_fn=lambda msg: batch_state.broadcast({
            "type": "job_log", "job_id": job.id, "email": job.email,
            "log_type": "info", "provider": "gumloop",
            "step": "mcp", "message": msg,
        }),
    )
    if result.get("error"):
        raise RuntimeError(result["error"])


async def attach_mcp_to_account(
    account: dict,
    mcp_urls: list[str],
    proxy_url: str | None = None,
    broadcast_fn=None,
) -> dict:
    """Attach MCP server(s) to an existing Gumloop account. Callable from batch or API.

    Returns {"ok": True, "attached": N} or {"error": "..."}.
    """
    import httpx
    import random
    import string

    gummie_id = account.get("gl_gummie_id", "")
    refresh_tok = account.get("gl_refresh_token", "")
    if not gummie_id or not refresh_tok:
        return {"error": "Account has no gummie_id or refresh_token"}

    # Auth
    from .gumloop.auth import GumloopAuth
    auth = GumloopAuth(
        refresh_token=refresh_tok,
        user_id=account.get("gl_user_id", ""),
        id_token=account.get("gl_id_token", ""),
        proxy_url=proxy_url,
    )
    try:
        id_token = await auth.get_token()
    except Exception as e:
        return {"error": f"Token refresh failed: {e}"}
    user_id = auth.user_id
    headers = _mcp_headers(id_token, user_id)

    # Persist refreshed tokens
    updated_tokens = auth.get_updated_tokens()
    if updated_tokens.get("gl_id_token"):
        try:
            await db.update_account(account["id"], **updated_tokens)
        except Exception:
            pass

    def _log(msg: str):
        if broadcast_fn:
            broadcast_fn(msg)
        log.info("[MCP attach] %s: %s", account.get("email", "?"), msg)

    try:
        client_kwargs = {"timeout": 60}
        if proxy_url:
            client_kwargs["proxy"] = proxy_url

        async with httpx.AsyncClient(**client_kwargs) as client:
            # Get existing MCP secrets
            resp = await client.get(f"{_MCP_API_BASE}//secrets/mcp_servers", headers=headers)
            existing_secrets = resp.json() if resp.status_code == 200 else []
            existing_urls = [s.get("url", "") for s in existing_secrets]

            # Get current gummie tools to preserve existing active MCPs
            resp2 = await client.get(f"{_MCP_API_BASE}/gummies/{gummie_id}", headers=headers)
            current_mcp_tools = []
            if resp2.status_code == 200:
                current_tools = resp2.json().get("gummie", {}).get("tools", [])
                current_mcp_tools = [t for t in current_tools if t.get("type") == "mcp_server"]

            # Start with existing active MCPs (merge, not replace)
            tools = list(current_mcp_tools)
            active_urls = {t.get("mcp_server_url", "") for t in tools}

            # Add new MCPs
            for mcp_url in mcp_urls:
                mcp_url = mcp_url.strip()
                if not mcp_url or mcp_url in active_urls:
                    if mcp_url in active_urls:
                        _log(f"Already active: {mcp_url}")
                    continue

                if mcp_url in existing_urls:
                    secret_id = next((s["secret_id"] for s in existing_secrets if s.get("url") == mcp_url), "")
                    mcp_name = next((s["nickname"] for s in existing_secrets if s.get("url") == mcp_url), "mcp")
                    _log(f"Reusing existing MCP: {mcp_name}")
                else:
                    mcp_name = "mcp-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
                    payload = {
                        "secret_type": "mcp_server", "value": "",
                        "metadata": [
                            {"name": "URL", "value": mcp_url, "placeholder": "https://mcp.example.com"},
                            {"name": "Label", "value": mcp_name, "placeholder": "slack-mcp-server"},
                            {"name": "Access Token / API Key", "value": "", "isSecret": True,
                             "isOptional": True, "description": "OAuth authentication token",
                             "placeholder": "xxxxxxxxxxxxxxxxxxxxxxxx"},
                            {"name": "Additional Header", "value": "", "isOptional": True,
                             "description": "Additional Header",
                             "placeholder": "Authorization: Basic xxxxxxxxxxxxxxxxxxxxxxxx"},
                        ],
                        "nickname": mcp_name, "user_id": user_id,
                    }
                    resp = await client.post(
                        f"{_MCP_API_BASE}//secret", json=payload, headers=headers,
                    )
                    if resp.status_code not in (200, 201):
                        _log(f"Failed to create MCP secret for {mcp_url}: HTTP {resp.status_code}")
                        continue
                    secret_id = resp.json().get("secret_id", "")
                    _log(f"Created MCP secret: {mcp_name}")

                if secret_id:
                    tools.append({
                        "secret_id": secret_id, "mcp_server_url": mcp_url,
                        "name": mcp_name, "type": "mcp_server", "restricted_tools": [],
                    })

            if not tools:
                return {"error": "No MCP servers could be created"}

            # Add built-in tools
            tools.extend([
                {"metadata": {}, "type": "web_search"},
                {"metadata": {}, "type": "web_fetch"},
                {"metadata": {"model": "gemini-3.1-flash-image-preview"}, "type": "image_generator"},
                {"type": "interaction_search"},
            ])

            # Attach to gummie
            resp = await client.patch(
                f"{_MCP_API_BASE}/gummies/{gummie_id}",
                json={"tools": tools}, headers=headers,
            )
            if resp.status_code != 200:
                return {"error": f"Failed to attach tools to gummie: HTTP {resp.status_code}"}

            gummie_tools = resp.json().get("gummie", {}).get("tools", [])
            mcp_count = sum(1 for t in gummie_tools if t.get("type") == "mcp_server")
            _log(f"Attached {mcp_count} MCP server(s) to gummie")
            return {"ok": True, "attached": mcp_count}

    except Exception as e:
        return {"error": f"MCP attach error: {e}"}


# ---------------------------------------------------------------------------
# Single account retry
# ---------------------------------------------------------------------------

async def retry_account(account_id: int, providers: Optional[list[str]] = None) -> dict:
    """Retry login for a single account. Returns result dict."""
    account = await db.get_account(account_id)
    if account is None:
        return {"error": "Account not found"}

    if providers is None:
        providers = []
        if account["kiro_status"] in ("pending", "failed"):
            providers.append("kiro")
        if account["cb_status"] in ("pending", "failed"):
            providers.append("codebuddy")
        if account.get("ws_status") in ("none", "pending", "failed"):
            providers.append("wavespeed")
        if account.get("gl_status") in ("none", "pending", "failed"):
            providers.append("gumloop")
        if account.get("skboss_status") in ("none", "pending", "failed"):
            providers.append("skillboss")
        if not providers:
            providers = ["kiro", "codebuddy"]

    job = AccountJob(
        id=str(uuid.uuid4())[:8],
        email=account["email"],
        password=account["password"],
        providers=providers,
        account_id=account_id,
    )

    # Reset status
    await db.update_account(account_id, status="active")

    result: dict = {}

    # Run Kiro/CodeBuddy login if needed
    kiro_cb_providers = [p for p in providers if p in ("kiro", "codebuddy")]
    if kiro_cb_providers:
        result, _proxy = await _run_login(job)

    # Run WaveSpeed login separately if needed
    if "wavespeed" in providers:
        ws_result, _ws_proxy = await _run_wavespeed_login(job)
        result.update(ws_result)

    # Run Gumloop login (direct Python, no subprocess)
    if "gumloop" in providers:
        gl_result, _gl_proxy = await _run_gumloop_login(job)
        result.update(gl_result)

    # Run SkillBoss login (direct Python)
    if "skillboss" in providers:
        skboss_result = await _run_skillboss_login(job)
        result.update(skboss_result)

    job.result = result

    imported: list[str] = []
    if "kiro" in providers and result.get("kiro", {}).get("success"):
        ok = await _import_kiro(job)
        if ok:
            imported.append("kiro")
        else:
            await db.update_account(account_id, kiro_status="failed", kiro_error="Import failed")
    elif "kiro" in providers:
        error = result.get("kiro", {}).get("error", "Login failed")
        await db.update_account(account_id, kiro_status="failed", kiro_error=error)

    if "codebuddy" in providers and result.get("codebuddy", {}).get("success"):
        ok = await _import_codebuddy(job)
        if ok:
            imported.append("codebuddy")
        else:
            await db.update_account(account_id, cb_status="failed", cb_error="Import failed")
    elif "codebuddy" in providers:
        error = result.get("codebuddy", {}).get("error", "Login failed")
        await db.update_account(account_id, cb_status="failed", cb_error=error)

    if "wavespeed" in providers and result.get("wavespeed", {}).get("success"):
        ok = await _import_wavespeed(job)
        if ok:
            imported.append("wavespeed")
        else:
            await db.update_account(account_id, ws_status="failed", ws_error="Import failed")
    elif "wavespeed" in providers:
        error = result.get("wavespeed", {}).get("error", "Login failed")
        await db.update_account(account_id, ws_status="failed", ws_error=error)

    if "gumloop" in providers and result.get("gumloop", {}).get("success"):
        ok = await _import_gumloop(job)
        if ok:
            imported.append("gumloop")
        else:
            await db.update_account(account_id, gl_status="failed", gl_error="Import failed")
    elif "gumloop" in providers:
        error = result.get("gumloop", {}).get("error", "Login failed")
        await db.update_account(account_id, gl_status="failed", gl_error=error)

    if "skillboss" in providers and result.get("skillboss", {}).get("success"):
        api_key = result["skillboss"].get("api_key", "")
        if api_key:
            from .config import SKBOSS_DEFAULT_CREDITS
            await db.update_account(account_id, skboss_status="ok", skboss_api_key=api_key, skboss_credits=SKBOSS_DEFAULT_CREDITS, skboss_error="")
            imported.append("skillboss")
        else:
            await db.update_account(account_id, skboss_status="failed", skboss_error="No API key returned")
    elif "skillboss" in providers:
        error = result.get("skillboss", {}).get("error", "Login failed")
        await db.update_account(account_id, skboss_status="failed", skboss_error=error)

    # Only mark account failed if no provider works at all
    if not imported:
        refreshed = await db.get_account(account_id)
        has_any_ok = refreshed and (
            refreshed.get("kiro_status") == "ok"
            or refreshed.get("cb_status") == "ok"
            or refreshed.get("ws_status") == "ok"
            or refreshed.get("gl_status") == "ok"
            or refreshed.get("skboss_status") == "ok"
        )
        if not has_any_ok:
            await db.update_account(account_id, status="failed")

    return {
        "imported": imported,
        "kiro": result.get("kiro", {}),
        "codebuddy": result.get("codebuddy", {}),
        "wavespeed": result.get("wavespeed", {}),
    }
