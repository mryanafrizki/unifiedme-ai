"""Cloudflare Turnstile captcha solver via 2Captcha.

Strategy (mirrors Gumloop's frontend behavior):
- Tokens are single-use but valid for ~280s if unused.
- On first request: solve and return token.
- After token is consumed: immediately prefetch next token in background.
- Next request gets the prefetched token instantly (0s wait).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger("unified.gumloop.turnstile")

TURNSTILE_SITEKEY = "0x4AAAAAACMum7HpvvFmcf2r"
TURNSTILE_URL = "https://www.gumloop.com"
TURNSTILE_ACTION = "websocket_connect"
TOKEN_TTL = 250  # seconds, safe margin under ~280s actual validity


class TurnstileSolver:
    """Instance-based Turnstile solver with prefetch strategy."""

    def __init__(self, captcha_api_key: str = ""):
        self._api_key = captcha_api_key
        self._ready_token: Optional[str] = None
        self._ready_at: float = 0
        self._prefetch_task: Optional[asyncio.Task] = None
        self._solve_lock = asyncio.Lock()
        self.solve_count: int = 0
        self.solve_errors: int = 0

    def update_api_key(self, key: str) -> None:
        self._api_key = key

    async def _solve(self) -> Optional[str]:
        """Solve a single Turnstile challenge via 2Captcha."""
        if not self._api_key:
            return None

        try:
            from twocaptcha import TwoCaptcha

            start = time.time()
            solver = TwoCaptcha(self._api_key, defaultTimeout=120, pollingInterval=5)

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: solver.turnstile(
                    sitekey=TURNSTILE_SITEKEY,
                    url=TURNSTILE_URL,
                    action=TURNSTILE_ACTION,
                ),
            )

            token = result.get("code", "")
            elapsed = time.time() - start

            if token:
                log.info("[turnstile] Solved in %.1fs (len=%d)", elapsed, len(token))
                self.solve_count += 1
                return token

            log.warning("[turnstile] 2Captcha returned empty token")
            self.solve_errors += 1
            return None

        except Exception as e:
            log.error("[turnstile] Solve failed: %s", e)
            self.solve_errors += 1
            return None

    def _start_prefetch(self) -> None:
        """Start background prefetch of next token."""
        if self._prefetch_task and not self._prefetch_task.done():
            return  # Already prefetching

        async def _do_prefetch():
            token = await self._solve()
            if token:
                self._ready_token = token
                self._ready_at = time.time()
                log.info("[turnstile] Prefetched token ready")

        try:
            self._prefetch_task = asyncio.create_task(_do_prefetch())
        except RuntimeError:
            pass  # No event loop

    async def get_token(self) -> Optional[str]:
        """Get a Turnstile token. Uses prefetched if available, else solves fresh."""
        if not self._api_key:
            return None

        async with self._solve_lock:
            # 1. Use prefetched token if fresh
            if self._ready_token and (time.time() - self._ready_at) < TOKEN_TTL:
                token = self._ready_token
                age = int(time.time() - self._ready_at)
                self._ready_token = None
                self._ready_at = 0
                log.info("[turnstile] Using prefetched token (age=%ds)", age)
                self._start_prefetch()
                return token

            # 2. Wait for in-flight prefetch if it exists
            if self._prefetch_task and not self._prefetch_task.done():
                log.info("[turnstile] Waiting for in-flight prefetch...")
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._prefetch_task), timeout=130
                    )
                except (asyncio.TimeoutError, Exception):
                    pass

                if self._ready_token and (time.time() - self._ready_at) < TOKEN_TTL:
                    token = self._ready_token
                    self._ready_token = None
                    self._ready_at = 0
                    self._start_prefetch()
                    return token

            # 3. Solve fresh
            log.info("[turnstile] Solving fresh token...")
            token = await self._solve()
            if token:
                self._start_prefetch()
            return token

    def close(self) -> None:
        """Cancel prefetch task if running."""
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
            self._prefetch_task = None
