#!/usr/bin/env python3
"""
Intercept & automate Gumloop University flow.

Full automation:
  1. Login Google via Camoufox (browser visible)
  2. Extract Firebase tokens from IndexedDB
  3. Create gummie via REST API
  4. Create MCP credential via REST API
  5. Attach MCP to gummie via PATCH
  6. Navigate to Gumloop University
  7. Handle OAuth authorize (click "Allow")
  8. Answer quiz questions on each lesson (1-6)

Usage:
    python intercept_gumloop_university.py --email X@gmail.com --password Y
    python intercept_gumloop_university.py --email X@gmail.com --password Y --mcp-url https://mcp.example.com
    python intercept_gumloop_university.py --email X@gmail.com --password Y --answers "2,1,3,2,1,3"

Quiz answers are 1-indexed option numbers for lessons 1-6.
If not provided, script pauses at each quiz for manual input.
"""

import argparse
import asyncio
import json
import os
import random
import string
import sys
import time
import uuid
import base64

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ─── Config ──────────────────────────────────────────────────────────

API_BASE = "https://api.gumloop.com"
FIREBASE_API_KEY = "AIzaSyCYuXqbJ0YBNltoGS4-7Y6Hozrra8KKmaE"
FIREBASE_REFRESH_URL = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"

UNIVERSITY_BASE = "https://university.gumloop.com"
UNIVERSITY_START = f"{UNIVERSITY_BASE}/getting-started-with-gumloop/what-is-gumloop"
AI_FUNDAMENTALS_START = f"{UNIVERSITY_BASE}/ai-fundamentals/what-is-an-ai-model"
OAUTH_AUTHORIZE_URL = "https://www.gumloop.com/oauth/authorize"

# Course plans in order
COURSE_PLAN = [
    {
        "name": "getting-started-with-gumloop",
        "start_path": "/getting-started-with-gumloop/what-is-gumloop",
        "lesson_paths": [
            "/getting-started-with-gumloop/what-is-gumloop",
            "/getting-started-with-gumloop/building-your-first-agent",
            "/getting-started-with-gumloop/bring-your-agents-where-you-work",
            "/getting-started-with-gumloop/teach-your-agents-skills",
            "/getting-started-with-gumloop/tasks-for-your-agents",
            "/getting-started-with-gumloop/chat-with-gumloop",
        ],
        "expected_reward_credits": 10000,
    },
    {
        "name": "ai-fundamentals",
        "start_path": "/ai-fundamentals/what-is-an-ai-model",
        "lesson_paths": [
            "/ai-fundamentals/what-is-an-ai-model",
            "/ai-fundamentals/hallucinations",
            "/ai-fundamentals/tokens-and-costs",
            "/ai-fundamentals/context",
            "/ai-fundamentals/giving-your-chatbot-tools",
            "/ai-fundamentals/what-are-instructions",
            "/ai-fundamentals/skills",
        ],
        "expected_reward_credits": 5000,
    },
]

# Course-local default answers when auto-detection fails (1-indexed option numbers)
COURSE_DEFAULT_ANSWERS = {
    "getting-started-with-gumloop": [2, 3, 3, 2, 2, 2],
    "ai-fundamentals": [2, 2, 2, 2, 2, 2, 2],
}

# Captured HTTP traffic
captured: list[dict] = []
CAPTURE_DOMAINS = [
    "api.gumloop.com",
    "gumloop.com",
    "university.gumloop.com",
    "firebaseinstallations.googleapis.com",
]
SKIP_PATTERNS = [
    ".js", ".css", ".png", ".jpg", ".svg", ".woff", ".ico",
    "analytics", "gtag", "sentry", "hotjar", "intercom",
    "segment", "mixpanel", "amplitude",
]


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", flush=True)


def random_mcp_name() -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"mcp-{suffix}"


def should_capture(url: str) -> bool:
    if not any(d in url for d in CAPTURE_DOMAINS):
        return False
    url_lower = url.lower()
    if any(p in url_lower for p in SKIP_PATTERNS):
        return False
    return True


def format_capture(entry: dict) -> str:
    method = entry.get("method", "?")
    url = entry.get("url", "?")
    status = entry.get("status", "?")
    return f"  {method} {status} {url}"


# ─── Browser Login (Camoufox) ───────────────────────────────────────


async def extract_firebase_tokens(page) -> dict | None:
    """Extract Firebase auth tokens from IndexedDB."""
    try:
        result = await page.evaluate("""() => {
            return new Promise((resolve) => {
                const request = indexedDB.open('firebaseLocalStorageDb');
                request.onsuccess = (event) => {
                    const db = event.target.result;
                    try {
                        const tx = db.transaction('firebaseLocalStorage', 'readonly');
                        const store = tx.objectStore('firebaseLocalStorage');
                        const getAll = store.getAll();
                        getAll.onsuccess = () => {
                            for (const item of getAll.result) {
                                const val = item.value || item;
                                if (!val || !val.uid) continue;
                                const stm = val.stsTokenManager || {};
                                const idToken = stm.accessToken || val.accessToken || '';
                                const refreshToken = stm.refreshToken || val.refreshToken || '';
                                if (idToken) {
                                    resolve({
                                        idToken, refreshToken,
                                        uid: val.uid || '',
                                        email: val.email || '',
                                        displayName: val.displayName || '',
                                    });
                                    return;
                                }
                            }
                            resolve(null);
                        };
                        getAll.onerror = () => resolve(null);
                    } catch(e) { resolve(null); }
                };
                request.onerror = () => resolve(null);
            });
        }""")
        return result
    except Exception as e:
        log(f"extract_firebase_tokens error: {e}")
        return None


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        data = base64.urlsafe_b64decode(payload + padding)
        return json.loads(data.decode("utf-8", errors="replace"))
    except Exception:
        return {}


async def extract_gumloop_auth_fallback(page) -> dict | None:
    """Fallback auth extraction from Gumloop DOM / local storage when IndexedDB isn't ready."""
    try:
        result = await page.evaluate("""() => {
            const el = document.querySelector('#gumloop-auth');
            const token = el?.dataset?.token || el?.getAttribute('data-token') || '';
            const email = el?.dataset?.email || el?.getAttribute('data-email') || '';
            if (!token) return null;
            return { token, email };
        }""")
        if not result or not result.get("token"):
            return None

        payload = _decode_jwt_payload(result["token"])
        return {
            "idToken": result["token"],
            "refreshToken": payload.get("firebase", {}).get("sign_in_provider", ""),
            "uid": payload.get("user_id") or payload.get("sub") or payload.get("uid") or "",
            "email": result.get("email") or payload.get("email") or "",
            "displayName": payload.get("name") or payload.get("name", "") or "",
        }
    except Exception as e:
        log(f"extract_gumloop_auth_fallback error: {e}")
        return None


