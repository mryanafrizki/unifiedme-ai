#!/usr/bin/env python3
"""
ChatBAI (chat.b.ai) signup via Google OAuth using Camoufox.
Registers account, claims signup bonus, creates API key.
Outputs JSON result to stdout (batch_runner compatible).

Usage:
    python register.py --email user@gmail.com --password pass123
    python register.py --email user@gmail.com --password pass123 --headless
    python register.py --email user@gmail.com --password pass123 --proxy socks5://user:pass@host:port
"""

import argparse
import asyncio
import json
import os
import socket
import sys

# ── Fix Windows socketpair broken by proxy apps ────────────────────
if sys.platform == "win32":
    _orig_socketpair = socket.socketpair

    def _patched_socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
        try:
            return _orig_socketpair(family, type, proto)
        except (ConnectionError, OSError):
            pass
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


def emit(data: dict):
    try:
        print(json.dumps(data, ensure_ascii=True), flush=True)
    except Exception:
        pass


# ── Google OAuth helpers (same pattern as gumloop_login.py) ─────────

async def fill_google_email(page, email: str) -> bool:
    try:
        await page.wait_for_selector("#identifierId", state="visible", timeout=30000)
        locator = page.locator("#identifierId").first
        await locator.click(force=True)
        await asyncio.sleep(0.2)
        await locator.press("Control+a")
        await locator.press("Backspace")
        await locator.press_sequentially(email, delay=60)
        await asyncio.sleep(0.5)
        await page.evaluate("""() => {
            const byId = document.querySelector('#identifierNext button');
            if (byId && byId.offsetParent !== null) { byId.click(); return; }
            for (const btn of document.querySelectorAll('button, div[role="button"]')) {
                if ((btn.textContent||'').trim() === 'Next' && btn.offsetParent !== null) { btn.click(); return; }
            }
        }""")
        await asyncio.sleep(2)
        return True
    except Exception as exc:
        emit({"type": "debug", "step": "email", "message": f"error: {exc}"})
        return False


async def fill_google_password(page, password: str) -> bool:
    try:
        await page.wait_for_selector('input[name="Passwd"]', state="visible", timeout=30000)
        locator = page.locator('input[name="Passwd"]').first
        await locator.click(force=True)
        await asyncio.sleep(0.2)
        await locator.press("Control+a")
        await locator.press("Backspace")
        await locator.press_sequentially(password, delay=70)
        await asyncio.sleep(0.5)

        url_before = page.url
        await locator.press("Enter")

        for _ in range(15):
            await asyncio.sleep(1)
            try:
                if page.url != url_before:
                    return True
                has_error = await page.evaluate("""() => {
                    const err = document.querySelector('.LXRPh');
                    return err && err.offsetParent !== null ? err.textContent : null;
                }""")
                if has_error:
                    emit({"type": "error", "step": "password", "message": f"Google: {has_error}"})
                    return False
            except Exception:
                pass
        return True
    except Exception as exc:
        emit({"type": "debug", "step": "password", "message": f"error: {exc}"})
        return False


async def handle_consent_and_redirect(google_page, main_page) -> bool:
    """Handle consent screens. Same pattern as gumloop_login.py."""
    clicked_consent = False
    failed_clicks = 0

    for tick in range(60):
        await asyncio.sleep(1)
        try:
            gurl = ""
            popup_closed = False
            try:
                popup_closed = google_page.is_closed()
                if not popup_closed:
                    gurl = google_page.url
            except Exception:
                popup_closed = True

            main_url = ""
            try:
                main_url = main_page.url if not main_page.is_closed() else ""
            except Exception:
                pass

            if failed_clicks >= 15:
                return False

            # Gaplustos on popup
            if not popup_closed and "/speedbump/gaplustos" in gurl:
                await google_page.evaluate("""() => {
                    const el = document.querySelector('#confirm') || document.querySelector('input[type="submit"]');
                    if (el) { el.click(); return; }
                    for (const btn of document.querySelectorAll('button')) {
                        const t = (btn.textContent||'').toLowerCase();
                        if ((t==='continue'||t==='i agree'||t.includes('accept')) && btn.offsetParent!==null) { btn.click(); return; }
                    }
                }""")
                await asyncio.sleep(3)
                continue

            # Consent on popup
            if not popup_closed and "accounts.google.com" in gurl and not clicked_consent:
                result = await google_page.evaluate("""() => {
                    for (const btn of document.querySelectorAll('button, div[role="button"], a[role="button"]')) {
                        const t = (btn.textContent||'').trim().toLowerCase();
                        if (!t || btn.offsetParent === null) continue;
                        if (t === 'continue' || t === 'allow' || t === 'lanjutkan' || t.includes('continue') || t.includes('allow')) {
                            btn.click(); return 'clicked:' + t;
                        }
                    }
                    return '';
                }""")
                if result and result.startswith("clicked:"):
                    clicked_consent = True
                    failed_clicks = 0
                    emit({"type": "progress", "step": "consent", "message": result})
                    await asyncio.sleep(3)
                    continue
                else:
                    failed_clicks += 1

            # Consent on main page
            if "accounts.google.com" in main_url and not clicked_consent:
                result = await main_page.evaluate("""() => {
                    for (const btn of document.querySelectorAll('button, div[role="button"]')) {
                        const t = (btn.textContent||'').trim().toLowerCase();
                        if (!t || btn.offsetParent === null) continue;
                        if (t === 'continue' || t === 'allow' || t.includes('continue')) {
                            btn.click(); return 'clicked:' + t;
                        }
                    }
                    return '';
                }""")
                if result and result.startswith("clicked:"):
                    clicked_consent = True
                    failed_clicks = 0
                    await asyncio.sleep(3)
                    continue
                else:
                    failed_clicks += 1

            # Check redirect — only after consent clicked or popup closed
            if clicked_consent or popup_closed:
                if "chat.b.ai" in main_url and "accounts.google.com" not in main_url:
                    # Verify session is actually established
                    try:
                        session = await main_page.evaluate("""async () => {
                            try {
                                const r = await fetch('/api/auth/session', {credentials:'include'});
                                const d = await r.json();
                                return d && d.user ? d.user.id : null;
                            } catch(e) { return null; }
                        }""")
                        if session:
                            emit({"type": "progress", "step": "redirect", "message": f"Logged in: {session}"})
                            return True
                    except Exception:
                        pass

        except Exception:
            pass

    return False


