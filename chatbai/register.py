#!/usr/bin/env python3
"""
chat.b.ai Signup Interceptor — Google OAuth + HAR capture.

Opens a Camoufox browser, automates Google OAuth login on chat.b.ai,
and captures ALL HTTP request/response pairs.

Usage:
    python intercept_chatbai.py --email you@gmail.com --password yourpass
    python intercept_chatbai.py --email you@gmail.com --password yourpass --headless
    python intercept_chatbai.py --email you@gmail.com --password yourpass --proxy http://192.168.18.25:9015

Output:
    - Console: live filtered request log
    - chatbai/register_log.json: full HAR dump on exit

Press Ctrl+C when done to save the log.
"""

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Fix Windows socketpair broken by proxy apps (CliProxy, etc.) ────
# Python's socket.socketpair() fails when a system-level proxy intercepts
# localhost connections. This patches it with a safe alternative.
if sys.platform == "win32":
    _orig_socketpair = socket.socketpair

    def _patched_socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
        """socketpair that works even when localhost is intercepted."""
        try:
            return _orig_socketpair(family, type, proto)
        except (ConnectionError, OSError):
            pass
        # Fallback: manual socketpair via listen+connect
        lsock = socket.socket(family, type, proto)
        try:
            lsock.bind(("127.0.0.1", 0))
            lsock.listen(1)
            addr, port = lsock.getsockname()
            csock = socket.socket(family, type, proto)
            csock.setblocking(False)
            try:
                csock.connect((addr, port))
            except (BlockingIOError, OSError):
                pass
            csock.setblocking(True)
            ssock, _ = lsock.accept()
            return ssock, csock
        finally:
            lsock.close()

    socket.socketpair = _patched_socketpair

# ── Config ──────────────────────────────────────────────────────────
CHATBAI_URL = "https://chat.b.ai"
OUTPUT_DIR = Path(__file__).parent
LOG_FILE = OUTPUT_DIR / "register_log.json"

# Domains to capture
CAPTURE_DOMAINS = [
    "chat.b.ai",
    "b.ai",
    "api.b.ai",
    "ainft.com",
    "chat.ainft.com",
]

# Skip noisy static resources
SKIP_PATTERNS = [
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".woff", ".woff2", ".ttf", ".ico", ".map",
    "analytics", "gtag", "sentry", "hotjar", "posthog",
    "intercom", "segment", "mixpanel", "amplitude",
    "google-analytics", "googletagmanager", "facebook.com/tr", "clarity.ms",
]

# Captured data
captured: list[dict] = []
ws_frames: list[dict] = []


def emit(data: dict):
    try:
        print(json.dumps(data, ensure_ascii=False), flush=True)
    except (BrokenPipeError, UnicodeEncodeError):
        try:
            print(json.dumps(data, ensure_ascii=True), flush=True)
        except Exception:
            pass


def safe_print(*args, **kwargs):
    """Print that won't crash on Windows with unicode."""
    try:
        print(*args, **kwargs)
    except (UnicodeEncodeError, UnicodeDecodeError):
        try:
            text = " ".join(str(a) for a in args)
            print(text.encode("ascii", errors="replace").decode(), **kwargs)
        except Exception:
            pass


def should_capture(url: str, method: str) -> bool:
    url_lower = url.lower()
    if any(p in url_lower for p in SKIP_PATTERNS):
        return False
    if any(d in url_lower for d in CAPTURE_DOMAINS):
        return True
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        return True
    return False


def format_entry(entry: dict) -> str:
    method = entry.get("method", "?")
    url = entry.get("url", "?")
    status = entry.get("status", "?")

    short_url = url
    for domain in CAPTURE_DOMAINS:
        short_url = short_url.replace(f"https://{domain}", "")
        short_url = short_url.replace(f"http://{domain}", "")

    line = f"  [{method}] {short_url[:80]} -> {status}"

    req_body = entry.get("request_body")
    if req_body and req_body != "null":
        try:
            parsed = json.loads(req_body) if isinstance(req_body, str) else req_body
            body_str = json.dumps(parsed, indent=2, ensure_ascii=False)
            if len(body_str) > 500:
                body_str = body_str[:500] + "..."
            line += f"\n    REQ: {body_str}"
        except (json.JSONDecodeError, TypeError):
            if len(str(req_body)) < 500:
                line += f"\n    REQ: {req_body}"

    resp_body = entry.get("response_body")
    if resp_body:
        try:
            parsed = json.loads(resp_body) if isinstance(resp_body, str) else resp_body
            body_str = json.dumps(parsed, indent=2, ensure_ascii=False)
            if len(body_str) > 1000:
                body_str = body_str[:1000] + "..."
            line += f"\n    RES: {body_str}"
        except (json.JSONDecodeError, TypeError):
            if len(str(resp_body)) < 1000:
                line += f"\n    RES: {resp_body}"

    return line