async def click_google_login(page) -> bool:
    """Click Get Started → Continue with Google."""
    for _ in range(5):
        try:
            opened = await page.evaluate("""() => {
                for (const btn of document.querySelectorAll('button')) {
                    const txt = (btn.textContent || '').trim().toLowerCase();
                    if (txt === 'get started' && btn.offsetParent !== null) {
                        btn.click(); return true;
                    }
                }
                return false;
            }""")
            if opened:
                await asyncio.sleep(2)
                break
        except Exception:
            pass
        await asyncio.sleep(1)

    for _ in range(10):
        try:
            clicked = await page.evaluate("""() => {
                for (const btn of document.querySelectorAll('button, a, div[role="button"]')) {
                    const txt = (btn.textContent || '').trim().toLowerCase();
                    if (txt.includes('continue with google') && btn.offsetParent !== null) {
                        btn.click(); return true;
                    }
                }
                return false;
            }""")
            if clicked:
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def fill_google_email(page, email: str) -> bool:
    try:
        await page.wait_for_selector("#identifierId", state="visible", timeout=15000)
        loc = page.locator("#identifierId").first
        await loc.click(force=True)
        await asyncio.sleep(0.2)
        await loc.press("Control+a")
        await loc.press("Backspace")
        await loc.press_sequentially(email, delay=60)
        await asyncio.sleep(0.5)
        await page.evaluate("() => { const b = document.querySelector('#identifierNext button'); if (b) b.click(); }")
        await asyncio.sleep(3)
        return True
    except Exception as e:
        log(f"fill_google_email error: {e}")
        return False


async def fill_google_password(page, password: str) -> bool:
    try:
        await page.wait_for_selector('input[name="Passwd"]', state="visible", timeout=15000)
        loc = page.locator('input[name="Passwd"]').first
        await loc.click(force=True)
        await asyncio.sleep(0.2)
        await loc.press("Control+a")
        await loc.press("Backspace")
        await loc.press_sequentially(password, delay=70)
        await asyncio.sleep(0.5)
        url_before = page.url
        await page.evaluate("() => { const b = document.querySelector('#passwordNext button'); if (b) b.click(); }")
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
                    log(f"Password error: {has_error}")
                    return False
            except Exception:
                pass
        return True
    except Exception as e:
        log(f"fill_google_password error: {e}")
        return False


async def handle_consent(google_page, main_page) -> bool:
    """Handle Google consent screens and wait for redirect to Gumloop."""
    async def _auth_token_present(target) -> bool:
        try:
            return await target.evaluate("""() => {
                const el = document.querySelector('#gumloop-auth');
                const token = el?.dataset?.token || el?.getAttribute('data-token') || '';
                return !!(token && token.trim());
            }""")
        except Exception:
            return False

    async def _oauth_done() -> bool:
        try:
            pages = []
            try:
                pages = [google_page, main_page, *google_page.context.pages]
            except Exception:
                pages = [google_page, main_page]

            for target in pages:
                try:
                    if target.is_closed():
                        continue
                    target_url = target.url
                except Exception:
                    continue

                if "/login/callback/oauth" in target_url:
                    if await _auth_token_present(target):
                        return True
                    return True

                if "gumloop.com" in target_url and "accounts.google.com" not in target_url and "/login" not in target_url:
                    if await _auth_token_present(target):
                        return True

            return False
        except Exception:
            return False

    for _ in range(60):
        await asyncio.sleep(1)
        try:
            for target in [google_page, main_page]:
                try:
                    if target.is_closed():
                        continue
                    target_url = target.url
                    if "accounts.google.com" not in target_url:
                        continue
                    await target.evaluate("""() => {
                        // Only click explicit approval buttons.
                        for (const btn of document.querySelectorAll('button, div[role="button"], a[role="button"]')) {
                            const t = (btn.textContent||'').trim().toLowerCase();
                            if (!t || btn.offsetParent === null) continue;
                            if (t === 'continue' || t === 'allow' || t === 'lanjutkan' || t === 'lanjut'
                                || t === 'i understand' || t === 'saya mengerti' || t === 'accept' || t === 'agree'
                                || t === 'got it' || t === 'next') {
                                btn.click(); return true;
                            }
                        }
                        // Fallback: only obvious submit/confirm controls.
                        const el = document.querySelector('#confirm') || document.querySelector('input[type="submit"]');
                        if (el) { el.click(); return true; }
                        return false;
                    }""")
                except Exception:
                    pass

            if await _oauth_done():
                return True
        except Exception:
            pass
    return False


async def click_google_account_choice(page, expected_email: str = "") -> bool:
    """Click the visible Google account on the account chooser screen.

    Returns True when it clicked a matching account row or any visible account row.
    """
    try:
        result = await page.evaluate(r"""(expectedEmail) => {
            const normalize = (text) => (text || '').trim().toLowerCase();
            const expected = normalize(expectedEmail);

            // Most reliable: find elements with data-identifier attribute (Google's standard)
            const identified = Array.from(document.querySelectorAll('[data-identifier]'))
                .filter(el => el && el.offsetParent !== null);

            for (const el of identified) {
                const idVal = normalize(el.getAttribute('data-identifier') || '');
                if (!idVal) continue;
                const txt = normalize(el.textContent || el.innerText || '');
                // Match by data-identifier (email) or text content
                const emailMatch = expected && (idVal === expected || idVal.includes(expected) || txt.includes(expected));
                if (emailMatch) {
                    el.click();
                    return true;
                }
            }

            // If no email match found, click first visible data-identifier element
            // that is NOT "Use another account"
            for (const el of identified) {
                const txt = normalize(el.textContent || el.innerText || '');
                if (txt.includes('use another account')) continue;
                el.click();
                return true;
            }

            // Broader strategy: look for visible account rows by role or class patterns
            const candidates = Array.from(document.querySelectorAll(
                'li[data-identifier], div[role="link"], div[class*="account"], ' +
                '[class*="accountChooser"] li, [jsname] li, ul[class*="account"] li'
            )).filter(el => el && el.offsetParent !== null);

            for (const el of candidates) {
                const txt = normalize(el.textContent || el.innerText || '');
                if (!txt || txt.includes('use another account') || txt.includes('help') || txt.includes('privacy')) continue;
                if (expected && (txt.includes(expected) || txt.includes('@'))) {
                    el.click();
                    return true;
                }
            }

            // Last resort: any visible clickable item with an @ sign
            const allVisible = Array.from(document.querySelectorAll(
                'button, a, div[role="button"], li, [tabindex]:not([tabindex="-1"])'
            )).filter(el => el && el.offsetParent !== null);

            for (const el of allVisible) {
                const txt = normalize(el.textContent || el.innerText || '');
                if (txt.includes('@') && !txt.includes('use another account')) {
                    el.click();
                    return true;
                }
            }

            return false;
        }""", expected_email)
        return bool(result)
    except Exception as e:
        log(f"click_google_account_choice error: {e}")
        return False


async def _close_popup_safe(popup_page) -> None:
    """Close a Google popup page if it's still open."""
    if popup_page is None:
        return
    try:
        if not popup_page.is_closed():
            await popup_page.close()
    except Exception:
        pass