async def run_login(email: str, password: str, proxy_url: str = "") -> dict:
    from browserforge.fingerprints import Screen
    from camoufox.async_api import AsyncCamoufox

    headless = os.getenv("BATCHER_CAMOUFOX_HEADLESS", "false").lower() == "true"

    emit({"type": "progress", "step": "init", "message": f"Starting ChatBAI signup for {email}..."})

    # Parse proxy — Firefox doesn't support socks5 auth, convert to HTTP
    proxy_cfg = None
    if proxy_url:
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)
        proxy_user = parsed.username or ""
        proxy_pass = parsed.password or ""
        scheme = "http" if parsed.scheme.startswith("socks") else parsed.scheme
        server = f"{scheme}://{parsed.hostname}:{parsed.port}"
        proxy_cfg = {"server": server}
        if proxy_user:
            proxy_cfg["username"] = proxy_user
        if proxy_pass:
            proxy_cfg["password"] = proxy_pass

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

    try:
        # Step 1: Navigate to chat.b.ai — wait for full load
        emit({"type": "progress", "step": "navigate", "message": "Opening chat.b.ai..."})
        await page.goto("https://chat.b.ai/chat", wait_until="load", timeout=60000)

        # Step 2: Wait for "Log in" button to appear, then click
        emit({"type": "progress", "step": "login", "message": "Waiting for Log in button..."})
        try:
            await page.wait_for_function("""() => {
                for (const b of document.querySelectorAll('button')) {
                    if (b.textContent.trim() === 'Log in' && b.offsetParent !== null) return true;
                }
                return false;
            }""", timeout=30000)
        except Exception:
            pass
        await asyncio.sleep(1)

        clicked = await page.evaluate("""() => {
            for (const b of document.querySelectorAll('button')) {
                if (b.textContent.trim() === 'Log in' && b.offsetParent !== null) { b.click(); return true; }
            }
            return false;
        }""")
        if not clicked:
            return {"success": False, "error": "Log in button not found"}
        emit({"type": "progress", "step": "login", "message": "Clicked Log in"})

        # Step 3: Wait for "Continue with Google" button, then click (opens popup)
        try:
            await page.wait_for_function("""() => {
                for (const b of document.querySelectorAll('button, a, div[role="button"]')) {
                    if ((b.textContent||'').toLowerCase().includes('continue with google') && b.offsetParent !== null) return true;
                }
                return false;
            }""", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(1)

        emit({"type": "progress", "step": "google", "message": "Clicking Continue with Google..."})

        popup_page = None
        popup_future = asyncio.get_event_loop().create_future()

        def on_popup(p):
            nonlocal popup_page
            popup_page = p
            if not popup_future.done():
                popup_future.set_result(p)

        page.context.on("page", on_popup)

        await page.evaluate("""() => {
            for (const b of document.querySelectorAll('button, a, div[role="button"]')) {
                if ((b.textContent||'').toLowerCase().includes('continue with google') && b.offsetParent !== null) {
                    b.click(); return true;
                }
            }
            return false;
        }""")

        try:
            await asyncio.wait_for(popup_future, timeout=8)
        except asyncio.TimeoutError:
            pass

        if popup_page:
            await popup_page.wait_for_load_state("domcontentloaded", timeout=15000)
            google_page = popup_page
        else:
            for _ in range(10):
                if "accounts.google.com" in page.url:
                    break
                await asyncio.sleep(1)
            google_page = page

        await asyncio.sleep(2)

        # Step 4: Fill email
        emit({"type": "progress", "step": "email", "message": f"Filling email: {email}"})
        ok = await fill_google_email(google_page, email)
        if not ok:
            return {"success": False, "error": "Failed to fill Google email"}

        # Step 5: Fill password
        emit({"type": "progress", "step": "password", "message": "Filling password..."})
        ok = await fill_google_password(google_page, password)
        if not ok:
            return {"success": False, "error": "Failed to fill Google password"}

        # Step 6: Handle consent + wait for redirect
        emit({"type": "progress", "step": "consent", "message": "Handling consent..."})
        redirected = await handle_consent_and_redirect(google_page, page)
        if not redirected:
            if "chat.b.ai" not in page.url:
                return {"success": False, "error": "Failed to redirect to chat.b.ai"}

        await asyncio.sleep(3)

        # Step 7: Extract session token
        emit({"type": "progress", "step": "session", "message": "Extracting session..."})
        session_token = ""
        try:
            cookies = await page.context.cookies(["https://chat.b.ai"])
            for cookie in cookies:
                if cookie.get("name") == "__Secure-authjs.session-token":
                    session_token = cookie.get("value", "")
                    break
        except Exception:
            pass

        user_id = ""
        try:
            user_id = await page.evaluate("""() => {
                const m = document.cookie.match(/ainft_posthog_id=(user_[a-zA-Z0-9]+)/);
                return m ? m[1] : '';
            }""") or ""
        except Exception:
            pass

        # Step 8: Click claim bonus (if popup appears)
        emit({"type": "progress", "step": "claim", "message": "Looking for claim button..."})
        claimed = False
        for _ in range(15):
            try:
                clicked = await page.evaluate("""() => {
                    // Modal claim button
                    for (const modal of document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="popup"]')) {
                        if (modal.offsetParent === null) continue;
                        for (const btn of modal.querySelectorAll('button, div[role="button"]')) {
                            if (btn.offsetParent === null) continue;
                            if ((btn.textContent||'').trim() === 'Claim') { btn.click(); return true; }
                        }
                    }
                    // Any exact "Claim" button
                    for (const btn of document.querySelectorAll('button')) {
                        if (btn.offsetParent === null) continue;
                        if ((btn.textContent||'').trim() === 'Claim') { btn.click(); return true; }
                    }
                    return false;
                }""")
                if clicked:
                    emit({"type": "progress", "step": "claim", "message": "Claimed!"})
                    claimed = True
                    await asyncio.sleep(2)
                    break
            except Exception:
                pass
            await asyncio.sleep(1)

        # Step 9: Create API key via tRPC
        emit({"type": "progress", "step": "api_key", "message": "Creating API key..."})
        api_key = ""

        # Navigate to /key page first (loads auth context)
        try:
            await page.goto("https://chat.b.ai/key", wait_until="load", timeout=20000)
            await asyncio.sleep(3)
        except Exception:
            pass

        import random
        key_name = f"{random.choice(['fast','main','dev','prod','test'])}-{random.choice(['server','worker','agent','bot','api'])}-{random.randint(10,99)}"

        try:
            result = await page.evaluate("""async (name) => {
                try {
                    const r = await fetch('/trpc/lambda/apiKey.createApiKey?batch=1', {
                        method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({"0":{"json":{"name": name}}})
                    });
                    return await r.text();
                } catch(e) { return ''; }
            }""", key_name)
            import re
            match = re.search(r'sk-[a-zA-Z0-9]{20,}', result or "")
            if match:
                api_key = match.group(0)
                emit({"type": "progress", "step": "api_key", "message": f"API key: {api_key[:20]}..."})
        except Exception:
            pass

        # Fallback: check if key already exists on page
        if not api_key:
            try:
                api_key = await page.evaluate("""() => {
                    for (const el of document.querySelectorAll('input, code, pre, span, td, div, p')) {
                        const v = (el.value || el.textContent || '').trim();
                        if (v.startsWith('sk-') && v.length >= 20 && v.length <= 80 && !v.includes(' ')) return v;
                    }
                    return '';
                }""") or ""
            except Exception:
                pass

        if not api_key:
            emit({"type": "debug", "step": "api_key", "message": "Could not create API key"})

        return {
            "success": bool(api_key or session_token),
            "email": email,
            "api_key": api_key,
            "session_token": session_token[:200] if session_token else "",
            "user_id": user_id,
            "claimed": claimed,
        }

    except Exception as exc:
        return {"success": False, "error": str(exc)}
    finally:
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            pass


async def main(email: str, password: str, proxy_url: str = ""):
    result = await run_login(email, password, proxy_url)
    emit({"type": "result", **result})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--headless", action="store_true", default=False)
    parser.add_argument("--proxy", default="")
    args = parser.parse_args()

    if args.headless:
        os.environ["BATCHER_CAMOUFOX_HEADLESS"] = "true"

    asyncio.run(main(args.email, args.password, args.proxy))
