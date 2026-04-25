"""Unified AI Proxy — FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import database as db
from .config import LISTEN_HOST, LISTEN_PORT, BASE_DIR
from .router_proxy import router as proxy_router
from .router_admin import router as admin_router
from .proxy_kiro import close_all_clients as close_kiro
from .proxy_codebuddy import close_all_clients as close_codebuddy
from .proxy_wavespeed import close_all_clients as close_wavespeed
from .proxy_gumloop import close_all_clients as close_gumloop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("unified")


class QuietAccessFilter(logging.Filter):
    """Suppress noisy polling endpoints from uvicorn access log (only 200 OK)."""
    _quiet_paths = ("/api/logs", "/api/batch/status", "/api/stats", "/favicon.ico")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # Only suppress successful (200) polling requests
        if "200" not in msg:
            return True  # Always show errors
        for path in self._quiet_paths:
            if f"GET {path}" in msg:
                return False
        return True


logging.getLogger("uvicorn.access").addFilter(QuietAccessFilter())


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

    log.info("Unified AI Proxy ready on port %d", LISTEN_PORT)

    yield

    # Shutdown
    log.info("Shutting down...")
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
    version="1.0.0",
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
        "version": "1.0.0",
        "endpoints": {
            "proxy": "/v1/chat/completions, /v1/messages, /v1/models",
            "admin": "/api/accounts, /api/keys, /api/stats, /api/batch/*",
            "dashboard": "/dashboard",
            "docs": "/docs",
        },
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import uvicorn
    uvicorn.run(
        "unified.main:app",
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
