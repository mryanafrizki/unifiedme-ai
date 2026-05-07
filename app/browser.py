"""Centralized anti-detection browser factory.

All browser launches go through this module to ensure consistent
fingerprint spoofing, WebGL/timezone randomization, and automation trace removal.

Usage:
    from app.browser import create_stealth_browser

    manager, browser, page = await create_stealth_browser(
        proxy={"server": "socks5://...", "username": "...", "password": "..."},
        headless=True,
    )
    # ... do work ...
    await manager.__aexit__(None, None, None)
"""

from __future__ import annotations

import os
import random
from typing import Any


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# OS choices weighted toward Windows (most common, least suspicious)
_OS_CHOICES = ["windows", "windows", "windows", "macos"]

# Screen resolutions (common real-world values)
_SCREEN_CONFIGS = [
    (1920, 1080),
    (1366, 768),
    (1536, 864),
    (1440, 900),
    (1680, 1050),
    (2560, 1440),
]

# Locales matching Singapore proxy
_LOCALES = ["en-US", "en-GB", "en-SG"]


def _get_headless() -> bool:
    """Get headless setting from env."""
    return os.getenv("BATCHER_CAMOUFOX_HEADLESS", "true").lower() == "true"


def _pick_os() -> str:
    """Pick random OS for fingerprint (weighted toward Windows)."""
    return random.choice(_OS_CHOICES)


def _pick_screen():
    """Pick random screen resolution."""
    from browserforge.fingerprints import Screen
    w, h = random.choice(_SCREEN_CONFIGS)
    return Screen(min_width=w, max_width=w, min_height=h, max_height=h)


def _pick_locale() -> str:
    """Pick random locale."""
    return random.choice(_LOCALES)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

async def create_stealth_browser(
    *,
    proxy: dict[str, str] | None = None,
    headless: bool | None = None,
    target_os: str | None = None,
    timeout: int = 20000,
    disable_coop: bool = True,
    humanize: bool | float = True,
    geoip: bool = True,
    block_images: bool = False,
) -> tuple[Any, Any, Any]:
    """Create a maximally stealthy browser session.

    Returns (manager, browser, page) tuple.
    Caller must close via: await manager.__aexit__(None, None, None)

    Anti-detection features:
    - Random OS fingerprint per session (weighted Windows)
    - Random screen resolution from common real-world values
    - WebGL vendor/renderer auto-matched to OS
    - Timezone/locale auto-derived from proxy IP (geoip=True)
    - Human-like cursor movement (humanize=True)
    - WebRTC blocked to prevent IP leak
    - No CDP traces (Camoufox uses Juggler, not CDP)
    - navigator.webdriver always false (C++ level patch)
    - Canvas/Audio/Font noise seeded uniquely per session
    - Playwright internals sandboxed (no __playwright__ leaks)
    """
    from browserforge.fingerprints import Screen
    from camoufox.async_api import AsyncCamoufox

    if headless is None:
        headless = _get_headless()

    if target_os is None:
        target_os = _pick_os()

    screen = _pick_screen()
    locale = _pick_locale()

    # Build proxy config for camoufox
    proxy_cfg = None
    if proxy:
        proxy_cfg = {
            "server": proxy.get("server", ""),
            "username": proxy.get("username", ""),
            "password": proxy.get("password", ""),
        }

    # Humanize: float = max seconds for cursor movement, True = default
    humanize_val = humanize
    if humanize is True:
        humanize_val = random.uniform(1.0, 2.5)
    elif humanize is False:
        humanize_val = False

    manager = AsyncCamoufox(
        headless=headless,
        os=target_os,
        geoip=geoip if proxy_cfg else False,
        humanize=humanize_val,
        block_webrtc=True,
        block_images=block_images,
        disable_coop=disable_coop,
        screen=screen,
        locale=locale,
        proxy=proxy_cfg,
        enable_cache=True,
        i_know_what_im_doing=True,
    )

    browser = await manager.__aenter__()
    page = await browser.new_page()
    page.set_default_timeout(timeout)

    return manager, browser, page


async def create_stealth_browser_simple(
    *,
    headless: bool | None = None,
    timeout: int = 20000,
) -> tuple[Any, Any, Any]:
    """Simplified version without proxy — for local/debug use.

    Still applies full fingerprint randomization.
    """
    return await create_stealth_browser(
        proxy=None,
        headless=headless,
        timeout=timeout,
        humanize=False,
        geoip=False,
    )
