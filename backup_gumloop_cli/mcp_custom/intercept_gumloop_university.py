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

import httpx

# ─── Config ──────────────────────────────────────────────────────────

API_BASE = "https://api.gumloop.com"
FIREBASE_API_KEY = "AIzaSyCYuXqbJ0YBNltoGS4-7Y6Hozrra8KKmaE"
FIREBASE_REFRESH_URL = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"

UNIVERSITY_BASE = "https://university.gumloop.com"
UNIVERSITY_START = f"{UNIVERSITY_BASE}/getting-started-with-gumloop/what-is-gumloop"
OAUTH_AUTHORIZE_URL = "https://www.gumloop.com/oauth/authorize"

# Lesson paths in order
LESSON_PATHS = [
    "/getting-started-with-gumloop/what-is-gumloop",                    # Lesson 1: Introduction
    "/getting-started-with-gumloop/building-your-first-agent",          # Lesson 2: Build Your First Agent
    "/getting-started-with-gumloop/bring-your-agents-where-you-work",   # Lesson 3: Bring Your Agent Where You Work
    "/getting-started-with-gumloop/teach-your-agents-skills",           # Lesson 4: Teach Your Agents with Skills
    "/getting-started-with-gumloop/tasks-for-your-agents",              # Lesson 5: Triggers for Your Agents
    "/getting-started-with-gumloop/chat-with-gumloop",                  # Lesson 6: Chat with Gumloop
]

# Correct quiz answers (1-indexed option numbers)
# Lesson 1: option 2 - "A tool connected to your apps that follows instructions to do work for you."
# Lesson 2: option 3 - "A model, tools (apps), and instructions."
# Lesson 3: option 3 - "It starts a new conversation with the agent in a thread, and you reply in the thread to continue."
# Lesson 4: option 2 - "Agents have no memory between conversations, so skills give them reusable procedures for specific tasks."
# Lesson 5: option 2 - "Add a recurring scheduled trigger directly on the agent."
# Lesson 6: option 2 - "When you have a one-off task and don't want to configure a new agent for it."
CORRECT_ANSWERS = [2, 3, 3, 2, 2, 2]

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
    for tick in range(60):
        await asyncio.sleep(1)
        try:
            popup_closed = False
            try:
                popup_closed = google_page.is_closed()
            except Exception:
                popup_closed = True

            for target in [google_page, main_page]:
                try:
                    if target.is_closed():
                        continue
                    target_url = target.url
                    if "accounts.google.com" not in target_url:
                        continue
                    await target.evaluate("""() => {
                        // Strategy 1: Find button by text content (includes "I understand")
                        for (const btn of document.querySelectorAll('button, div[role="button"], a[role="button"]')) {
                            const t = (btn.textContent||'').trim().toLowerCase();
                            if (!t || btn.offsetParent === null) continue;
                            if (t === 'continue' || t === 'allow' || t === 'lanjutkan' || t === 'lanjut'
                                || t === 'i understand' || t === 'saya mengerti' || t === 'accept' || t === 'agree'
                                || t === 'got it' || t === 'next'
                                || t.includes('continue') || t.includes('allow') || t.includes('understand')) {
                                btn.click(); return true;
                            }
                        }
                        // Strategy 2: Find submit buttons/inputs
                        const el = document.querySelector('#confirm') || document.querySelector('input[type="submit"]');
                        if (el) { el.click(); return true; }
                        return false;
                    }""")
                except Exception:
                    pass

            if popup_closed or tick > 5:
                try:
                    main_url = main_page.url if not main_page.is_closed() else ""
                except Exception:
                    main_url = ""
                if "gumloop.com" in main_url and "login" not in main_url and "accounts.google.com" not in main_url:
                    return True
        except Exception:
            pass
    return False