# ── Google OAuth helpers (from gumloop_login.py / wavespeed/register.py pattern) ──

async def fill_google_email(page, email: str) -> bool:
    """Fill Google email step (matches gumloop_login.py pattern)."""
    try:
        await page.wait_for_selector("#identifierId", state="visible", timeout=30000)
        locator = page.locator("#identifierId").first
        await locator.click(force=True)
        await asyncio.sleep(0.2)
        await locator.press("Control+a")
        await locator.press("Backspace")
        await locator.press_sequentially(email, delay=60)
        await asyncio.sleep(0.5)

        # Click Next — try ID first, then by text
        await page.evaluate("""() => {
            const byId = document.querySelector('#identifierNext button');
            if (byId && byId.offsetParent !== null) { byId.click(); return; }
            for (const btn of document.querySelectorAll('button, div[role="button"]')) {
                const txt = (btn.textContent || '').trim();
                if (txt === 'Next' && btn.offsetParent !== null) { btn.click(); return; }
            }
        }""")
        await asyncio.sleep(2)
        return True
    except Exception as exc:
        emit({"type": "debug", "step": "email", "message": f"fill_google_email error: {exc}"})
        return False


async def _inject_input_value(page, selector: str, value: str) -> bool:
    """Inject value into input using React's native setter (bypasses Google's input guard)."""
    try:
        return bool(await page.evaluate("""({sel, val}) => {
            const el = document.querySelector(sel);
            if (!el || el.offsetParent === null) return false;
            el.focus();
            // Use React's native value setter to bypass input guards
            const proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
            const setter = proto ? Object.getOwnPropertyDescriptor(proto, 'value')?.set : null;
            if (setter) {
                setter.call(el, val);
            } else {
                el.value = val;
            }
            el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
            el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
            // Also dispatch keydown/keyup for good measure
            el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true }));
            el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
            return el.value === val;
        }""", {"sel": selector, "val": value}))
    except Exception:
        return False


async def fill_google_password(page, password: str) -> bool:
    """Fill Google password step — multiple strategies."""
    try:
        await page.wait_for_selector('input[name="Passwd"]', state="visible", timeout=30000)
    except Exception:
        return False

    locator = page.locator('input[name="Passwd"]').first

    # Strategy 1: press_sequentially (human-like, works in most cases)
    await locator.click(force=True)
    await asyncio.sleep(0.3)
    await locator.press("Control+a")
    await locator.press("Backspace")
    await asyncio.sleep(0.2)

    # Use Playwright's fill() — this uses internal input mechanism, not keyboard events
    # More reliable than press_sequentially in popup windows
    await locator.fill(password)
    await asyncio.sleep(0.5)

    typed = await locator.input_value()
    emit({"type": "debug", "step": "password", "message": f"fill(): typed={len(typed)}, expected={len(password)}"})

    if len(typed) != len(password):
        # Fallback: press_sequentially
        emit({"type": "debug", "step": "password", "message": "Fallback to press_sequentially..."})
        await locator.click(force=True)
        await locator.press("Control+a")
        await locator.press("Backspace")
        await locator.press_sequentially(password, delay=70)
        await asyncio.sleep(0.3)
        typed = await locator.input_value()
        emit({"type": "debug", "step": "password", "message": f"press_sequentially: typed={len(typed)}"})

    # Remember URL before clicking Next
    url_before = page.url

    # Click Next
    await locator.press("Enter")

    # Wait for navigation away from password page
    for _ in range(15):
        await asyncio.sleep(1)
        try:
            current_url = page.url
            if current_url != url_before:
                emit({"type": "debug", "step": "password", "message": f"Navigated to: {current_url[:80]}"})
                await asyncio.sleep(1)
                return True
            has_error = await page.evaluate("""() => {
                const err = document.querySelector('.LXRPh');
                return err && err.offsetParent !== null ? err.textContent : null;
            }""")
            if has_error:
                emit({"type": "error", "step": "password", "message": f"Google error: {has_error}"})
                return False
        except Exception:
            pass

    emit({"type": "debug", "step": "password", "message": "No navigation after 15s, proceeding anyway"})
    return True


