"""Unified AI Proxy — FastAPI application entry point with CLI license flow."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from . import database as db
from .config import LISTEN_HOST, LISTEN_PORT, BASE_DIR, DATA_DIR, VERSION, CENTRAL_API_URL
from .router_proxy import router as proxy_router
from .router_admin import router as admin_router
from .proxy_kiro import close_all_clients as close_kiro
from .proxy_codebuddy import close_all_clients as close_codebuddy
from .proxy_wavespeed import close_all_clients as close_wavespeed
from .proxy_gumloop import close_all_clients as close_gumloop
from . import license_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("unified")

# License file path (persisted after first input)
LICENSE_FILE = DATA_DIR / ".license"


class QuietAccessFilter(logging.Filter):
    """Suppress noisy polling endpoints from uvicorn access log (only 200 OK)."""
    _quiet_paths = ("/api/logs", "/api/batch/status", "/api/stats", "/favicon.ico")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "200" not in msg:
            return True
        for path in self._quiet_paths:
            if f"GET {path}" in msg:
                return False
        return True


logging.getLogger("uvicorn.access").addFilter(QuietAccessFilter())


# ---------------------------------------------------------------------------
# CLI: License input + validation (runs BEFORE uvicorn)
# ---------------------------------------------------------------------------

_LICENSE_PATTERN = re.compile(r'^UNIF-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}$')


def _print_banner():
    print()
    print("  +======================================+")
    print(f"  |     Unified AI Proxy v{VERSION:<14s}|")
    print("  +======================================+")
    print()

    # Non-blocking update check
    try:
        import httpx
        logging.getLogger("httpx").setLevel(logging.WARNING)
        resp = httpx.get(f"{CENTRAL_API_URL}/api/version", timeout=3)
        data = resp.json()
        latest = data.get("version", "")
        if latest and latest != VERSION:
            print(f"  ** New version available: v{latest} **")
            print(f"  ** Run: unifiedme update **")
            print()
    except Exception:
        pass


def _load_saved_license() -> str:
    """Load license key from file or env var."""
    # Env var takes priority
    env_key = os.getenv("LICENSE_KEY", "").strip()
    if env_key:
        return env_key
    # Check saved file
    if LICENSE_FILE.exists():
        saved = LICENSE_FILE.read_text().strip()
        if saved:
            return saved
    return ""


def _save_license(key: str) -> None:
    """Save license key to local file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LICENSE_FILE.write_text(key)


def _prompt_license() -> str:
    """Interactive prompt for license key."""
    while True:
        try:
            key = input("  Enter license key: ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)

        if not key:
            print("  License key is required.\n")
            continue

        if not _LICENSE_PATTERN.match(key):
            print("  Invalid format. Expected: UNIF-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX\n")
            continue

        return key


def _validate_license_sync(key: str) -> dict:
    """Validate license against central API (synchronous wrapper)."""
    import httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)

    fingerprint = license_client._generate_fingerprint()
    pc_name = platform.node() or "unknown"
    os_name = f"{platform.system()} {platform.release()}"
    machine_id = license_client._get_machine_id()

    try:
        resp = httpx.post(
            f"{license_client.CENTRAL_API_URL}/api/auth/activate",
            json={
                "license_key": key,
                "device_fingerprint": fingerprint,
                "device_name": pc_name,
                "os": os_name,
                "pc_name": pc_name,
                "machine_id": machine_id,
            },
            timeout=15,
        )
        return resp.json()
    except Exception as e:
        return {"error": f"Cannot reach license server: {e}"}