async def browser_login(email: str, password: str) -> tuple:
    """
    Full browser login flow via Camoufox.
    Returns (tokens_dict, manager, page) — caller is responsible for closing browser.
    On error returns ({"error": ...}, None, None).
    """
    from browserforge.fingerprints import Screen
    from camoufox.async_api import AsyncCamoufox

    log("Launching browser (visible)...")
    manager = AsyncCamoufox(
        headless=False,
        os="windows",
        block_webrtc=True,
        humanize=False,
        screen=Screen(max_width=1920, max_height=1080),
    )
    browser = await manager.__aenter__()
    page = await browser.new_page()
    page.set_default_timeout(30000)

    try:
        log("Opening Gumloop...")
        await page.goto("https://www.gumloop.com/home", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        log("Clicking Google sign-in...")
        popup_page = None
        popup_future = asyncio.get_event_loop().create_future()

        def on_popup(p):
            nonlocal popup_page
            popup_page = p
            if not popup_future.done():
                popup_future.set_result(p)

        page.context.on("page", on_popup)
        clicked = await click_google_login(page)
        if not clicked:
            await manager.__aexit__(None, None, None)
            return {"error": "Could not find Google sign-in button"}, None, None

        try:
            await asyncio.wait_for(popup_future, timeout=10)
        except asyncio.TimeoutError:
            pass

        google_page = popup_page or page
        if popup_page:
            await popup_page.wait_for_load_state("domcontentloaded", timeout=15000)
        await asyncio.sleep(2)

        log(f"Filling email: {email}")
        ok = await fill_google_email(google_page, email)
        if not ok:
            await manager.__aexit__(None, None, None)
            return {"error": "Failed to fill Google email"}, None, None

        log("Filling password...")
        ok = await fill_google_password(google_page, password)
        if not ok:
            await manager.__aexit__(None, None, None)
            return {"error": "Failed to fill Google password"}, None, None

        log("Handling consent & redirect...")
        redirected = await handle_consent(google_page, page)
        if not redirected:
            if "gumloop.com" not in page.url:
                await manager.__aexit__(None, None, None)
                return {"error": "Failed to redirect to Gumloop"}, None, None

        await asyncio.sleep(3)

        log("Extracting Firebase tokens...")
        try:
            await page.goto("https://www.gumloop.com/home", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(4)
        except Exception:
            pass

        tokens = None
        for attempt in range(8):
            tokens = await extract_firebase_tokens(page)
            if tokens and tokens.get("idToken"):
                break
            log(f"Token attempt {attempt+1}/8...")
            await asyncio.sleep(3)

        if not tokens or not tokens.get("idToken"):
            await manager.__aexit__(None, None, None)
            return {"error": "Failed to extract Firebase tokens"}, None, None

        log(f"Got tokens (uid={tokens.get('uid', '?')[:8]}...)")
        result = {
            "id_token": tokens["idToken"],
            "refresh_token": tokens.get("refreshToken", ""),
            "user_id": tokens.get("uid", ""),
            "email": tokens.get("email", email),
            "display_name": tokens.get("displayName", ""),
        }
        # Return browser alive — caller closes it after university flow
        return result, manager, page

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


async def navigate_to_university(page) -> bool:
    """Navigate to Gumloop University start page."""
    log("Navigating to Gumloop University...")
    try:
        await page.goto(UNIVERSITY_START, wait_until="domcontentloaded", timeout=30000)
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


async def answer_quiz(page, answer_index: int) -> bool:
    """
    Answer a quiz question on the current lesson page.
    answer_index: 1-indexed (1 = first option, 2 = second, 3 = third).
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

        # Click "Mark Complete" if present
        await asyncio.sleep(1)
        clicked_mark = await page.evaluate("""() => {
            const buttons = document.querySelectorAll('button, a');
            for (const btn of buttons) {
                const txt = (btn.textContent || '').trim().toLowerCase();
                if (txt.includes('mark complete') || txt.includes('mark as complete') || txt.includes('completed')) {
                    btn.click();
                    return txt;
                }
            }
            return null;
        }""")

        if clicked_mark:
            log(f"Clicked '{clicked_mark}'")
            await asyncio.sleep(2)

        return True

    except Exception as e:
        log(f"Answer quiz error: {e}")
        return False


async def navigate_to_lesson(page, lesson_index: int) -> bool:
    """Navigate to a specific lesson by index (0-based)."""
    if lesson_index >= len(LESSON_PATHS):
        log(f"Lesson {lesson_index + 1} out of range")
        return False

    path = LESSON_PATHS[lesson_index]
    url = f"{UNIVERSITY_BASE}{path}"
    log(f"Navigating to lesson {lesson_index + 1}: {url}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        return True
    except Exception as e:
        log(f"Lesson nav error: {e}")
        return False


async def click_lesson_sidebar(page, lesson_index: int) -> bool:
    """Try clicking lesson in the sidebar navigation."""
    try:
        clicked = await page.evaluate(f"""(idx) => {{
            // Find sidebar lesson links
            const links = document.querySelectorAll('nav a, aside a, [class*="sidebar"] a, [class*="lesson"] a');
            const lessonLinks = Array.from(links).filter(a => {{
                const href = a.getAttribute('href') || '';
                return href.includes('getting-started') || href.includes('lesson');
            }});
            if (lessonLinks.length > idx) {{
                lessonLinks[idx].click();
                return lessonLinks[idx].textContent.trim().substring(0, 40);
            }}
            return null;
        }}""", lesson_index)

        if clicked:
            log(f"Clicked sidebar: {clicked}")
            await asyncio.sleep(3)
            return True
        return False
    except Exception:
        return False


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

    # Navigate to university
    ok = await navigate_to_university(page)
    if not ok:
        log("FAILED: Could not navigate to university")
        return False

    # Handle OAuth authorize
    ok = await handle_oauth_authorize(page, context)
    if not ok:
        log("FAILED: Could not handle OAuth")
        return False

    log("OAuth complete. Starting lessons...")
    await asyncio.sleep(2)

    # Answer all 6 lessons
    for i in range(6):
        log(f"\n--- LESSON {i + 1}/{6} ---")

        # Navigate to lesson
        ok = await navigate_to_lesson(page, i)
        if not ok:
            # Try sidebar
            ok = await click_lesson_sidebar(page, i)
            if not ok:
                log(f"WARNING: Could not navigate to lesson {i + 1}")
                continue

        await asyncio.sleep(2)

        # Scroll to quiz section
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

        if answers and i < len(answers):
            # Use provided answer
            answer = answers[i]
            log(f"Using provided answer: {answer}")
            ok = await answer_quiz(page, answer)
            if not ok:
                log(f"WARNING: Failed to answer lesson {i + 1}")
        else:
            # Pause for manual input
            print()
            print(f"  ┌─ MANUAL INPUT NEEDED ─────────────────────")
            print(f"  │ Lesson {i + 1} quiz is visible in the browser.")
            print(f"  │ Enter the answer number (1, 2, or 3):")
            print(f"  └──────────────────────────────────────────────")
            try:
                user_answer = input(f"  Answer for lesson {i + 1}: ").strip()
                if user_answer.isdigit():
                    ok = await answer_quiz(page, int(user_answer))
                    if not ok:
                        log(f"WARNING: Failed to answer lesson {i + 1}")
                else:
                    log(f"Skipping lesson {i + 1} (invalid input: {user_answer})")
            except (EOFError, KeyboardInterrupt):
                log("Input cancelled, skipping remaining lessons")
                break

        await asyncio.sleep(2)

    # Wait for and claim credits modal
    log("\n--- WAITING FOR CREDITS CLAIM MODAL ---")
    try:
        # Wait for the modal to appear (max 30 seconds)
        modal_appeared = False
        for attempt in range(30):
            # Check if modal with "Claim Credits" button exists
            claim_button = await page.query_selector('button.credit-redeem-action')
            if claim_button:
                modal_appeared = True
                log("Credits claim modal detected!")
                break
            await asyncio.sleep(1)
        
        if modal_appeared:
            # Click the "Claim Credits" button
            log("Clicking 'Claim Credits' button...")
            await claim_button.click()
            await asyncio.sleep(2)
            log("Credits claimed successfully!")
        else:
            log("WARNING: Credits claim modal did not appear within 30 seconds")
    except Exception as e:
        log(f"WARNING: Failed to claim credits: {e}")

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

    # Step 1: Browser login (browser stays open for Phase 2)
    log("PHASE 1: Account & MCP Setup")
    log("STEP 1: Browser login...")
    login_result, browser_manager, browser_page = await browser_login(email, password)

    if "error" in login_result:
        log(f"FAILED: {login_result['error']}")
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