async def handle_consent_and_redirect(
    page, target_domain: str = "chat.b.ai", popup_page=None,
) -> bool:
    """Handle Google consent/gaplustos/speedbump screens and wait for redirect.

    Polls BOTH the popup page AND the main page for consent screens.
    Google GIS flow shows consent in popup, not main page.
    Only checks for successful redirect AFTER consent was clicked or popup closed.
    (Pattern from gumloop_login.py)
    """
    clicked_consent = False
    clicked_gaplustos = False
    failed_click_count = 0
    _MAX_FAILED_CLICKS = 15

    async def _try_click_consent(target) -> str:
        """Try to click Continue/Allow on a Google consent page."""
        try:
            return str(await target.evaluate("""() => {
                // Strategy 1: Find button by exact text
                for (const btn of document.querySelectorAll('button, div[role="button"], a[role="button"]')) {
                    const t = (btn.textContent||'').trim().toLowerCase();
                    if (!t || btn.offsetParent === null) continue;
                    if (t === 'continue' || t === 'allow' || t === 'lanjutkan' || t === 'lanjut'
                        || t.includes('continue') || t.includes('allow')) {
                        btn.click(); return 'text:' + t;
                    }
                }
                // Strategy 2: Find submit buttons/inputs
                for (const el of document.querySelectorAll('input[type="submit"], input[type="button"]')) {
                    const v = (el.value||'').toLowerCase();
                    if (v.includes('continue') || v.includes('allow') || v.includes('next')) {
                        el.click(); return 'input:' + v;
                    }
                }
                // Strategy 3: Find by ID patterns common in Google consent
                const byId = document.querySelector('#submit_approve_access')
                    || document.querySelector('[data-idom-class*="continue"]');
                if (byId) { byId.click(); return 'id:' + (byId.id || 'found'); }
                // Return debug info about visible buttons
                const btns = [];
                for (const btn of document.querySelectorAll('button')) {
                    if (btn.offsetParent !== null) btns.push(btn.textContent.trim().substring(0, 30));
                }
                return btns.length > 0 ? 'no_match:' + btns.join('|') : 'no_buttons';
            }""") or "")
        except Exception:
            return ""

    for tick in range(60):
        await asyncio.sleep(1)
        try:
            # ── Gather URLs from both pages ─────────────────────────
            popup_url = ""
            popup_closed = True
            try:
                if popup_page:
                    popup_closed = popup_page.is_closed()
                    if not popup_closed:
                        popup_url = popup_page.url
            except Exception:
                popup_closed = True

            main_url = ""
            try:
                main_url = page.url if not page.is_closed() else ""
            except Exception:
                pass

            if tick % 5 == 0:
                emit({"type": "debug", "step": "consent",
                      "tick": tick, "popup": "closed" if popup_closed else popup_url[:60],
                      "main": main_url[:60], "clicked": clicked_consent})

            # ── Too many failed clicks — bail ───────────────────────
            if failed_click_count >= _MAX_FAILED_CLICKS:
                emit({"type": "debug", "step": "consent", "message": f"Bailing after {failed_click_count} failed clicks"})
                return False

            # ── Gaplustos on popup ──────────────────────────────────
            if not popup_closed and "/speedbump/gaplustos" in popup_url and not clicked_gaplustos:
                await popup_page.evaluate("""() => {
                    const el = document.querySelector('#confirm') || document.querySelector('input[type="submit"]');
                    if (el) { el.click(); return; }
                    for (const btn of document.querySelectorAll('button')) {
                        const t = (btn.textContent||'').toLowerCase();
                        if ((t==='continue'||t==='i agree'||t.includes('accept')) && btn.offsetParent!==null) { btn.click(); return; }
                    }
                }""")
                clicked_gaplustos = True
                failed_click_count = 0
                emit({"type": "progress", "step": "consent", "message": "Clicked gaplustos on popup"})
                await asyncio.sleep(3)
                continue

            # ── Gaplustos on main page ──────────────────────────────
            if "/speedbump/gaplustos" in main_url and not clicked_gaplustos:
                await page.evaluate("""() => {
                    const el = document.querySelector('#confirm') || document.querySelector('input[type="submit"]');
                    if (el) { el.click(); return; }
                    for (const btn of document.querySelectorAll('button')) {
                        const t = (btn.textContent||'').toLowerCase();
                        if ((t==='continue'||t==='i agree'||t.includes('accept')) && btn.offsetParent!==null) { btn.click(); return; }
                    }
                }""")
                clicked_gaplustos = True
                failed_click_count = 0
                emit({"type": "progress", "step": "consent", "message": "Clicked gaplustos on main"})
                await asyncio.sleep(3)
                continue

            # ── Try consent click on POPUP page ─────────────────────
            if not popup_closed and "accounts.google.com" in popup_url and not clicked_consent:
                result = await _try_click_consent(popup_page)
                emit({"type": "debug", "step": "consent", "message": f"popup click: {result}"})
                if result and not result.startswith("no_"):
                    clicked_consent = True
                    failed_click_count = 0
                    emit({"type": "progress", "step": "consent", "message": f"Clicked consent on popup: {result}"})
                    await asyncio.sleep(3)
                    continue
                else:
                    failed_click_count += 1

            # ── Try consent click on MAIN page ──────────────────────
            if "accounts.google.com" in main_url and not clicked_consent:
                result = await _try_click_consent(page)
                emit({"type": "debug", "step": "consent", "message": f"main click: {result}"})
                if result and not result.startswith("no_"):
                    clicked_consent = True
                    failed_click_count = 0
                    emit({"type": "progress", "step": "consent", "message": f"Clicked consent on main: {result}"})
                    await asyncio.sleep(3)
                    continue
                else:
                    failed_click_count += 1

            # ── Check for successful redirect (ONLY after consent clicked or popup closed) ──
            if clicked_consent or clicked_gaplustos or popup_closed:
                try:
                    main_url = page.url if not page.is_closed() else ""
                except Exception:
                    main_url = ""

                if target_domain in main_url and "accounts.google.com" not in main_url:
                    emit({"type": "progress", "step": "redirect", "message": f"Landed: {main_url[:80]}"})
                    return True

        except Exception as exc:
            emit({"type": "debug", "step": "consent", "message": f"error: {exc}"})

    return False