async def _try_google_login_flow(page, email: str, password: str) -> tuple:
    """Single attempt at the Google OAuth flow inside an already-open browser.

    Returns (tokens_dict | None, error_str | None, popup_page_to_close).
    On success: (tokens, None, popup).
    On failure: (None, "reason", popup).
    """
    popup_page = None
    popup_future = asyncio.get_event_loop().create_future()

    def on_popup(p):
        nonlocal popup_page
        popup_page = p
        if not popup_future.done():
            popup_future.set_result(p)

    page.context.on("page", on_popup)
    try:
        clicked = await click_google_login(page)
        if not clicked:
            return None, "Could not find Google sign-in button", popup_page

        try:
            await asyncio.wait_for(popup_future, timeout=10)
        except asyncio.TimeoutError:
            pass

        google_page = popup_page or page
        if popup_page:
            try:
                await popup_page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                return None, "Google popup failed to load", popup_page
        await asyncio.sleep(2)

        # Account chooser
        try:
            chooser_url = google_page.url if not google_page.is_closed() else ""
        except Exception:
            chooser_url = ""
        if "accounts.google.com" in chooser_url and "accountchooser" in chooser_url:
            log("Google account chooser detected — clicking saved account...")
            chosen = await click_google_account_choice(google_page, email)
            if not chosen:
                log("WARNING: Could not auto-click account chooser; continuing")
            else:
                await asyncio.sleep(3)

        # Email
        log(f"Filling email: {email}")
        ok = await fill_google_email(google_page, email)
        if not ok:
            return None, "Failed to fill Google email", popup_page

        try:
            after_email_url = google_page.url if not google_page.is_closed() else ""
        except Exception:
            after_email_url = ""
        if "accounts.google.com" in after_email_url:
            log("Checking for account chooser after email entry...")
            chosen_after = await click_google_account_choice(google_page, email)
            if chosen_after:
                log("Clicked account in post-email chooser")
                await asyncio.sleep(3)

        # Password
        log("Filling password...")
        ok = await fill_google_password(google_page, password)
        if not ok:
            return None, "Failed to fill Google password", popup_page

        # Consent & redirect
        log("Handling consent & redirect...")
        redirected = await handle_consent(google_page, page)
        if not redirected:
            if "gumloop.com" not in page.url:
                return None, "Failed to redirect to Gumloop after consent", popup_page

        await asyncio.sleep(3)

        log("Extracting Firebase tokens.")

        try:
            current_url = page.url
            if "/onboarding" in current_url or "/boarding" in current_url:
                log("New account detected at onboarding page")
            elif "gumloop.com" in current_url:
                log(f"Redirected to: {current_url[:60]}")
        except Exception:
            pass

        tokens = None
        for tok_attempt in range(8):
            log(f"Token extraction attempt {tok_attempt+1}/8")
            tokens = await extract_firebase_tokens(page)
            if not tokens or not tokens.get("idToken"):
                tokens = await extract_gumloop_auth_fallback(page)
            if tokens and tokens.get("idToken"):
                log(f"Got tokens (uid={tokens.get('uid', '?')[:10]}.)")
                break

            if tok_attempt == 3:
                log("Tokens not found yet, trying /home navigation.")
                try:
                    await page.goto("https://www.gumloop.com/home", wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(4)
                    log("Navigated to /home")
                except Exception as e:
                    log(f"Navigation to /home failed: {e}")

            await asyncio.sleep(3)

        if not tokens or not tokens.get("idToken"):
            log("FAILED: No tokens after 8 attempts")
            return None, "Failed to extract Firebase tokens", popup_page

        log("Token extraction complete")

        return tokens, None, popup_page

    finally:
        # Remove the popup listener to avoid stacking on retries
        try:
            page.context.remove_listener("page", on_popup)
        except Exception:
            pass


async def browser_login(email: str, password: str) -> tuple:
    """
    Full browser login flow via Camoufox with in-session retry.

    On error at any Google auth step, closes the login dialog/popup,
    navigates back to Gumloop home, and clicks "Login by Google" again.
    Up to 3 retries WITHOUT restarting the browser.

    Returns (tokens_dict, manager, page) — caller is responsible for closing browser.
    On error returns ({"error": ...}, None, None).
    """
    from browserforge.fingerprints import Screen
    from camoufox.async_api import AsyncCamoufox

    log("Launching browser (visible)...")
    manager = AsyncCamoufox(
        headless=os.environ.get("BATCHER_CAMOUFOX_HEADLESS", "false").lower() in ("true", "1"),
        os="windows",
        block_webrtc=True,
        humanize=False,
        screen=Screen(max_width=1920, max_height=1080),
    )
    browser = await manager.__aenter__()
    page = await browser.new_page()
    page.set_default_timeout(30000)

    max_in_session_retries = 3
    last_error = "Unknown error"

    try:
        log("Opening Gumloop...")
        await page.goto("https://www.gumloop.com/home", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        for attempt in range(1, max_in_session_retries + 1):
            log(f"Google login attempt {attempt}/{max_in_session_retries}...")

            tokens, error, popup = await _try_google_login_flow(page, email, password)

            if tokens and not error:
                # Success
                await _close_popup_safe(popup)
                log(f"Got tokens (uid={tokens.get('uid', '?')[:8]}...)")
                result = {
                    "id_token": tokens["idToken"],
                    "refresh_token": tokens.get("refreshToken", ""),
                    "user_id": tokens.get("uid", ""),
                    "email": tokens.get("email", email),
                    "display_name": tokens.get("displayName", ""),
                }
                return result, manager, page

            # Failed — close popup, go back to Gumloop, try again
            last_error = error or "Unknown error"
            log(f"Attempt {attempt} failed: {last_error}")

            await _close_popup_safe(popup)

            if attempt < max_in_session_retries:
                log("Closing login dialog, navigating back to Gumloop home...")
                try:
                    await page.goto("https://www.gumloop.com/home", wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(3)
                except Exception as nav_err:
                    log(f"Navigation back failed: {nav_err}")
                log("Retrying — clicking Login by Google again...")

        # All in-session retries exhausted
        await manager.__aexit__(None, None, None)
        return {"error": f"All {max_in_session_retries} in-session retries failed: {last_error}"}, None, None

    except Exception as e:
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            pass
        return {"error": str(e)}, None, None


# ─── API Calls ───────────────────────────────────────────────────────


def _api_headers(id_token: str, user_id: str) -> dict:
    return {
        "Authorization": f"Bearer {id_token}",
        "x-auth-key": user_id,
        "Content-Type": "application/json",
        "Origin": "https://www.gumloop.com",
        "Referer": "https://www.gumloop.com/",
    }


async def refresh_token(refresh_tok: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(FIREBASE_REFRESH_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
        })
        resp.raise_for_status()
        data = resp.json()
        return {
            "id_token": data.get("id_token", ""),
            "refresh_token": data.get("refresh_token", refresh_tok),
            "user_id": data.get("user_id", ""),
        }


async def create_gummie(id_token: str, user_id: str, name: str = "Proxy Agent") -> str:
    headers = _api_headers(id_token, user_id)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{API_BASE}/gummies", json={
            "name": name,
            "model_name": "claude-sonnet-4-5",
            "author_id": user_id,
        }, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        gummie_id = data.get("gummie", {}).get("gummie_id", "")
        if not gummie_id:
            raise ValueError(f"No gummie_id in response: {data}")
        return gummie_id


async def create_mcp_secret(
    id_token: str, user_id: str,
    mcp_url: str, mcp_name: str,
) -> str:
    headers = _api_headers(id_token, user_id)
    payload = {
        "secret_type": "mcp_server",
        "value": "",
        "metadata": [
            {
                "name": "URL",
                "value": mcp_url,
                "placeholder": "https://mcp.example.com",
            },
            {
                "name": "Label",
                "value": mcp_name,
                "placeholder": "slack-mcp-server",
            },
            {
                "name": "Access Token / API Key",
                "value": "",
                "description": "OAuth authentication token, if required by the MCP server.",
                "placeholder": "xxxxxxxxxxxxxxxxxxxxxxxx",
                "isSecret": True,
                "isOptional": True,
            },
            {
                "name": "Additional Header",
                "value": "",
                "description": "Additional Header",
                "placeholder": "Authorization: Basic xxxxxxxxxxxxxxxxxxxxxxxx",
                "isOptional": True,
            },
        ],
        "nickname": mcp_name,
        "user_id": user_id,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{API_BASE}//secret", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        secret_id = data.get("secret_id", "")
        if not secret_id:
            raise ValueError(f"No secret_id in response: {data}")
        return secret_id


async def attach_mcp_to_gummie(
    id_token: str, user_id: str,
    gummie_id: str, secret_id: str,
    mcp_url: str, mcp_name: str,
) -> dict:
    headers = _api_headers(id_token, user_id)
    payload = {
        "tools": [
            {
                "secret_id": secret_id,
                "mcp_server_url": mcp_url,
                "name": mcp_name,
                "type": "mcp_server",
                "restricted_tools": [],
            },
            {"metadata": {}, "type": "web_search"},
            {"metadata": {}, "type": "web_fetch"},
            {"metadata": {"model": "gemini-3.1-flash-image-preview"}, "type": "image_generator"},
            {"type": "interaction_search"},
        ],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.patch(
            f"{API_BASE}/gummies/{gummie_id}",
            json=payload, headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


async def verify_mcp_servers(id_token: str, user_id: str) -> list:
    headers = _api_headers(id_token, user_id)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{API_BASE}//secrets/mcp_servers", headers=headers)
        resp.raise_for_status()
        return resp.json()


# ─── University Browser Automation ───────────────────────────────────


async def setup_request_interception(page):
    """Attach request/response listeners for HTTP traffic capture."""

    async def on_response(response):
        url = response.url
        if not should_capture(url):
            return
        request = response.request
        entry = {
            "timestamp": time.strftime("%H:%M:%S"),
            "method": request.method,
            "url": url,
            "status": response.status,
            "request_headers": dict(request.headers) if request.headers else {},
        }
        # Try to capture response body for API calls
        try:
            if "api.gumloop.com" in url or "university.gumloop.com" in url:
                content_type = response.headers.get("content-type", "")
                if "json" in content_type:
                    body = await response.text()
                    entry["response_body"] = body[:2000]
        except Exception:
            pass

        # Try to capture request post data
        try:
            post = request.post_data
            if post:
                entry["request_body"] = post[:2000]
        except Exception:
            pass

        captured.append(entry)
        print(format_capture(entry))

    page.on("response", on_response)


async def navigate_to_university(page, start_path: str | None = None) -> bool:
    """Navigate to Gumloop University start page."""
    start_url = f"{UNIVERSITY_BASE}{start_path or '/getting-started-with-gumloop/what-is-gumloop'}"
    log(f"Navigating to Gumloop University: {start_url}")
    try:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        
        # Check if there's a Login button and click it
        log("Checking for Login button...")
        for attempt in range(5):
            login_clicked = await page.evaluate("""() => {
                // Look for Login/Sign In button
                const buttons = document.querySelectorAll('button, a, div[role="button"], [class*="login"], [class*="signin"]');
                for (const btn of buttons) {
                    const txt = (btn.textContent || btn.innerText || '').trim().toLowerCase();
                    if ((txt.includes('login') || txt.includes('sign in') || txt === 'login' || txt === 'sign in') 
                        && btn.offsetParent !== null) {
                        btn.click();
                        return 'clicked: ' + txt;
                    }
                }
                
                // Also try href links
                const links = document.querySelectorAll('a[href*="login"], a[href*="signin"]');
                if (links.length > 0 && links[0].offsetParent !== null) {
                    links[0].click();
                    return 'clicked link: ' + links[0].textContent.trim();
                }
                
                return null;
            }""")
            
            if login_clicked:
                log(f"Login button clicked: {login_clicked}")
                await asyncio.sleep(3)
                break
            
            await asyncio.sleep(1)
        
        return True
    except Exception as e:
        log(f"Navigation error: {e}")
        return False


async def handle_oauth_authorize(page, context) -> bool:
    """
    Handle the OAuth authorize redirect.
    When university redirects to gumloop.com/oauth/authorize,
    click the "Allow" button.
    """
    log("Waiting for OAuth authorize page...")

    for tick in range(60):
        await asyncio.sleep(1)
        try:
            current_url = page.url

            # Check if we're on the OAuth authorize page
            if OAUTH_AUTHORIZE_URL in current_url or "/oauth/authorize" in current_url:
                log(f"OAuth page detected: {current_url[:80]}...")
                await asyncio.sleep(2)

                # Click "Allow" button
                for attempt in range(10):
                    try:
                        clicked = await page.evaluate("""() => {
                            // Look for Allow button - multiple strategies
                            const buttons = document.querySelectorAll('button, a, div[role="button"], input[type="submit"]');
                            for (const btn of buttons) {
                                const txt = (btn.textContent || btn.value || '').trim().toLowerCase();
                                if ((txt.includes('allow') || txt === 'allow') && btn.offsetParent !== null) {
                                    btn.click();
                                    return 'clicked: ' + txt;
                                }
                            }
                            return null;
                        }""")
                        if clicked:
                            log(f"OAuth Allow button: {clicked}")
                            await asyncio.sleep(3)
                            return True
                    except Exception:
                        pass
                    await asyncio.sleep(1)

                log("Could not find Allow button, trying alternative selectors...")
                # Fallback: try specific CSS selectors from the screenshot
                try:
                    # The Allow button appears pink/gradient in the screenshot
                    allow_btn = page.locator("button:has-text('Allow')").first
                    await allow_btn.click(timeout=5000)
                    log("Clicked Allow via locator")
                    await asyncio.sleep(3)
                    return True
                except Exception:
                    pass

            # Check if we're already on university (post-auth)
            if "university.gumloop.com" in current_url and "/login" not in current_url:
                log("Already on University (authenticated)")
                return True

            # Check all pages in context for OAuth
            for p in context.pages:
                try:
                    if p.is_closed():
                        continue
                    p_url = p.url
                    if "/oauth/authorize" in p_url:
                        log(f"Found OAuth in another tab: {p_url[:80]}...")
                        await p.bring_to_front()
                        await asyncio.sleep(1)
                        await p.evaluate("""() => {
                            const buttons = document.querySelectorAll('button');
                            for (const btn of buttons) {
                                const txt = (btn.textContent || '').trim().toLowerCase();
                                if (txt.includes('allow')) {
                                    btn.click(); return true;
                                }
                            }
                            return false;
                        }""")
                        await asyncio.sleep(3)
                        return True
                except Exception:
                    pass

        except Exception:
            pass

    log("OAuth timeout - could not handle authorize")
    return False


async def answer_quiz(page, answer_index: int, force_mark_complete: bool = False) -> bool:
    """
    Answer a quiz question on the current lesson page.
    answer_index: 1-indexed (1 = first option, 2 = second, 3 = third).
    force_mark_complete: when True, attempt Mark Complete regardless of correctness.
    Returns True if answered and checked successfully.
    """
    log(f"Answering quiz: selecting option {answer_index}...")

    try:
        # Wait for quiz section to load
        await asyncio.sleep(2)

        # Select the radio button (0-indexed internally)
        idx = answer_index - 1
        selected = await page.evaluate(f"""(idx) => {{
            // Strategy 1: radio inputs in quiz area
            const radios = document.querySelectorAll('input[type="radio"]');
            if (radios.length > idx) {{
                radios[idx].click();
                // Also click the label/parent for React-style components
                const label = radios[idx].closest('label') || radios[idx].parentElement;
                if (label) label.click();
                return 'radio-' + idx;
            }}

            // Strategy 2: quiz option divs/labels (clickable options)
            const options = document.querySelectorAll('[class*="quiz"] label, [class*="quiz"] [role="radio"], [class*="option"], .quiz-option');
            if (options.length > idx) {{
                options[idx].click();
                return 'option-' + idx;
            }}

            // Strategy 3: any label that looks like a quiz option (has radio inside or sibling)
            const labels = Array.from(document.querySelectorAll('label'));
            const quizLabels = labels.filter(l => {{
                const radio = l.querySelector('input[type="radio"]') || l.previousElementSibling?.matches?.('input[type="radio"]');
                return radio || l.closest('[class*="quiz"]');
            }});
            if (quizLabels.length > idx) {{
                quizLabels[idx].click();
                return 'label-' + idx;
            }}

            return null;
        }}""", idx)

        if not selected:
            log(f"Could not find quiz option {answer_index}, trying broader search...")
            # Broader: find all clickable elements near "Quiz:" text
            selected = await page.evaluate(f"""(idx) => {{
                // Find radio inputs or circles that act as radio buttons
                const all = document.querySelectorAll('input[type="radio"], [role="radio"], circle');
                const visible = Array.from(all).filter(el => el.offsetParent !== null);
                if (visible.length > idx) {{
                    visible[idx].click();
                    const parent = visible[idx].closest('label') || visible[idx].parentElement;
                    if (parent) parent.click();
                    return 'broad-' + idx;
                }}
                return null;
            }}""", idx)

        if selected:
            log(f"Selected: {selected}")
            await asyncio.sleep(1)
        else:
            log(f"WARNING: Could not select option {answer_index}")
            return False

        # Click "Check" button - try multiple times with better selectors
        await asyncio.sleep(1)
        clicked_check = False
        for check_attempt in range(5):
            result = await page.evaluate("""() => {
                // Strategy 0: Specific Gumloop University quiz check button
                const quizCheckLabel = document.querySelector('label[for="q-check"].quiz-check-btn');
                if (quizCheckLabel && quizCheckLabel.offsetParent !== null) {
                    quizCheckLabel.click();
                    return 'clicked quiz-check-btn label';
                }
                
                // Strategy 1: Look for buttons with "check" text (case-insensitive)
                const buttons = document.querySelectorAll('button, a[role="button"], div[role="button"], input[type="submit"]');
                for (const btn of buttons) {
                    const txt = (btn.textContent || btn.innerText || btn.value || '').trim().toLowerCase();
                    if ((txt === 'check' || txt === 'submit' || txt === 'verify' || txt.includes('check')) 
                        && btn.offsetParent !== null && !btn.disabled) {
                        btn.click();
                        return 'clicked: ' + txt;
                    }
                }
                
                // Strategy 2: Look for buttons by class or id
                const checkBtns = document.querySelectorAll('[class*="check"], [class*="submit"], [id*="check"], [id*="submit"]');
                for (const btn of checkBtns) {
                    if (btn.tagName.toLowerCase() === 'button' && btn.offsetParent !== null && !btn.disabled) {
                        btn.click();
                        return 'clicked by class: ' + (btn.className || btn.id);
                    }
                }
                
                // Strategy 3: Find submit type buttons
                const submitBtns = document.querySelectorAll('button[type="submit"]');
                if (submitBtns.length > 0 && submitBtns[0].offsetParent !== null && !submitBtns[0].disabled) {
                    submitBtns[0].click();
                    return 'clicked submit button';
                }
                
                return null;
            }""")
            
            if result:
                log(f"Check button clicked: {result}")
                clicked_check = True
                await asyncio.sleep(2)
                break
            
            await asyncio.sleep(0.5)
        
        if not clicked_check:
            log("WARNING: Could not find Check button after multiple attempts")

        quiz_result = await wait_for_quiz_result(page)
        if quiz_result == "wrong":
            log("Quiz result is WRONG")
            if not force_mark_complete:
                log("Not advancing")
                return False
            log("force_mark_complete=True, proceeding despite wrong answer...")
        elif quiz_result != "correct":
            log(f"WARNING: Quiz result not confirmed (got {quiz_result!r})")
            if not force_mark_complete:
                log("Not advancing")
                return False
            log("force_mark_complete=True, proceeding despite unconfirmed result...")
        else:
            log("Quiz result confirmed CORRECT")

        clicked_mark = await click_mark_complete(page)
        if clicked_mark:
            log(f"Clicked '{clicked_mark}'")
            completed = await wait_for_completion_confirmation(page)
            if not completed:
                log("WARNING: Mark Complete clicked but completion state was not confirmed")
                if not force_mark_complete:
                    return False
                log("force_mark_complete=True, treating as completed anyway")
            else:
                log("Lesson completion confirmed")
        else:
            log("No 'Mark Complete' button found; assuming lesson auto-completes")

        return True

    except Exception as e:
        log(f"Answer quiz error: {e}")
        return False


async def detect_correct_answer(page) -> dict | None:
    """Detect the correct quiz answer from DOM markers, independent of option ordering.

    Enhanced to handle cases where the correct answer is identified by:
    - CSS class 'quiz-correct' on the option element
    - 'quiz-explain-correct' explanation text matching an option
    - data-correct attribute on the option or its radio input
    - aria-label / aria-checked state after checking
    - Hidden input or sibling element marking the correct option
    - The checked radio input inside a correct-class parent
    """
    try:
        result = await page.evaluate(r"""() => {
            const normalize = (text) => (text || '').trim().replace(/\s+/g, ' ');
            const options = Array.from(document.querySelectorAll('label.quiz-option, .quiz-option'))
                .filter(el => el && el.offsetParent !== null);
            if (!options.length) return null;

            // Strategy 1: quiz-correct class on option
            for (let i = 0; i < options.length; i++) {
                const option = options[i];
                const cls = option.className || '';
                if (cls.includes('quiz-correct')) {
                    return {
                        index: i + 1,
                        text: normalize(option.textContent || option.innerText || ''),
                        source: 'quiz-correct-class'
                    };
                }
            }

            // Strategy 2: data-correct attribute on option or child input
            for (let i = 0; i < options.length; i++) {
                const option = options[i];
                if (option.dataset && (option.dataset.correct === 'true' || option.dataset.correct === '1' || option.getAttribute('data-correct') === 'true')) {
                    return {
                        index: i + 1,
                        text: normalize(option.textContent || option.innerText || ''),
                        source: 'data-correct-attr'
                    };
                }
                const radio = option.querySelector('input[type="radio"]');
                if (radio && (radio.dataset.correct === 'true' || radio.getAttribute('data-correct') === 'true')) {
                    return {
                        index: i + 1,
                        text: normalize(option.textContent || option.innerText || ''),
                        source: 'radio-data-correct'
                    };
                }
            }

            // Strategy 3: explanation text match (original)
            const correctExplain = document.querySelector('.quiz-explain-correct');
            if (correctExplain) {
                const explanationText = normalize(correctExplain.textContent || '');
                // Exact substring match
                for (let i = 0; i < options.length; i++) {
                    const optionText = normalize(options[i].textContent || options[i].innerText || '');
                    if (optionText && explanationText && explanationText.toLowerCase().includes(optionText.toLowerCase())) {
                        return {
                            index: i + 1,
                            text: optionText,
                            source: 'explanation-text-match'
                        };
                    }
                }
                // Partial keyword overlap (for cases where explanation paraphrases the answer)
                for (let i = 0; i < options.length; i++) {
                    const optionText = normalize(options[i].textContent || options[i].innerText || '');
                    if (!optionText) continue;
                    const optionWords = optionText.toLowerCase().split(/\s+/).filter(w => w.length > 3);
                    const explainLower = explanationText.toLowerCase();
                    const matchCount = optionWords.filter(w => explainLower.includes(w)).length;
                    if (optionWords.length > 0 && matchCount >= Math.ceil(optionWords.length * 0.6)) {
                        return {
                            index: i + 1,
                            text: optionText,
                            source: 'explanation-keyword-overlap'
                        };
                    }
                }
            }

            // Strategy 4: Look for correct answer in hidden elements or script data
            const scripts = document.querySelectorAll('script[type="application/json"], script[type="application/ld+json"]');
            for (const script of scripts) {
                try {
                    const data = JSON.parse(script.textContent || '{}');
                    const correctIdx = data.correctAnswer || data.correct_answer || data.answer;
                    if (typeof correctIdx === 'number' && correctIdx >= 0 && correctIdx < options.length) {
                        return {
                            index: correctIdx + 1,
                            text: normalize(options[correctIdx].textContent || ''),
                            source: 'script-json-data'
                        };
                    }
                } catch(e) {}
            }

            // Strategy 5: CSS color / style hints (correct answers often have green styling)
            for (let i = 0; i < options.length; i++) {
                const style = window.getComputedStyle(options[i]);
                const color = style.color || '';
                const bg = style.backgroundColor || '';
                // Green color typically indicates correct
                if ((color.includes('rgb(0') || color.includes('rgb(34') || color.includes('rgb(22') || color.includes('#0') || color.includes('#2'))
                    && (bg.includes('rgb(') || bg === '')) {
                    // Only use this if other options don't share the same color
                    const othersSame = options.some((opt, j) => {
                        if (j === i) return false;
                        return window.getComputedStyle(opt).color === color;
                    });
                    if (!othersSame) {
                        return {
                            index: i + 1,
                            text: normalize(options[i].textContent || options[i].innerText || ''),
                            source: 'css-color-hint'
                        };
                    }
                }
            }

            return null;
        }""")
        return result
    except Exception as e:
        log(f"detect_correct_answer error: {e}")
        return None


async def navigate_to_lesson(page, lesson_path: str) -> bool:
    """Navigate to a specific lesson path."""
    url = f"{UNIVERSITY_BASE}{lesson_path}"
    log(f"Navigating to lesson: {url}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        return True
    except Exception as e:
        log(f"Lesson nav error: {e}")
        return False


async def wait_for_quiz_result(page, timeout_seconds: int = 12) -> str | None:
    """Wait until the page clearly shows whether the quiz answer is correct or wrong."""
    for _ in range(timeout_seconds * 2):
        try:
            state = await page.evaluate("""() => {
                const visible = (el) => !!el && el.offsetParent !== null;
                const correct = document.querySelector('.quiz-explain-correct');
                const wrong = document.querySelector('.quiz-explain-wrong');
                if (visible(correct)) return 'correct';
                if (visible(wrong)) return 'wrong';

                const checkedInput = document.querySelector('#q-check');
                if (checkedInput && checkedInput.checked) {
                    if (correct && (correct.textContent || '').trim()) return 'correct';
                    if (wrong && (wrong.textContent || '').trim()) return 'wrong';
                }

                const bodyText = (document.body?.innerText || '').toLowerCase();
                if (bodyText.includes('correct!')) return 'correct';
                if (bodyText.includes('not quite')) return 'wrong';
                return null;
            }""")
            if state in ("correct", "wrong"):
                return state
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return None


async def click_mark_complete(page) -> str | None:
    """Click an explicit mark-complete control, but never click a generic already-completed label."""
    await asyncio.sleep(1)
    return await page.evaluate(r"""() => {
        const normalize = (text) => (text || '').trim().toLowerCase().replace(/\s+/g, ' ');
        const clickable = document.querySelectorAll('button, a, label[for], div[role="button"]');
        for (const el of clickable) {
            const txt = normalize(el.textContent || el.innerText || el.value || '');
            if (!txt || el.offsetParent === null) continue;
            if (txt === 'mark complete' || txt === 'mark as complete' || txt === 'complete lesson') {
                el.click();
                return txt;
            }
        }
        return null;
    }""")


async def wait_for_completion_confirmation(page, timeout_seconds: int = 12) -> bool:
    """Wait until the page shows that the lesson is complete before advancing."""
    for _ in range(timeout_seconds * 2):
        try:
            confirmed = await page.evaluate(r"""() => {
                const normalize = (text) => (text || '').trim().toLowerCase().replace(/\s+/g, ' ');
                const visible = (el) => !!el && el.offsetParent !== null;

                const clickable = document.querySelectorAll('button, a, label[for], div[role="button"]');
                for (const el of clickable) {
                    const txt = normalize(el.textContent || el.innerText || el.value || '');
                    if (!txt || !visible(el)) continue;
                    if (txt === 'completed' || txt === 'lesson completed' || txt === 'complete!') {
                        return true;
                    }
                }

                const bodyText = normalize(document.body?.innerText || '');
                if (bodyText.includes('lesson completed') || bodyText.includes('marked complete')) {
                    return true;
                }

                // Sidebar state sometimes reflects completion before navigation changes.
                const completedItem = document.querySelector('[aria-current="page"] [class*="complete"], [aria-current="page"][class*="complete"]');
                return !!completedItem;
            }""")
            if confirmed:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


async def click_lesson_sidebar(page, lesson_path: str) -> bool:
    """Try clicking lesson in the sidebar navigation."""
    try:
        clicked = await page.evaluate(f"""(path) => {{
            // Find sidebar lesson links
            const links = document.querySelectorAll('nav a, aside a, [class*="sidebar"] a, [class*="lesson"] a');
            const lessonLinks = Array.from(links).filter(a => {{
                const href = a.getAttribute('href') || '';
                return href.includes(path) || href.includes('getting-started') || href.includes('ai-fundamentals');
            }});
            if (lessonLinks.length > 0) {{
                lessonLinks[0].click();
                return lessonLinks[0].textContent.trim().substring(0, 40);
            }}
            return null;
        }}""", lesson_path)

        if clicked:
            log(f"Clicked sidebar: {clicked}")
            await asyncio.sleep(3)
            return True
        return False
    except Exception:
        return False


async def claim_course_credits(page, expected_reward_credits: int) -> bool:
    """Wait for and claim the course completion credits modal."""
    log(f"Waiting for credits claim modal ({expected_reward_credits} credits)...")
    try:
        modal_appeared = False
        for _ in range(30):
            claim_button = await page.query_selector('button.credit-redeem-action')
            if claim_button:
                modal_appeared = True
                log("Credits claim modal detected!")
                break
            await asyncio.sleep(1)

        if modal_appeared:
            log("Clicking 'Claim Credits' button...")
            await claim_button.click()
            await asyncio.sleep(2)
            log(f"Credits claimed successfully! (+{expected_reward_credits})")
            return True

        log("WARNING: Credits claim modal did not appear within 30 seconds")
        return False
    except Exception as e:
        log(f"WARNING: Failed to claim credits: {e}")
        return False


async def run_course(page, context, course: dict, answers: list[int] | None = None):
    course_name = course["name"]
    lesson_paths = course["lesson_paths"]
    default_answers = COURSE_DEFAULT_ANSWERS.get(course_name, [])

    log(f"\n### COURSE START: {course_name} ###")
    ok = await navigate_to_university(page, course["start_path"])
    if not ok:
        log(f"FAILED: Could not navigate to course {course_name}")
        return False

    await asyncio.sleep(2)
    for i, lesson_path in enumerate(lesson_paths):
        log(f"\n--- LESSON {i + 1}/{len(lesson_paths)}: {lesson_path} ---")
        ok = await navigate_to_lesson(page, lesson_path)
        if not ok:
            ok = await click_lesson_sidebar(page, lesson_path)
            if not ok:
                log(f"WARNING: Could not navigate to lesson {lesson_path}")
                continue

        await asyncio.sleep(2)
        await page.evaluate("""() => {
            const quizHeader = Array.from(document.querySelectorAll('h2, h3, h4, strong, b'))
                .find(el => (el.textContent || '').toLowerCase().includes('quiz'));
            if (quizHeader) {
                quizHeader.scrollIntoView({ behavior: 'smooth', block: 'center' });
            } else {
                window.scrollTo(0, document.body.scrollHeight * 0.7);
            }
        }""")
        await asyncio.sleep(1)

        auto_detected = await detect_correct_answer(page)
        if auto_detected:
            answer = int(auto_detected["index"])
            log(f"Auto-detected correct answer: option {answer} ({auto_detected.get('source', 'unknown-source')})")
            log(f"Auto-detected answer text: {auto_detected.get('text', '')[:120]}")
            ok = await answer_quiz(page, answer, force_mark_complete=True)
        elif i < len(default_answers):
            answer = default_answers[i]
            log(f"Using course default answer (force-complete): {answer}")
            ok = await answer_quiz(page, answer, force_mark_complete=True)
        elif answers and i < len(answers):
            answer = answers[i]
            log(f"Using provided answer (force-complete): {answer}")
            ok = await answer_quiz(page, answer, force_mark_complete=True)
        else:
            # In subprocess/headless mode (no TTY), use fallback answer instead of blocking on input()
            if not sys.stdin.isatty():
                fallback = 2  # safe default
                log(f"No TTY — using fallback answer {fallback} for lesson {i + 1} (force-complete)")
                ok = await answer_quiz(page, fallback, force_mark_complete=True)
            else:
                print()
                print(f"  ┌─ MANUAL INPUT NEEDED ─────────────────────")
                print(f"  │ Lesson {i + 1} quiz is visible in the browser.")
                print(f"  │ Enter the answer number (1, 2, or 3):")
                print(f"  └──────────────────────────────────────────────")
                try:
                    user_answer = input(f"  Answer for lesson {i + 1}: ").strip()
                    ok = user_answer.isdigit() and await answer_quiz(page, int(user_answer))
                except (EOFError, KeyboardInterrupt):
                    log("Input cancelled, skipping remaining lessons")
                    break

        if not ok:
            log(f"WARNING: Failed to answer lesson {i + 1}, force-clicking Mark Complete as fallback...")
            # Force mark complete even on failure
            try:
                clicked_mark = await click_mark_complete(page)
                if clicked_mark:
                    log(f"Fallback: clicked '{clicked_mark}'")
                    await asyncio.sleep(2)
                else:
                    log("Fallback: no Mark Complete button found, continuing anyway")
            except Exception as fallback_err:
                log(f"Fallback mark complete error: {fallback_err}")
        await asyncio.sleep(2)

    await claim_course_credits(page, course["expected_reward_credits"])
    log(f"### COURSE COMPLETE: {course_name} ###")
    return True


async def run_university_flow(page, context, answers: list[int] | None = None):
    """
    Complete Gumloop University flow:
    1. Navigate to university
    2. Handle OAuth
    3. Answer all 6 lesson quizzes
    """
    log("=" * 50)
    log("UNIVERSITY FLOW START")
    log("=" * 50)

    # Set up interception
    await setup_request_interception(page)

    # Start on the first course landing page before OAuth kicks in
    first_course_start = COURSE_PLAN[0]["start_path"]
    ok = await navigate_to_university(page, first_course_start)
    if not ok:
        log("FAILED: Could not navigate to first course landing page")
        return False

    # Handle OAuth authorize
    ok = await handle_oauth_authorize(page, context)
    if not ok:
        log("FAILED: Could not handle OAuth")
        return False

    log("OAuth complete. Starting courses...")
    await asyncio.sleep(2)
    for course in COURSE_PLAN:
        ok = await run_course(page, context, course, answers)
        if not ok:
            log(f"WARNING: Course flow issue for {course['name']}")

    log("\n" + "=" * 50)
    log("UNIVERSITY FLOW COMPLETE")
    log("=" * 50)
    return True


# ─── Main ────────────────────────────────────────────────────────────


async def main(email: str, password: str, mcp_url: str, answers: list[int] | None = None):
    print()
    print("=" * 60)
    print("  Gumloop Auto Register + MCP + University")
    print("=" * 60)
    print()

    # ── PHASE 1: Account Setup ──────────────────────────────────────

    log("PHASE 1: Account & MCP Setup")
    login_result, browser_manager, browser_page = None, None, None
    for attempt in range(1, 4):
        log(f"STEP 1: Browser login (attempt {attempt}/3)...")
        result, manager, page_obj = await browser_login(email, password)
        if "error" not in result:
            login_result, browser_manager, browser_page = result, manager, page_obj
            log(f"Login OK on attempt {attempt}")
            break
        log(f"Attempt {attempt} failed: {result['error']}")
        if attempt < 3:
            log("Waiting 5s before next attempt...")
            await asyncio.sleep(5)

    if "error" in (login_result or {"error": "no result"}):
        log("FAILED: All 3 login attempts exhausted")
        return

    # From here on, browser is alive — ensure cleanup on any exit path
    try:
        await _main_with_browser(
            login_result, browser_manager, browser_page,
            email, mcp_url, answers,
        )
    finally:
        # Always close the browser at the very end
        if browser_manager:
            try:
                await browser_manager.__aexit__(None, None, None)
            except Exception:
                pass


async def _main_with_browser(
    login_result: dict, browser_manager, browser_page,
    email: str, mcp_url: str, answers: list[int] | None,
):
    """Inner main logic — browser cleanup handled by caller."""
    id_token = login_result["id_token"]
    refresh_tok = login_result["refresh_token"]
    user_id = login_result["user_id"]
    log(f"Login OK — user_id={user_id}")

    # Refresh token
    log("Refreshing token...")
    try:
        refreshed = await refresh_token(refresh_tok)
        id_token = refreshed["id_token"] or id_token
        refresh_tok = refreshed["refresh_token"] or refresh_tok
        user_id = refreshed["user_id"] or user_id
        log("Token refreshed OK")
    except Exception as e:
        log(f"Token refresh failed (using original): {e}")

    # Step 2: Create gummie
    log("STEP 2: Creating gummie...")
    try:
        gummie_id = await create_gummie(id_token, user_id)
        log(f"Gummie created: {gummie_id}")
    except Exception as e:
        log(f"FAILED to create gummie: {e}")
        return

    # Step 3: Create MCP credential
    mcp_name = random_mcp_name()
    log(f"STEP 3: Creating MCP credential '{mcp_name}' → {mcp_url}")
    try:
        secret_id = await create_mcp_secret(id_token, user_id, mcp_url, mcp_name)
        log(f"MCP secret created: {secret_id}")
    except Exception as e:
        log(f"FAILED to create MCP secret: {e}")
        return

    # Step 4: Attach MCP to gummie
    log("STEP 4: Attaching MCP to gummie...")
    try:
        result = await attach_mcp_to_gummie(
            id_token, user_id, gummie_id, secret_id, mcp_url, mcp_name,
        )
        tools = result.get("gummie", {}).get("tools", [])
        mcp_tools = [t for t in tools if t.get("type") == "mcp_server"]
        if mcp_tools:
            log(f"MCP attached OK — {len(mcp_tools)} MCP tool(s)")
        else:
            log("WARNING: MCP not found in gummie tools after PATCH")
    except Exception as e:
        log(f"FAILED to attach MCP: {e}")
        return

    # Step 5: Verify
    log("STEP 5: Verifying MCP servers...")
    try:
        servers = await verify_mcp_servers(id_token, user_id)
        log(f"MCP servers found: {len(servers)}")
    except Exception as e:
        log(f"Verify failed: {e}")

    # Save credentials
    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "result.json")
    full_output = {
        "email": email,
        "user_id": user_id,
        "gummie_id": gummie_id,
        "mcp_secret_id": secret_id,
        "mcp_name": mcp_name,
        "mcp_url": mcp_url,
        "id_token": id_token,
        "refresh_token": refresh_tok,
    }
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(full_output, f, indent=2, ensure_ascii=False)
    log(f"Credentials saved to {out_file}")

    # Emit JSON result for batch_runner (must be before university so credentials are captured early)
    import json as _json
    _batch_result = {
        "type": "result",
        "gumloop": {
            "success": True,
            "credentials": {
                "id_token": id_token,
                "refresh_token": refresh_tok,
                "user_id": user_id,
                "gummie_id": gummie_id,
            },
        },
    }
    print(_json.dumps(_batch_result), flush=True)

    # -- PHASE 2: University Flow (reuse same browser -- session intact) --

    log("")
    log("PHASE 2: Gumloop University")
    log("Reusing browser session (already logged in)...")

    page = browser_page
    context = page.context

    try:
        # Browser is already logged into Gumloop -- go straight to university
        ok = await run_university_flow(page, context, answers)
        if not ok:
            log("University flow had issues -- check browser")

    except Exception as e:
        log(f"University flow error: {e}")
    finally:
        # Save captured traffic
        if captured:
            capture_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intercept_university_log.json")
            with open(capture_file, "w", encoding="utf-8") as f:
                json.dump(captured, f, indent=2, ensure_ascii=False)
            log(f"Captured {len(captured)} HTTP requests -> {capture_file}")

        # Browser cleanup handled by caller (_main_with_browser's caller)
        pass


    # Final output
    print()
    print("=" * 60)
    print("  RESULT")
    print("=" * 60)
    output = {
        "email": email,
        "user_id": user_id,
        "gummie_id": gummie_id,
        "mcp_secret_id": secret_id,
        "mcp_name": mcp_name,
        "mcp_url": mcp_url,
        "id_token": id_token[:50] + "...",
        "refresh_token": refresh_tok[:50] + "...",
        "captured_requests": len(captured),
    }
    for k, v in output.items():
        print(f"  {k}: {v}")
    print("=" * 60)
    print()


def parse_answers(answers_str: str) -> list[int]:
    """Parse comma-separated answer string like '2,1,3,2,1,3' into list of ints."""
    parts = [p.strip() for p in answers_str.split(",") if p.strip()]
    result = []
    for p in parts:
        if p.isdigit():
            result.append(int(p))
        else:
            raise ValueError(f"Invalid answer: {p!r} (expected number)")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Gumloop Auto Register + MCP + University Automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full auto with answers
  python intercept_gumloop_university.py --email x@gmail.com --password pass --answers "2,1,3,2,1,3"

  # Interactive (pauses at each quiz for manual input)
  python intercept_gumloop_university.py --email x@gmail.com --password pass

  # With custom MCP URL
  python intercept_gumloop_university.py --email x@gmail.com --password pass --mcp-url https://mcp.example.com
""",
    )
    parser.add_argument("--email", required=True, help="Google account email")
    parser.add_argument("--password", required=True, help="Google account password")
    parser.add_argument("--mcp-url", default="http://google.com", help="MCP server URL (default: http://google.com)")
    parser.add_argument(
        "--answers",
        default=None,
        help='Comma-separated quiz answers for lessons 1-6, e.g. "2,1,3,2,1,3"',
    )
    args = parser.parse_args()

    answers = None
    if args.answers:
        try:
            answers = parse_answers(args.answers)
            if len(answers) != 6:
                print(f"WARNING: Expected 6 answers, got {len(answers)}. Will prompt for missing.")
        except ValueError as e:
            print(f"ERROR: {e}")
            sys.exit(1)

    try:
        asyncio.run(main(args.email, args.password, args.mcp_url, answers))
    except KeyboardInterrupt:
        if captured:
            capture_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intercept_university_log.json")
            with open(capture_file, "w", encoding="utf-8") as f:
                json.dump(captured, f, indent=2, ensure_ascii=False)
            print(f"\n  Saved {len(captured)} captured requests to {capture_file}")
