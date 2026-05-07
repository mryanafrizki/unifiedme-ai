"""Windsurf account login — email+password (Auth1 HTTP) + Google OAuth (Camoufox).

Google OAuth flow (from HAR intercept):
  1. Open windsurf.com/account/login → click "Continue with Google"
  2. Google OAuth → redirect to windsurf.com/auth/callback?code=xxx
  3. POST /_devin-auth/google/exchange {code, redirect_uri, code_verifier, ...}
     → {token: "auth1_xxx", email, user_id}
  4. POST /_backend/.../WindsurfPostAuth  (header: x-devin-auth1-token)
     → {sessionToken: "devin-session-token$xxx"}  ← API KEY

Usage:
  python -m unified.windsurf_login --email user@gmail.com --password pass123
  python -m unified.windsurf_login --email user@gmail.com --password pass123 --google
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

import httpx

log = logging.getLogger("unified.windsurf_login")

# ─── Constants ───────────────────────────────────────────────────────────────

WINDSURF_BASE = "https://windsurf.com"
CHECK_LOGIN_METHOD_URL = f"{WINDSURF_BASE}/_backend/exa.api_server_pb.ApiServerService/CheckUserLoginMethod"
AUTH1_LOGIN_URL = f"{WINDSURF_BASE}/_devin-auth/password/login"
POST_AUTH_URL = f"{WINDSURF_BASE}/_backend/exa.seat_management_pb.SeatManagementService/WindsurfPostAuth"
GOOGLE_EXCHANGE_URL = f"{WINDSURF_BASE}/_devin-auth/google/exchange"
WINDSURF_LOGIN_PAGE = f"{WINDSURF_BASE}/account/login"
WINDSURF_AUTH_CALLBACK = f"{WINDSURF_BASE}/auth/callback"

DATA_DIR = Path(__file__).resolve().parent / "data"
HAR_FILE = DATA_DIR / "windsurf_login_har.json"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── Helpers ─────────────────────────────────────────────────────────────────

def emit(data: dict):
    """Output JSON line for batch_runner integration."""
    try:
        print(json.dumps(data, ensure_ascii=False), flush=True)
    except UnicodeEncodeError:
        print(json.dumps(data, ensure_ascii=True), flush=True)


def _generate_fingerprint() -> str:
    return hashlib.sha256(os.urandom(32)).hexdigest()


def _random_user_agent() -> str:
    versions = ["124.0.6367.91", "125.0.6422.60", "126.0.6478.55", "127.0.6533.72"]
    v = random.choice(versions)
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36"


# ─── Method 1: Email + Password (Auth1 path, pure HTTP) ─────────────────────

async def windsurf_login_password(
    email: str,
    password: str,
    proxy: str | None = None,
) -> dict:
    """Login to Windsurf via email+password (Auth1 path). Pure HTTP, no browser."""
    fingerprint = _generate_fingerprint()
    ua = _random_user_agent()

    base_headers = {
        "User-Agent": ua,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": WINDSURF_BASE,
        "Referer": f"{WINDSURF_BASE}/account/login",
    }

    try:
        async with httpx.AsyncClient(timeout=30, proxy=proxy, follow_redirects=True) as client:
            # Step 1: Check login method
            emit({"type": "progress", "step": "check_method", "message": f"Checking login method for {email}"})
            resp = await client.post(
                CHECK_LOGIN_METHOD_URL,
                json={"email": email},
                headers={**base_headers, "Connect-Protocol-Version": "1"},
            )
            auth_method = "auth1"
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    method = data.get("method") or data.get("loginMethod") or ""
                    if "firebase" in str(method).lower():
                        auth_method = "firebase"
                except (json.JSONDecodeError, ValueError):
                    pass

            # Step 2: Auth1 password login
            emit({"type": "progress", "step": "auth1_login", "message": "Authenticating..."})
            resp = await client.post(AUTH1_LOGIN_URL, json={"email": email, "password": password}, headers=base_headers)

            if resp.status_code != 200:
                if resp.status_code == 401:
                    return {"success": False, "error": f"Wrong password for {email}"}
                if resp.status_code == 429:
                    return {"success": False, "error": "Rate limited — try again later"}
                if resp.status_code == 403:
                    return {"success": False, "error": "Account locked or banned"}
                return {"success": False, "error": f"Auth1 login failed: HTTP {resp.status_code} — {resp.text[:200]}"}

            auth1_data = resp.json()
            auth1_token = auth1_data.get("token") or auth1_data.get("access_token") or ""
            if not auth1_token:
                return {"success": False, "error": f"Auth1 returned no token: {json.dumps(auth1_data)[:200]}"}

            # Step 3: PostAuth → sessionToken
            emit({"type": "progress", "step": "post_auth", "message": "Exchanging for session token..."})
            resp = await client.post(
                POST_AUTH_URL, json={},
                headers={**base_headers, "Connect-Protocol-Version": "1", "x-devin-auth1-token": auth1_token},
            )

            if resp.status_code != 200:
                return {"success": False, "error": f"PostAuth failed: HTTP {resp.status_code} — {resp.text[:200]}"}

            post_auth_data = resp.json()
            session_token = (
                post_auth_data.get("sessionToken") or post_auth_data.get("session_token")
                or post_auth_data.get("apiKey") or post_auth_data.get("api_key") or ""
            )
            if not session_token:
                return {"success": False, "error": f"PostAuth returned no sessionToken: {json.dumps(post_auth_data)[:200]}"}

            emit({"type": "progress", "step": "done", "message": "Login OK"})
            return {"success": True, "api_key": session_token, "email": email, "auth_method": auth_method}

    except httpx.TimeoutException:
        return {"success": False, "error": "Timeout connecting to windsurf.com"}
    except httpx.ConnectError as e:
        return {"success": False, "error": f"Cannot connect to windsurf.com: {e}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {e}"}


# ─── Google OAuth helpers (same pattern as wavespeed/register.py) ────────────

async def _fill_google_email(page, email: str) -> bool:
    """Fill Google email step. Handles 'Choose an account' screens."""
    try:
        for _ in range(15):
            has_input = await page.evaluate("() => { const el = document.querySelector('#identifierId'); return el && el.offsetParent !== null; }")
            if has_input:
                break
            # Click "Use another account" if present
            await page.evaluate("""() => {
                for (const el of document.querySelectorAll('li, div[role="link"], div[data-identifier]')) {
                    const txt = (el.textContent || '').toLowerCase();
                    if (txt.includes('use another account') || txt.includes('gunakan akun lain')) { el.click(); return; }
                }
            }""")
            # Check if already at password step
            at_pw = await page.evaluate("() => { const pw = document.querySelector('input[name=\"Passwd\"]'); return pw && pw.offsetParent !== null; }")
            if at_pw:
                return True
            await asyncio.sleep(1)

        try:
            await page.wait_for_selector("#identifierId", state="visible", timeout=5000)
        except Exception:
            has_alt = await page.evaluate("() => { const el = document.querySelector('input[type=\"email\"]'); return el && el.offsetParent !== null; }")
            if not has_alt:
                return False

        loc = page.locator("#identifierId, input[type='email']").first
        await loc.click(force=True)
        await asyncio.sleep(0.3)
        await loc.press("Control+a")
        await loc.press("Backspace")
        await loc.press_sequentially(email, delay=50)
        await asyncio.sleep(0.5)

        clicked = await page.evaluate("() => { const b = document.querySelector('#identifierNext button'); if (b) { b.click(); return true; } return false; }")
        if not clicked:
            await loc.press("Enter")

        await page.wait_for_function("""() => {
            const el = document.querySelector('#identifierId');
            if (!el || el.offsetParent === null) return true;
            const pw = document.querySelector('input[name="Passwd"]');
            if (pw && pw.offsetParent !== null) return true;
            return false;
        }""", timeout=10000)
        return True
    except Exception as e:
        emit({"type": "error", "step": "email", "message": str(e)})
        return False


async def _fill_google_password(page, password: str) -> bool:
    """Fill Google password step."""
    try:
        await page.wait_for_selector('input[name="Passwd"]', state="visible", timeout=10000)
        loc = page.locator('input[name="Passwd"]').first
        await loc.click(force=True)
        await asyncio.sleep(0.3)
        await loc.press("Control+a")
        await loc.press("Backspace")
        await loc.press_sequentially(password, delay=60)
        await asyncio.sleep(0.5)

        clicked = await page.evaluate("() => { const b = document.querySelector('#passwordNext button'); if (b) { b.click(); return true; } return false; }")
        if not clicked:
            await loc.press("Enter")

        for _ in range(20):
            await asyncio.sleep(1)
            try:
                url = page.url
                if "accounts.google.com" not in url:
                    return True
                pw_visible = await page.evaluate("() => { const pw = document.querySelector('input[name=\"Passwd\"]'); return pw && pw.offsetParent !== null; }")
                if not pw_visible:
                    return True
                has_error = await page.evaluate("() => { const t = (document.body?.innerText || '').toLowerCase(); return t.includes('wrong password') || t.includes('incorrect'); }")
                if has_error:
                    emit({"type": "error", "step": "password", "message": "Wrong password detected"})
                    return False
            except Exception:
                pass
        return True
    except Exception as e:
        emit({"type": "error", "step": "password", "message": str(e)})
        return False


async def _click_google_signin(page) -> bool:
    """Find and click Google sign-in button on Windsurf login page."""
    for attempt in range(15):
        try:
            if "accounts.google.com" in page.url:
                return True

            clicked = await page.evaluate("""() => {
                const selectors = [
                    'button:has(img[alt*="Google"])',
                    'button:has(svg[data-testid="GoogleIcon"])',
                    '[data-provider="google"]',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) { el.click(); return 'selector:' + sel; }
                }
                for (const el of document.querySelectorAll('button, a, div[role="button"]')) {
                    const txt = (el.textContent || '').toLowerCase();
                    if ((txt.includes('google') || txt.includes('continue with google') || txt.includes('sign in with google')) && el.offsetParent !== null) {
                        el.click(); return 'text:' + txt.trim().substring(0, 40);
                    }
                }
                return null;
            }""")

            if clicked:
                emit({"type": "progress", "step": "google_click", "message": f"Clicked: {clicked}"})
                await asyncio.sleep(3)
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


# ─── Method 2: Google OAuth (Camoufox browser automation) ────────────────────

async def windsurf_login_google(
    email: str,
    password: str,
    proxy: str | None = None,
    headless: bool = True,
) -> dict:
    """Login to Windsurf via Google OAuth using Camoufox.

    Flow (from HAR intercept):
      1. Open windsurf.com/account/login → click Google
      2. Google OAuth → redirect to windsurf.com/auth/callback?code=xxx
      3. Browser auto-POSTs /_devin-auth/google/exchange → auth1_token
      4. Browser auto-POSTs WindsurfPostAuth → sessionToken (API key)
      5. We intercept the sessionToken from network response
    """
    try:
        from app.browser import create_stealth_browser
    except ImportError:
        return {"success": False, "error": "app.browser not available — check camoufox installation"}

    captured_token = None
    captured_email = None
    har_entries = []

    proxy_cfg = None
    if proxy:
        from urllib.parse import urlparse
        parsed = urlparse(proxy)
        if parsed.username:
            proxy_cfg = {
                "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
                "username": parsed.username,
                "password": parsed.password or "",
            }
        else:
            proxy_cfg = {"server": proxy}
    proxy_msg = f" via proxy {proxy[:50]}..." if proxy else ""

    emit({"type": "progress", "step": "init", "message": f"Launching Camoufox ({'headless' if headless else 'visible'}){proxy_msg}..."})

    try:
        manager, browser, page = await create_stealth_browser(
            proxy=proxy_cfg,
            headless=headless,
            timeout=30000,
            humanize=True,
        )
    except Exception as e:
        return {"success": False, "error": f"Failed to launch browser: {e}"}

    try:
        # Intercept network — capture auth tokens from responses
        async def on_response(response):
            nonlocal captured_token, captured_email
            url = response.url

            # Capture /_devin-auth/google/exchange → auth1 token + email
            if "/_devin-auth/google/exchange" in url:
                try:
                    body = await response.text()
                    har_entries.append({"url": url, "status": response.status, "body": body[:3000], "ts": time.strftime("%H:%M:%S")})
                    data = json.loads(body)
                    captured_email = data.get("email", "")
                    emit({"type": "progress", "step": "google_exchange", "message": f"Google exchange OK — {captured_email}"})
                except Exception:
                    pass

            # Capture WindsurfPostAuth → sessionToken (THE API KEY)
            if "WindsurfPostAuth" in url:
                try:
                    body = await response.text()
                    har_entries.append({"url": url, "status": response.status, "body": body[:3000], "ts": time.strftime("%H:%M:%S")})
                    data = json.loads(body)
                    token = data.get("sessionToken") or data.get("session_token") or ""
                    if token and len(token) > 20:
                        captured_token = token
                        emit({"type": "progress", "step": "post_auth", "message": f"Got sessionToken (API key)"})
                except Exception:
                    pass

            # Also log other auth-related requests
            if any(k in url for k in ["_devin-auth", "register_user", "codeium.com"]):
                try:
                    body = await response.text()
                    har_entries.append({"url": url, "status": response.status, "body": body[:2000], "ts": time.strftime("%H:%M:%S")})
                except Exception:
                    pass

        page.on("response", on_response)

        # Listen for popup (Google OAuth may open in popup)
        popup_page = None
        def on_popup(p):
            nonlocal popup_page
            popup_page = p
        page.context.on("page", on_popup)

        # Step 1: Navigate to login page
        emit({"type": "progress", "step": "navigate", "message": "Opening windsurf.com/account/login..."})
        await page.goto(WINDSURF_LOGIN_PAGE, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Step 2: Click Google sign-in
        emit({"type": "progress", "step": "google_click", "message": "Looking for Google login button..."})
        clicked = await _click_google_signin(page)
        if not clicked:
            return {"success": False, "error": "Could not find Google login button on windsurf.com"}

        await asyncio.sleep(5)

        # Handle popup vs same-page navigation
        if popup_page:
            emit({"type": "debug", "step": "popup", "message": f"Google popup: {popup_page.url[:80]}"})
            try:
                await popup_page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            google_page = popup_page
        else:
            for _ in range(15):
                if "accounts.google.com" in page.url:
                    break
                await asyncio.sleep(1)
            google_page = page
            emit({"type": "debug", "step": "same_page_google", "message": f"Google page: {page.url[:80]}"})

        await asyncio.sleep(2)

        # Step 3: Fill Google email
        emit({"type": "progress", "step": "email", "message": f"Filling email: {email}"})
        ok = await _fill_google_email(google_page, email)
        if not ok:
            return {"success": False, "error": "Failed to fill Google email"}

        await asyncio.sleep(1)

        # Step 4: Fill Google password
        emit({"type": "progress", "step": "password", "message": "Filling password..."})
        ok = await _fill_google_password(google_page, password)
        if not ok:
            return {"success": False, "error": "Failed to fill Google password (2FA may be required)"}

        # Step 5: Handle consent screens + wait for redirect to windsurf.com
        emit({"type": "progress", "step": "consent", "message": "Handling consent + waiting for redirect..."})
        clicked_consent: set = set()
        landed = False

        for tick in range(60):
            await asyncio.sleep(1)

            # If we already captured the token, we're done
            if captured_token:
                landed = True
                break

            try:
                url = page.url

                # Gaplustos (Terms of Service)
                if "/speedbump/gaplustos" in url:
                    if "gaplustos" not in clicked_consent:
                        await page.evaluate("""() => {
                            let el = document.querySelector('#confirm') || document.querySelector('input[type="submit"]');
                            if (el) { el.click(); return; }
                            for (const btn of document.querySelectorAll('button, div[role="button"]')) {
                                const txt = (btn.textContent || '').trim().toLowerCase();
                                if ((txt === 'continue' || txt === 'i agree' || txt.includes('accept') || txt.includes('lanjut')) && btn.offsetParent !== null) {
                                    btn.click(); return;
                                }
                            }
                        }""")
                        clicked_consent.add("gaplustos")
                        emit({"type": "progress", "step": "consent", "message": "Clicked gaplustos confirm"})
                    await asyncio.sleep(5)
                    continue

                # OAuth consent (Continue/Allow)
                if "accounts.google.com" in url:
                    if "oauth_consent" not in clicked_consent:
                        await page.evaluate("""() => {
                            const kw = ['continue', 'allow', 'lanjut', 'i understand', 'accept', 'agree', 'got it', 'next'];
                            for (const btn of document.querySelectorAll('button, div[role="button"], input[type="submit"]')) {
                                const txt = (btn.textContent || btn.value || '').trim().toLowerCase();
                                if (kw.some(k => txt.includes(k)) && btn.offsetParent !== null) { btn.click(); return; }
                            }
                        }""")
                        clicked_consent.add("oauth_consent")
                        emit({"type": "progress", "step": "consent", "message": "Clicked consent"})
                    await asyncio.sleep(3)
                    continue

                # Landed on windsurf.com (not login page, not callback processing)
                if "windsurf.com" in url and "/account/login" not in url and "accounts.google.com" not in url:
                    await asyncio.sleep(3)
                    emit({"type": "progress", "step": "redirected", "message": f"Landed on: {url[:100]}"})
                    landed = True
                    break

            except Exception:
                pass

        # Wait for background network requests to complete
        await asyncio.sleep(5)

        # Step 6: If no token captured from network, try show-auth-token page
        if not captured_token:
            emit({"type": "progress", "step": "extract", "message": "Extracting token from show-auth-token..."})
            try:
                await page.goto(f"{WINDSURF_BASE}/show-auth-token", wait_until="networkidle", timeout=15000)
                await asyncio.sleep(3)
                # The token is in a specific element, not the whole page text
                token_text = await page.evaluate("""() => {
                    // Try to find the token input/code element
                    for (const el of document.querySelectorAll('input, code, pre, textarea, [class*="token"]')) {
                        const val = (el.value || el.textContent || '').trim();
                        if (val.length > 20 && val.length < 500 && !val.includes(' ') && (val.startsWith('ott$') || val.startsWith('devin-'))) return val;
                    }
                    // Fallback: look for ott$ or devin- pattern in page text
                    const text = document.body?.innerText || '';
                    const m = text.match(/(ott\\$[A-Za-z0-9_-]+|devin-session-token\\$[A-Za-z0-9._-]+)/);
                    return m ? m[0] : null;
                }""")
                if token_text:
                    captured_token = token_text
                    emit({"type": "progress", "step": "extract", "message": f"Got token from show-auth-token: {token_text[:30]}..."})
            except Exception as e:
                emit({"type": "debug", "step": "extract", "message": f"show-auth-token failed: {e}"})

        # Save HAR for debugging
        if har_entries:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            try:
                HAR_FILE.write_text(json.dumps(har_entries, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        if captured_token:
            return {
                "success": True,
                "api_key": captured_token,
                "email": captured_email or email,
                "auth_method": "google_oauth",
            }
        else:
            return {"success": False, "error": "Could not capture auth token after Google login"}

    except Exception as e:
        return {"success": False, "error": f"Browser error: {e}"}
    finally:
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            pass


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

async def _main():
    parser = argparse.ArgumentParser(description="Windsurf account login")
    parser.add_argument("--email", required=True, help="Account email")
    parser.add_argument("--password", required=True, help="Account password")
    parser.add_argument("--proxy", default=None, help="Proxy URL (optional)")
    parser.add_argument("--google", action="store_true", help="Use Google OAuth (Camoufox)")
    parser.add_argument("--headless", action="store_true", default=False, help="Run browser headless")
    args = parser.parse_args()

    if args.google:
        result = await windsurf_login_google(args.email, args.password, proxy=args.proxy, headless=args.headless)
    else:
        result = await windsurf_login_password(args.email, args.password, proxy=args.proxy)

    emit(result)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