async def main(email: str, password: str, headless: bool = False, proxy_url: str = ""):
    from browserforge.fingerprints import Screen
    from camoufox.async_api import AsyncCamoufox

    # Also respect BATCHER_CAMOUFOX_HEADLESS env var (set by batch_runner)
    if os.getenv("BATCHER_CAMOUFOX_HEADLESS", "").lower() in ("true", "1"):
        headless = True

    emit({"type": "progress", "step": "init", "message": f"Starting ChatBAI signup for {email}..."})
    emit({"type": "debug", "step": "init", "message": f"headless={headless}, proxy={proxy_url or 'none'}, BATCHER_HEADLESS={os.getenv('BATCHER_CAMOUFOX_HEADLESS', 'unset')}"})

    # ── Launch Camoufox ─────────────────────────────────────────────
    # Firefox/Playwright doesn't support SOCKS5 with auth — convert to HTTP with separate auth fields
    proxy_cfg = None
    if proxy_url:
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)
        # Extract username:password if present (use proxy_ prefix to avoid shadowing function params)
        proxy_user = parsed.username or ""
        proxy_pass = parsed.password or ""
        # Build server URL without credentials, force HTTP scheme for Firefox compat
        scheme = "http" if parsed.scheme.startswith("socks") else parsed.scheme
        server = f"{scheme}://{parsed.hostname}:{parsed.port}"
        proxy_cfg = {"server": server}
        if proxy_user:
            proxy_cfg["username"] = proxy_user
        if proxy_pass:
            proxy_cfg["password"] = proxy_pass
        emit({"type": "debug", "step": "init", "message": f"Proxy: {server} (auth={'yes' if proxy_user else 'no'})"})

    try:
        manager = AsyncCamoufox(
            headless=headless,
            os="windows",
            block_webrtc=True,
            humanize=False,
            screen=Screen(max_width=1920, max_height=1080),
            proxy=proxy_cfg,
        )
    except Exception as exc:
        emit({"type": "result", "success": False, "error": f"Browser launch failed: {exc}", "email": email})
        return
    try:
        browser = await manager.__aenter__()
    except Exception as exc:
        emit({"type": "result", "success": False, "error": f"Browser start failed: {exc}", "email": email})
        return

    try:
        page = await browser.new_page()
        page.set_default_timeout(30000)
    except Exception as exc:
        emit({"type": "result", "success": False, "error": f"Page creation failed: {exc}", "email": email})
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            pass
        return

    try:
        await _run_signup_flow(page, email, password, manager)
    except Exception as exc:
        emit({"type": "result", "success": False, "error": f"Signup flow error: {exc}", "email": email})
    finally:
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            pass