def cli_license_flow() -> str:
    """Run the CLI license flow. Returns validated license key or exits."""
    _print_banner()

    key = _load_saved_license()

    if key:
        print(f"  License: {key}")
        print("  Validating...", end=" ", flush=True)
        result = _validate_license_sync(key)

        if result.get("ok"):
            print("OK")
            lic = result.get("license", {})
            print()
            print(f"  Owner:        {lic.get('owner_name', '?')}")
            print(f"  Tier:         {lic.get('tier', '?')}")
            print(f"  Max Devices:  {lic.get('max_devices', '?')}")
            print(f"  Max Accounts: {lic.get('max_accounts', '?')}")
            print(f"  Device ID:    {result.get('device_id', '?')}")
            if result.get("is_new"):
                print("  Status:       NEW device bound")
            print()
            return key
        else:
            print("FAILED")
            print(f"  Error: {result.get('error', 'Unknown error')}")
            print()
            # Saved key is invalid — clear it and prompt
            if LICENSE_FILE.exists():
                LICENSE_FILE.unlink()
    else:
        print("  No license key found.\n")

    # Interactive prompt
    while True:
        key = _prompt_license()
        print("  Validating...", end=" ", flush=True)
        result = _validate_license_sync(key)

        if result.get("ok"):
            print("OK")
            lic = result.get("license", {})
            print()
            print(f"  Owner:        {lic.get('owner_name', '?')}")
            print(f"  Tier:         {lic.get('tier', '?')}")
            print(f"  Max Devices:  {lic.get('max_devices', '?')}")
            print(f"  Max Accounts: {lic.get('max_accounts', '?')}")
            print(f"  Device ID:    {result.get('device_id', '?')}")
            if result.get("is_new"):
                print("  Status:       NEW device bound")
            print()

            # Save for next time
            _save_license(key)
            print(f"  License saved to {LICENSE_FILE}")
            print()
            return key
        else:
            print("FAILED")
            print(f"  Error: {result.get('error', 'Unknown error')}")
            print("  Try again.\n")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    log.info("Initializing database...")
    await db.init_db()

    # Seed default filter rules
    seeded = await db.seed_default_filters()
    if seeded:
        log.info("Seeded %d default filter rules", seeded)

    # Generate default API key if none exists
    keys = await db.get_api_keys()
    if not keys:
        key_id, full_key = await db.create_api_key("default")
        log.info("Generated default API key: %s", full_key)
        log.info("Save this key — it will not be shown again in logs.")
    else:
        log.info("Found %d existing API key(s)", len(keys))

    # License already validated in CLI flow — just activate in async context
    await license_client.activate()
    license_client.start_sync_loop()
    await license_client.pull_sync()

    # Check if admin password is set
    admin_pw = await db.get_setting("admin_password_set", "")
    if not admin_pw:
        log.info("First time setup — open http://localhost:%d/dashboard to set your admin password", LISTEN_PORT)

    log.info("Unified AI Proxy ready on port %d", LISTEN_PORT)

    yield

    # Shutdown
    log.info("Shutting down...")
    try:
        await license_client.stop_sync_loop()
    except Exception:
        pass
    try:
        await close_kiro()
    except Exception:
        pass
    try:
        await close_codebuddy()
    except Exception:
        pass
    try:
        await close_wavespeed()
    except Exception:
        pass
    try:
        await close_gumloop()
    except Exception:
        pass
    try:
        await db.close_db()
    except Exception:
        pass
    log.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Unified AI Proxy",
    description="Merged Kiro + CodeBuddy proxy with account management",
    version=VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers
app.include_router(proxy_router)
app.include_router(admin_router)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

DASHBOARD_PATH = BASE_DIR / "dashboard.html"


@app.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve the admin dashboard."""
    if DASHBOARD_PATH.exists():
        return FileResponse(DASHBOARD_PATH, media_type="text/html")
    return HTMLResponse(
        "<html><body><h1>Dashboard not found</h1>"
        "<p>Place dashboard.html in the unified/ directory.</p></body></html>",
        status_code=200,
    )


@app.get("/")
async def root():
    """Health check / info endpoint."""
    return {
        "service": "Unified AI Proxy",
        "version": VERSION,
        "endpoints": {
            "proxy": "/v1/chat/completions, /v1/messages, /v1/models",
            "admin": "/api/accounts, /api/keys, /api/stats, /api/batch/*",
            "dashboard": "/dashboard",
            "docs": "/docs",
        },
    }


# ---------------------------------------------------------------------------
# Admin password setup endpoint
# ---------------------------------------------------------------------------

@app.post("/api/setup-password")
async def setup_password(request: Request):
    """First-time password setup. Only works if no password is set yet."""
    body = await request.json()
    new_password = str(body.get("password", "")).strip()
    if not new_password or len(new_password) < 4:
        return {"error": "Password must be at least 4 characters"}

    # Check if already set
    existing = await db.get_setting("admin_password_set", "")
    if existing:
        return {"error": "Password already set. Use dashboard to change it."}

    # Save password
    from .config import ADMIN_PASSWORD
    # Update the runtime config
    import unified.config as cfg
    cfg.ADMIN_PASSWORD = new_password
    # Persist to DB
    await db.set_setting("admin_password", new_password)
    await db.set_setting("admin_password_set", "1")

    return {"ok": True, "message": "Password set successfully"}


@app.get("/api/setup-status")
async def setup_status():
    """Check if first-time setup is needed."""
    pw_set = await db.get_setting("admin_password_set", "")
    return {"password_set": bool(pw_set)}


@app.get("/api/uptime")
async def get_uptime():
    """Return server uptime + version."""
    from .cli import get_uptime_seconds
    uptime = get_uptime_seconds()
    h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
    return {
        "version": VERSION,
        "uptime_seconds": uptime,
        "uptime_human": f"{h}h {m}m {s}s",
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import uvicorn

    # Step 1: CLI license flow (interactive, before uvicorn starts)
    license_key = cli_license_flow()

    # Set env var so license_client.activate() picks it up in lifespan
    os.environ["LICENSE_KEY"] = license_key

    print(f"  Starting proxy on port {LISTEN_PORT}...")
    print()
    print(f"  Dashboard:  http://localhost:{LISTEN_PORT}/dashboard")
    print(f"  API:        http://localhost:{LISTEN_PORT}/v1/chat/completions")
    print()
    print("  First time? Open the dashboard to set your admin password")
    print("  and get your API key.")
    print()

    uvicorn.run(
        "unified.main:app",
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
