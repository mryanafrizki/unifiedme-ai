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
    """Fill Google email step. Handles account picker too."""
    for _ in range(15):
        has_input = await page.evaluate("""() => {
            const el = document.querySelector('#identifierId');
            return el && el.offsetParent !== null;
        }""")
        if has_input:
            break

        # Check for "Use another account"
        clicked = await page.evaluate("""() => {
            for (const el of document.querySelectorAll('li, div[role="link"], div[data-identifier]')) {
                const txt = (el.textContent || '').toLowerCase();
                if (txt.includes('use another account') || txt.includes('gunakan akun lain')) {
                    el.click(); return true;
                }
            }
            return false;
        }""")
        if clicked:
            emit({"type": "debug", "step": "email", "message": "Clicked 'Use another account'"})
            await asyncio.sleep(2)
            continue

        # Already at password step?
        at_pw = await page.evaluate("""() => {
            const pw = document.querySelector('input[name="Passwd"]');
            return pw && pw.offsetParent !== null;
        }""")
        if at_pw:
            return True

        await asyncio.sleep(1)

    try:
        await page.wait_for_selector("#identifierId", state="visible", timeout=5000)
    except Exception:
        return False

    loc = page.locator("#identifierId").first
    await loc.click(force=True)
    await asyncio.sleep(0.3)
    await loc.press("Control+a")
    await loc.press("Backspace")
    await loc.press_sequentially(email, delay=50)
    await asyncio.sleep(0.5)

    clicked = await page.evaluate("""() => {
        const btn = document.querySelector('#identifierNext button');
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not clicked:
        await loc.press("Enter")

    try:
        await page.wait_for_function("""() => {
            const el = document.querySelector('#identifierId');
            if (!el || el.offsetParent === null) return true;
            const pw = document.querySelector('input[name="Passwd"]');
            if (pw && pw.offsetParent !== null) return true;
            return false;
        }""", timeout=10000)
    except Exception:
        pass
    return True


async def fill_google_password(page, password: str) -> bool:
    """Fill Google password step."""
    try:
        await page.wait_for_selector('input[name="Passwd"]', state="visible", timeout=10000)
    except Exception:
        return False

    loc = page.locator('input[name="Passwd"]').first
    await loc.click(force=True)
    await asyncio.sleep(0.3)
    await loc.press("Control+a")
    await loc.press("Backspace")
    await loc.press_sequentially(password, delay=60)
    await asyncio.sleep(0.5)

    clicked = await page.evaluate("""() => {
        const btn = document.querySelector('#passwordNext button');
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not clicked:
        await loc.press("Enter")

    for _ in range(20):
        await asyncio.sleep(1)
        try:
            url = page.url
            if "accounts.google.com" not in url:
                return True
            pw_visible = await page.evaluate("""() => {
                const pw = document.querySelector('input[name="Passwd"]');
                return pw && pw.offsetParent !== null;
            }""")
            if not pw_visible:
                return True
            has_error = await page.evaluate("""() => {
                const text = (document.body?.innerText || '').toLowerCase();
                return text.includes('wrong password') || text.includes('incorrect');
            }""")
            if has_error:
                emit({"type": "error", "step": "password", "message": "Wrong password"})
                return False
        except Exception:
            pass
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

    proxy_msg = f" via {proxy_url}" if proxy_url else ""
    print()
    print("=" * 64)
    print("  chat.b.ai Signup Interceptor (Google OAuth)")
    print("=" * 64)
    print()
    print(f"  Email  : {email}")
    print(f"  Proxy  : {proxy_url or 'none'}")
    print(f"  Mode   : {'headless' if headless else 'visible'}")
    print()
    print("  All API calls will be captured.")
    print("  Press Ctrl+C when done to save the log.")
    print("=" * 64)
    print()

    # ── Launch Camoufox ─────────────────────────────────────────────
    proxy_cfg = {"server": proxy_url} if proxy_url else None

    manager = AsyncCamoufox(
        headless=headless,
        os="windows",
        block_webrtc=True,
        humanize=False,
        screen=Screen(max_width=1920, max_height=1080),
        proxy=proxy_cfg,
    )
    browser = await manager.__aenter__()
    page = await browser.new_page()
    page.set_default_timeout(30000)

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
        print(format_entry(entry))
        print()

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
            print(f"  [WS->] {ws_url[:60]} : {str(payload)[:200]}")

        def on_frame_received(payload):
            ws_frames.append({
                "timestamp": datetime.now().isoformat(),
                "direction": "received", "url": ws_url,
                "payload": str(payload)[:2000],
            })
            print(f"  [WS<-] {ws_url[:60]} : {str(payload)[:200]}")

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

    # Listen for popup (Google OAuth may open in popup)
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
        emit({"type": "error", "step": "email", "message": "Failed to fill email"})
        print("\n  ⚠ Email step failed. Complete login manually in the browser.")
    else:
        await asyncio.sleep(1)

        # ── Fill Google password ────────────────────────────────────
        emit({"type": "progress", "step": "password", "message": "Filling password..."})
        ok = await fill_google_password(google_page, password)
        if not ok:
            emit({"type": "error", "step": "password", "message": "Failed to fill password"})
            print("\n  ⚠ Password step failed. Complete login manually in the browser.")
        else:
            # ── Handle consent + redirect ───────────────────────────
            emit({"type": "progress", "step": "consent", "message": "Handling consent..."})
            landed = await handle_consent_and_redirect(page, "chat.b.ai", popup_page=popup_page)
            if landed:
                emit({"type": "progress", "step": "done", "message": "Logged in to chat.b.ai!"})
            else:
                emit({"type": "debug", "step": "redirect_timeout", "url": page.url[:80]})
                print("\n  ⚠ Redirect timeout. Check browser manually.")

    # ── Claim signup bonus ─────────────────────────────────────────
    await asyncio.sleep(5)  # Wait for claim popup to appear
    emit({"type": "progress", "step": "claim", "message": "Looking for claim bonus button..."})

    claimed = False
    for attempt in range(20):
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
        # Fallback: check if already claimed via tRPC
        emit({"type": "debug", "step": "claim", "message": "No claim button found, checking if auto-claimed..."})

    # Wait for claim API call to complete
    await asyncio.sleep(3)

    # ── Extract account info ────────────────────────────────────────
    emit({"type": "progress", "step": "extract", "message": "Extracting account info..."})
    try:
        info = await page.evaluate("""() => {
            const result = {};
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                const val = localStorage.getItem(key);
                if (key.toLowerCase().includes('token') || key.toLowerCase().includes('auth')
                    || key.toLowerCase().includes('session') || key.toLowerCase().includes('user')) {
                    result[key] = val ? val.substring(0, 500) : '';
                }
            }
            result['_cookies'] = document.cookie.substring(0, 1000);
            result['_url'] = window.location.href;
            return result;
        }""")
        emit({"type": "account_info", "data": info})
    except Exception:
        pass

    # ── Keep alive until Ctrl+C ─────────────────────────────────────
    print()
    print("=" * 64)
    print("  Interceptor running. All traffic is being captured.")
    print("  Press Ctrl+C to save and exit.")
    print("=" * 64)
    print()

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    # ── Save logs ───────────────────────────────────────────────────
    save_logs()

    try:
        await manager.__aexit__(None, None, None)
    except Exception:
        pass


def save_logs():
    output = {
        "captured_at": datetime.now().isoformat(),
        "http_requests": captured,
        "ws_frames": ws_frames,
    }
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print()
    print(f"  Saved {len(captured)} HTTP + {len(ws_frames)} WS frames to {LOG_FILE}")

    print()
    print("  === SUMMARY (non-GET requests) ===")
    for entry in captured:
        if entry["method"] != "GET":
            print(f"    {entry['method']} {entry['url'][:100]}")

    if ws_frames:
        print()
        unique_ws = set(f["url"] for f in ws_frames)
        for ws_url in unique_ws:
            count = sum(1 for f in ws_frames if f["url"] == ws_url)
            print(f"    WS {ws_url[:80]} ({count} frames)")
    print()


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