async def _run_signup_flow(page, email: str, password: str, manager):
    """Core signup flow — separated for clean error handling."""

    # ── HTTP interceptor ────────────────────────────────────────────
    async def on_response(response):
        url = response.url
        request = response.request
        method = request.method

        if not should_capture(url, method):
            return

        entry = {
            "timestamp": datetime.now().isoformat(),
            "method": method,
            "url": url,
            "status": response.status,
            "request_headers": dict(request.headers),
            "request_body": None,
            "response_headers": dict(response.headers),
            "response_body": None,
        }

        if method in ("POST", "PATCH", "PUT", "DELETE"):
            try:
                entry["request_body"] = request.post_data
            except Exception:
                pass

        try:
            body = await response.text()
            entry["response_body"] = body
        except Exception:
            pass

        captured.append(entry)
        safe_print(format_entry(entry))
        safe_print()

    page.on("response", on_response)

    # ── WebSocket capture ───────────────────────────────────────────
    def on_websocket(ws):
        ws_url = ws.url
        emit({"type": "ws_open", "url": ws_url})

        def on_frame_sent(payload):
            ws_frames.append({
                "timestamp": datetime.now().isoformat(),
                "direction": "sent", "url": ws_url,
                "payload": str(payload)[:2000],
            })
            safe_print(f"  [WS->] {ws_url[:60]} : {str(payload)[:200]}")

        def on_frame_received(payload):
            ws_frames.append({
                "timestamp": datetime.now().isoformat(),
                "direction": "received", "url": ws_url,
                "payload": str(payload)[:2000],
            })
            safe_print(f"  [WS<-] {ws_url[:60]} : {str(payload)[:200]}")

        ws.on("framesent", on_frame_sent)
        ws.on("framereceived", on_frame_received)

    page.on("websocket", on_websocket)

    # ── Navigate to chat.b.ai ───────────────────────────────────────
    emit({"type": "progress", "step": "navigate", "message": f"Opening {CHATBAI_URL}..."})
    await page.goto(CHATBAI_URL, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    # ── Click Log in ────────────────────────────────────────────────
    emit({"type": "progress", "step": "login_click", "message": "Clicking Log in..."})
    for _ in range(10):
        clicked = await page.evaluate("""() => {
            for (const b of document.querySelectorAll('button')) {
                if (b.textContent.trim() === 'Log in' && b.offsetParent !== null) {
                    b.click(); return true;
                }
            }
            return false;
        }""")
        if clicked:
            break
        await asyncio.sleep(1)
    await asyncio.sleep(2)

    # ── Click "Continue with Google" ────────────────────────────────
    emit({"type": "progress", "step": "google_click", "message": "Clicking Continue with Google..."})

    # Listen for popup (Google GIS opens OAuth in popup)
    popup_page = None
    popup_future = asyncio.get_event_loop().create_future()

    def on_popup(p):
        nonlocal popup_page
        popup_page = p
        if not popup_future.done():
            popup_future.set_result(p)

    page.context.on("page", on_popup)

    for _ in range(10):
        clicked = await page.evaluate("""() => {
            for (const b of document.querySelectorAll('button, a, div[role="button"]')) {
                const txt = (b.textContent || '').toLowerCase();
                if (txt.includes('continue with google') && b.offsetParent !== null) {
                    b.click(); return true;
                }
            }
            return false;
        }""")
        if clicked:
            break
        await asyncio.sleep(1)

    # Wait for popup or same-page navigation
    try:
        await asyncio.wait_for(popup_future, timeout=8)
    except asyncio.TimeoutError:
        pass

    if popup_page:
        emit({"type": "debug", "step": "popup", "url": popup_page.url[:80]})
        await popup_page.wait_for_load_state("domcontentloaded", timeout=10000)
        google_page = popup_page
    else:
        for _ in range(10):
            if "accounts.google.com" in page.url:
                break
            await asyncio.sleep(1)
        google_page = page
        emit({"type": "debug", "step": "same_page", "url": page.url[:80]})

    await asyncio.sleep(2)

    # ── Fill Google email ───────────────────────────────────────────
    emit({"type": "progress", "step": "email", "message": f"Filling email: {email}"})
    ok = await fill_google_email(google_page, email)
    if not ok:
        emit({"type": "result", "success": False, "error": "Failed to fill Google email", "email": email})
        return

    await asyncio.sleep(1)

    # ── Fill Google password ────────────────────────────────────────
    emit({"type": "progress", "step": "password", "message": "Filling password..."})
    ok = await fill_google_password(google_page, password)
    if not ok:
        emit({"type": "result", "success": False, "error": "Failed to fill Google password (wrong password or typing failed)", "email": email})
        return

    # ── Handle consent + redirect ───────────────────────────────────
    emit({"type": "progress", "step": "consent", "message": "Handling consent..."})
    landed = await handle_consent_and_redirect(page, "chat.b.ai", popup_page=popup_page)
    if not landed:
        emit({"type": "result", "success": False, "error": "Failed to redirect to chat.b.ai after consent", "email": email})
        return

    emit({"type": "progress", "step": "done", "message": "Logged in to chat.b.ai!"})

    # ── Reload page to ensure session is fully established ──────────
    try:
        await page.goto("https://chat.b.ai/chat", wait_until="load", timeout=30000)
    except Exception:
        pass
    # Wait for page to be fully interactive (JS loaded, session settled)
    try:
        await page.wait_for_function("() => document.readyState === 'complete'", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(5)

    # ── Claim signup bonus ─────────────────────────────────────────
    emit({"type": "progress", "step": "claim", "message": "Looking for claim bonus button..."})

    claimed = False
    for attempt in range(30):  # 30 seconds to find claim button
        try:
            # Priority 1: Find the big "Claim" button inside a modal/dialog (BAI Welcome Gift popup)
            clicked = await page.evaluate("""() => {
                // Look for modal/dialog first
                const modals = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="dialog"], [class*="popup"], [class*="overlay"]');
                for (const modal of modals) {
                    if (modal.offsetParent === null) continue;
                    // Find "Claim" button inside modal
                    for (const btn of modal.querySelectorAll('button, div[role="button"], a')) {
                        const txt = (btn.textContent || '').trim();
                        if (btn.offsetParent === null) continue;
                        if (txt === 'Claim' || txt.toLowerCase() === 'claim') {
                            btn.click(); return 'modal-claim';
                        }
                    }
                }
                // Priority 2: Any button with exact text "Claim" (not "Claim Free Credits")
                for (const btn of document.querySelectorAll('button, div[role="button"]')) {
                    const txt = (btn.textContent || '').trim();
                    if (btn.offsetParent === null) continue;
                    if (txt === 'Claim') {
                        btn.click(); return 'exact-claim';
                    }
                }
                // Priority 3: Button containing "500,000" or "Welcome Gift" nearby
                for (const btn of document.querySelectorAll('button, div[role="button"]')) {
                    const txt = (btn.textContent || '').trim().toLowerCase();
                    if (btn.offsetParent === null) continue;
                    if (txt === 'claim' && btn.closest('[class*="gift"], [class*="welcome"], [class*="bonus"]')) {
                        btn.click(); return 'gift-claim';
                    }
                }
                return null;
            }""")
            if clicked:
                emit({"type": "progress", "step": "claim", "message": f"Claim button: {clicked}"})
                claimed = True
                await asyncio.sleep(3)
                break
        except Exception:
            pass
        await asyncio.sleep(1)

    if not claimed:
        emit({"type": "debug", "step": "claim", "message": "No claim button found, checking if auto-claimed..."})

    # Wait for claim API call to complete
    await asyncio.sleep(3)

    # ── Extract session token + user info BEFORE navigating away ────
    session_token = ""
    user_id = ""
    try:
        cookies = await page.context.cookies(["https://chat.b.ai"])
        for cookie in cookies:
            if cookie.get("name") == "__Secure-authjs.session-token":
                session_token = cookie.get("value", "")
                break
    except Exception:
        pass
    try:
        user_id = await page.evaluate("""() => {
            const cookies = document.cookie;
            const match = cookies.match(/ainft_posthog_id=(user_[a-zA-Z0-9]+)/);
            return match ? match[1] : '';
        }""") or ""
    except Exception:
        pass
    emit({"type": "debug", "step": "extract", "message": f"session_token={'yes' if session_token else 'no'}, user_id={user_id or 'none'}"})

    # ── Create API key via tRPC ────────────────────────────────────────
    emit({"type": "progress", "step": "api_key", "message": "Creating API key..."})
    api_key = ""

    try:
        # Navigate to /key page first — this loads frontend JS that sets x-ainft-chat-auth
        await page.goto("https://chat.b.ai/key", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(5)  # Let frontend JS initialize and set auth headers

        import random
        _adj = ["fast", "main", "dev", "prod", "test", "local", "cloud", "app", "my", "lab"]
        _noun = ["server", "worker", "agent", "bot", "runner", "node", "service", "client", "hub", "api"]
        key_name = f"{random.choice(_adj)}-{random.choice(_noun)}-{random.randint(10, 99)}"

        # Create API key via page.evaluate fetch — browser context has all cookies + headers
        result = await page.evaluate("""async (keyName) => {
            try {
                const resp = await fetch('/trpc/lambda/apiKey.createApiKey?batch=1', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({"0": {"json": {"name": keyName}}})
                });
                const text = await resp.text();
                return { status: resp.status, body: text };
            } catch(e) {
                return { status: 0, body: String(e) };
            }
        }""", key_name)

        status = result.get("status", 0)
        body = result.get("body", "")
        emit({"type": "debug", "step": "api_key", "message": f"tRPC createApiKey status={status}, body={body[:200]}"})

        if status == 200 and body:
            import re
            match = re.search(r'sk-[a-zA-Z0-9]{20,}', body)
            if match:
                api_key = match.group(0)
                emit({"type": "progress", "step": "api_key", "message": f"API key created: {api_key[:20]}..."})

        # Fallback: check if key already exists on /key page
        if not api_key:
            try:
                await page.goto("https://chat.b.ai/key", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(3)
                api_key = await page.evaluate("""() => {
                    for (const el of document.querySelectorAll('input, code, pre, span, td, div, p')) {
                        const val = (el.value || el.textContent || '').trim();
                        if (val.startsWith('sk-') && val.length >= 20 && val.length <= 80 && !val.includes(' ')) return val;
                    }
                    return '';
                }""") or ""
                if api_key:
                    emit({"type": "progress", "step": "api_key", "message": f"API key from page: {api_key[:20]}..."})
            except Exception:
                pass

        if not api_key:
            emit({"type": "debug", "step": "api_key", "message": "Could not create/find API key"})
    except Exception as e:
        emit({"type": "debug", "step": "api_key", "message": f"API key error: {e}"})

    # session_token and user_id already extracted above (before API key step)

    # ── Emit result for batch runner ────────────────────────────────
    # If we got here, login was successful (failures return early above)
    # api_key is needed for proxy, session_token is backup
    success = bool(api_key or session_token)
    emit({
        "type": "result",
        "success": success,
        "email": email,
        "api_key": api_key,
        "session_token": session_token[:200] if session_token else "",
        "user_id": user_id,
        "claimed": claimed,
    })


def save_logs():
    output = {
        "captured_at": datetime.now().isoformat(),
        "http_requests": captured,
        "ws_frames": ws_frames,
    }
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    safe_print()
    safe_print(f"  Saved {len(captured)} HTTP + {len(ws_frames)} WS frames to {LOG_FILE}")

    safe_print()
    safe_print("  === SUMMARY (non-GET requests) ===")
    for entry in captured:
        if entry["method"] != "GET":
            safe_print(f"    {entry['method']} {entry['url'][:100]}")

    if ws_frames:
        safe_print()
        unique_ws = set(f["url"] for f in ws_frames)
        for ws_url in unique_ws:
            count = sum(1 for f in ws_frames if f["url"] == ws_url)
            safe_print(f"    WS {ws_url[:80]} ({count} frames)")
    safe_print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="chat.b.ai signup interceptor (Google OAuth)")
    parser.add_argument("--email", required=True, help="Google account email")
    parser.add_argument("--password", required=True, help="Google account password")
    parser.add_argument("--headless", action="store_true", default=False)
    parser.add_argument("--proxy", default="", help="Proxy URL (http:// or socks5://)")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.email, args.password, headless=args.headless, proxy_url=args.proxy))
    except KeyboardInterrupt:
        if captured:
            save_logs()
