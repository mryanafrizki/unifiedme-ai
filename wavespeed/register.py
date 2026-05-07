#!/usr/bin/env python3
"""
WaveSpeed AI account registration via Google OAuth using Camoufox.
Non-headless mode for debugging — you can watch the browser.

Usage:
  python register.py --email user@gmail.com --password pass123
  python register.py --email user@gmail.com --password pass123 --headless
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time

# Add parent auth dir to path for shared utilities
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "auth"))


def emit(data: dict):
    try:
        print(json.dumps(data, ensure_ascii=False), flush=True)
    except UnicodeEncodeError:
        # Fallback: ASCII-safe encoding for Windows cp1252 console
        print(json.dumps(data, ensure_ascii=True), flush=True)


async def fill_google_email(page, email: str) -> bool:
    """Fill Google email step. Handles both fresh login and 'Choose an account' screens."""
    try:
        # Wait for either email input OR account picker
        for _ in range(15):
            has_email_input = await page.evaluate("""() => {
                const el = document.querySelector('#identifierId');
                return el && el.offsetParent !== null;
            }""")
            if has_email_input:
                break

            # Check for "Choose an account" / "Use another account" screen
            clicked_another = await page.evaluate("""() => {
                // Look for "Use another account" button
                for (const el of document.querySelectorAll('li, div[role="link"], div[data-identifier]')) {
                    const txt = (el.textContent || '').toLowerCase();
                    if (txt.includes('use another account') || txt.includes('gunakan akun lain')) {
                        el.click(); return 'use_another';
                    }
                }
                // Also check for the specific email in account list and click it
                return null;
            }""")
            if clicked_another:
                emit({"type": "debug", "step": "email", "message": f"Clicked: {clicked_another}"})
                await asyncio.sleep(2)
                continue

            # Check if already past email (e.g. password step visible)
            at_password = await page.evaluate("""() => {
                const pw = document.querySelector('input[name="Passwd"]');
                return pw && pw.offsetParent !== null;
            }""")
            if at_password:
                return True  # Skip email, already at password

            await asyncio.sleep(1)

        # Now fill the email input
        try:
            await page.wait_for_selector("#identifierId", state="visible", timeout=5000)
        except Exception:
            emit({"type": "debug", "step": "email", "message": "identifierId not found, checking alternatives"})
            # Try input[type=email] as fallback
            has_alt = await page.evaluate("""() => {
                const el = document.querySelector('input[type="email"]');
                return el && el.offsetParent !== null;
            }""")
            if not has_alt:
                return False

        loc = page.locator("#identifierId, input[type='email']").first
        await loc.click(force=True)
        await asyncio.sleep(0.3)
        await loc.press("Control+a")
        await loc.press("Backspace")
        await loc.press_sequentially(email, delay=50)
        await asyncio.sleep(0.5)

        # Click Next
        clicked = await page.evaluate("""() => {
            const btn = document.querySelector('#identifierNext button');
            if (btn) { btn.click(); return true; }
            return false;
        }""")
        if not clicked:
            await loc.press("Enter")

        # Wait for transition (email field gone or password field visible)
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


async def fill_google_password(page, password: str) -> bool:
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

        clicked = await page.evaluate("""() => {
            const btn = document.querySelector('#passwordNext button');
            if (btn) { btn.click(); return true; }
            return false;
        }""")
        if not clicked:
            await loc.press("Enter")

        # Wait longer and check multiple conditions
        for _ in range(20):
            await asyncio.sleep(1)
            try:
                url = page.url
                # If we left Google auth, password was accepted
                if "accounts.google.com" not in url:
                    return True
                # If password field is gone
                pw_visible = await page.evaluate("""() => {
                    const pw = document.querySelector('input[name="Passwd"]');
                    return pw && pw.offsetParent !== null;
                }""")
                if not pw_visible:
                    return True
                # Check for error messages
                has_error = await page.evaluate("""() => {
                    const text = (document.body?.innerText || '').toLowerCase();
                    return text.includes('wrong password') || text.includes('incorrect');
                }""")
                if has_error:
                    emit({"type": "error", "step": "password", "message": "Wrong password detected"})
                    return False
            except Exception:
                pass
        return True  # Assume success after 20s
    except Exception as e:
        emit({"type": "error", "step": "password", "message": str(e)})
        return False


async def handle_google_consent(page) -> None:
    """Handle consent/gaplustos screens.

    Clicks Continue/Allow ONCE per distinct URL, then waits for navigation.
    Prevents spam-clicking that causes infinite loading.
    """
    clicked_urls: set = set()

    for _ in range(30):
        await asyncio.sleep(1)
        try:
            url = page.url

            # If we're back on wavespeed.ai, we're done
            if "wavespeed.ai" in url and "sign-in" not in url:
                break

            # Gaplustos (Terms of Service)
            if "/speedbump/gaplustos" in url and url not in clicked_urls:
                await page.evaluate("""() => {
                    const el = document.querySelector('#confirm') || document.querySelector('input[type="submit"]');
                    if (el) el.click();
                }""")
                clicked_urls.add(url)
                emit({"type": "progress", "step": "consent", "message": "Accepted gaplustos"})
                await asyncio.sleep(3)
                continue

            # OAuth consent / Welcome page (Continue/Allow/I understand button)
            if "accounts.google.com" in url:
                if url not in clicked_urls:
                    await page.evaluate("""() => {
                        const keywords = ['continue', 'allow', 'lanjut', 'i understand', 'accept', 'agree', 'got it', 'next'];
                        for (const btn of document.querySelectorAll('button, div[role="button"], input[type="submit"]')) {
                            const txt = (btn.textContent || btn.value || '').trim().toLowerCase();
                            if (keywords.some(k => txt.includes(k)) && btn.offsetParent !== null) {
                                btn.click(); return;
                            }
                        }
                    }""")
                    clicked_urls.add(url)
                    emit({"type": "progress", "step": "consent", "message": "Clicked consent/welcome button"})
                # Wait for navigation after click — don't click again
                await asyncio.sleep(3)
                continue

        except Exception:
            pass


async def click_google_signin(page) -> bool:
    """Find and click the Google sign-in button on WaveSpeed."""
    for attempt in range(15):
        try:
            url = page.url
            emit({"type": "debug", "step": "find_google_btn", "url": url[:80], "attempt": attempt})

            # If already on Google, we're good
            if "accounts.google.com" in url:
                return True

            clicked = await page.evaluate("""() => {
                // Look for Google sign-in button
                const selectors = [
                    'button:has(img[alt*="Google"])',
                    'button:has(svg[data-testid="GoogleIcon"])',
                    '[data-provider="google"]',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) { el.click(); return 'selector:' + sel; }
                }
                // Text-based search
                for (const el of document.querySelectorAll('button, a, div[role="button"]')) {
                    const txt = (el.textContent || '').toLowerCase();
                    if ((txt.includes('google') || txt.includes('sign in with google') || txt.includes('continue with google')) && el.offsetParent !== null) {
                        el.click(); return 'text:' + txt.trim().substring(0, 40);
                    }
                }
                return null;
            }""")

            if clicked:
                emit({"type": "progress", "step": "google_click", "message": f"Clicked: {clicked}"})
                await asyncio.sleep(3)
                return True

        except Exception as e:
            emit({"type": "debug", "step": "find_google_btn_error", "message": str(e)})

        await asyncio.sleep(1)

    return False


async def extract_account_info(page) -> dict:
    """Extract account info after successful login."""
    try:
        url = page.url
        info = {"url": url}

        # Try to get API key or token from the page
        data = await page.evaluate("""() => {
            const result = {};
            // Check localStorage
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                const val = localStorage.getItem(key);
                if (key.toLowerCase().includes('token') || key.toLowerCase().includes('key') || key.toLowerCase().includes('auth') || key.toLowerCase().includes('session')) {
                    result[key] = val ? val.substring(0, 200) : '';
                }
            }
            // Check cookies
            result['_cookies'] = document.cookie.substring(0, 500);
            return result;
        }""")
        info["storage"] = data

        # Get page title and user info
        info["title"] = await page.title()
        info["body_snippet"] = await page.evaluate("() => document.body?.innerText?.substring(0, 300) || ''")

        return info
    except Exception as e:
        return {"error": str(e)}


async def run(email: str, password: str, headless: bool = False, proxy_url: str = ""):
    from app.browser import create_stealth_browser

    proxy_msg = f" via proxy {proxy_url}" if proxy_url else ""
    emit({"type": "progress", "step": "init", "message": f"Launching Camoufox ({'headless' if headless else 'visible'}){proxy_msg}..."})

    # Build proxy config
    proxy_cfg = None
    if proxy_url:
        proxy_cfg = {"server": proxy_url}

    manager, browser, page = await create_stealth_browser(
        proxy=proxy_cfg,
        headless=headless,
        timeout=20000,
        humanize=True,
    )

    try:
        # Step 1: Navigate to WaveSpeed sign-in
        emit({"type": "progress", "step": "navigate", "message": "Opening WaveSpeed sign-in..."})
        await page.goto("https://wavespeed.ai/sign-in", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)

        emit({"type": "debug", "step": "page_loaded", "url": page.url, "title": await page.title()})

        # Step 2: Find and click Google sign-in
        emit({"type": "progress", "step": "google_click", "message": "Looking for Google sign-in button..."})

        # Listen for popup
        popup_page = None
        def on_popup(p):
            nonlocal popup_page
            popup_page = p
        page.context.on("page", on_popup)

        clicked = await click_google_signin(page)
        if not clicked:
            emit({"type": "error", "step": "google_click", "message": "Could not find Google sign-in button"})
            return {"success": False, "error": "Google sign-in button not found"}

        # Wait for either popup or same-page navigation to Google
        await asyncio.sleep(5)

        if popup_page:
            emit({"type": "debug", "step": "popup", "url": popup_page.url[:80]})
            await popup_page.wait_for_load_state("domcontentloaded", timeout=10000)
            google_page = popup_page
        else:
            # Same-page navigation — wait for Google URL
            for _ in range(15):
                if "accounts.google.com" in page.url:
                    break
                await asyncio.sleep(1)
            google_page = page
            emit({"type": "debug", "step": "same_page_google", "url": page.url[:80]})

        await asyncio.sleep(2)

        # Step 3: Fill Google email
        emit({"type": "progress", "step": "email", "message": f"Filling email: {email}"})
        ok = await fill_google_email(google_page, email)
        if not ok:
            return {"success": False, "error": "Failed to fill Google email"}

        await asyncio.sleep(1)

        # Step 4: Fill Google password
        emit({"type": "progress", "step": "password", "message": "Filling password..."})
        ok = await fill_google_password(google_page, password)
        if not ok:
            return {"success": False, "error": "Failed to fill Google password"}

        # Step 5+6: Handle consent screens AND wait for redirect to WaveSpeed
        emit({"type": "progress", "step": "consent", "message": "Handling consent + waiting for redirect..."})
        active_page = google_page if popup_page else page
        clicked_consent: set = set()
        landed = False

        for tick in range(60):
            await asyncio.sleep(1)
            try:
                # Use page.url (Playwright property) — more reliable than JS eval
                url = page.url

                # Handle Google screens FIRST (before checking wavespeed)

                # Gaplustos (Terms of Service) — click confirm/continue ONCE
                if "/speedbump/gaplustos" in url:
                    if "gaplustos" not in clicked_consent:
                        await page.evaluate("""() => {
                            // Try #confirm first (old gaplustos)
                            let el = document.querySelector('#confirm') || document.querySelector('input[type="submit"]');
                            if (el) { el.click(); return; }
                            // Try Continue/I agree buttons
                            for (const btn of document.querySelectorAll('button, div[role="button"]')) {
                                const txt = (btn.textContent || '').trim().toLowerCase();
                                if ((txt === 'continue' || txt === 'i agree' || txt.includes('accept') || txt.includes('lanjut')) && btn.offsetParent !== null) {
                                    btn.click(); return;
                                }
                            }
                        }""")
                        clicked_consent.add("gaplustos")
                        emit({"type": "progress", "step": "consent", "message": "Clicked confirm on gaplustos"})
                    await asyncio.sleep(5)
                    continue

                # OAuth consent / Welcome (Continue/Allow/I understand) — click ONCE
                if "accounts.google.com" in url:
                    if "oauth_consent" not in clicked_consent:
                        await page.evaluate("""() => {
                            const keywords = ['continue', 'allow', 'lanjut', 'i understand', 'accept', 'agree', 'got it', 'next'];
                            for (const btn of document.querySelectorAll('button, div[role="button"], input[type="submit"]')) {
                                const txt = (btn.textContent || btn.value || '').trim().toLowerCase();
                                if (keywords.some(k => txt.includes(k)) && btn.offsetParent !== null) {
                                    btn.click(); return;
                                }
                            }
                        }""")
                        clicked_consent.add("oauth_consent")
                        emit({"type": "progress", "step": "consent", "message": "Clicked consent/welcome"})
                    await asyncio.sleep(3)
                    continue

                # Still on Google but not consent/gaplustos — just wait
                if "accounts.google.com" in url:
                    if tick % 5 == 0:
                        emit({"type": "debug", "step": "waiting", "url": url[:80]})
                    continue

                # Done: landed on wavespeed.ai dashboard (not sign-in)
                if "wavespeed.ai" in url and "sign-in" not in url:
                    await asyncio.sleep(2)
                    url2 = page.url
                    if "wavespeed.ai" in url2 and "sign-in" not in url2:
                        emit({"type": "progress", "step": "redirected", "message": f"Landed on WaveSpeed: {url2[:100]}"})
                        landed = True
                        break

            except Exception:
                pass

        if not landed:
            emit({"type": "debug", "step": "redirect_timeout", "message": f"Never reached wavespeed.ai, last url: {page.url[:100]}"})

        await asyncio.sleep(3)

        # Step 7: Navigate to API keys page and create a key
        emit({"type": "progress", "step": "create_key", "message": "Creating API key..."})
        for nav_attempt in range(3):
            try:
                await page.goto("https://wavespeed.ai/accesskey", wait_until="domcontentloaded", timeout=30000)
                break
            except Exception as nav_err:
                emit({"type": "debug", "step": "nav_retry", "message": f"Attempt {nav_attempt+1}/3: {nav_err}"})
                if nav_attempt == 2:
                    emit({"type": "error", "step": "create_key", "message": f"Failed to navigate to accesskey page after 3 attempts"})
                await asyncio.sleep(3)
        await asyncio.sleep(5)

        # Click "Create Key" button
        emit({"type": "debug", "step": "click_create", "message": "Clicking Create Key..."})
        await page.evaluate("""() => {
            for (const b of document.querySelectorAll('button, a, div[role="button"]')) {
                const t = (b.textContent||'').trim().toLowerCase();
                if (t.includes('create key') || t.includes('create') || t.includes('new key')) {
                    if (b.offsetParent !== null) { b.click(); return true; }
                }
            }
            return false;
        }""")
        await asyncio.sleep(3)

        # Check what appeared — dump visible elements
        page_state = await page.evaluate("""() => {
            const buttons = Array.from(document.querySelectorAll('button')).filter(b => b.offsetParent !== null).map(b => b.textContent.trim().substring(0, 40));
            const inputs = Array.from(document.querySelectorAll('input')).filter(i => i.offsetParent !== null).map(i => ({type: i.type, placeholder: i.placeholder || '', name: i.name || ''}));
            const modals = Array.from(document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="dialog"], [class*="overlay"]')).map(m => m.innerText.substring(0, 200));
            return {buttons, inputs, modals, body: (document.body?.innerText || '').substring(0, 600)};
        }""")
        emit({"type": "debug", "step": "after_create_click", "buttons": page_state["buttons"], "inputs": page_state["inputs"], "modals": page_state["modals"]})

        # Fill the "Enter key name" input
        try:
            name_input = page.locator('input[placeholder="Enter key name"]').first
            if await name_input.count() > 0:
                await name_input.click(force=True)
                _adjectives = ["fast", "main", "dev", "prod", "test", "local", "cloud", "app", "my", "lab"]
                _nouns = ["server", "worker", "agent", "bot", "runner", "node", "service", "client", "hub", "api"]
                key_name = f"{random.choice(_adjectives)}-{random.choice(_nouns)}-{random.randint(10,99)}"
                await name_input.fill(key_name)
                emit({"type": "debug", "step": "filled_name", "message": "Filled key name"})
                await asyncio.sleep(1)

                # Click "Create Key" button (the one in the form, not the page button)
                await page.evaluate("""() => {
                    const buttons = document.querySelectorAll('button');
                    for (const b of buttons) {
                        const t = (b.textContent||'').trim().toLowerCase();
                        if (t === 'create key' && b.offsetParent !== null) {
                            b.click(); return t;
                        }
                    }
                }""")
                await asyncio.sleep(5)
                emit({"type": "debug", "step": "submitted", "message": "Clicked Create Key submit"})
        except Exception as e:
            emit({"type": "debug", "step": "fill_error", "message": str(e)})

        # Extract API key — try multiple patterns
        api_key = await page.evaluate("""() => {
            // Check all text on page for key patterns
            const allText = document.body.innerText || '';
            const patterns = [
                /wsk_[a-zA-Z0-9]{20,}/,
                /sk-[a-zA-Z0-9_-]{20,}/,
                /ws_[a-zA-Z0-9]{20,}/,
                /[a-f0-9]{32,}/
            ];
            for (const p of patterns) {
                const m = allText.match(p);
                if (m) return m[0];
            }
            // Check inputs and code elements
            for (const el of document.querySelectorAll('input, code, pre, span, td, div')) {
                const val = (el.value || el.textContent || '').trim();
                if (val.length >= 30 && val.length <= 100 && !val.includes(' ') && !val.includes('<')) return val;
            }
            return null;
        }""")

        if api_key:
            emit({"type": "progress", "step": "key_created", "message": f"API key: {api_key[:20]}..."})
        else:
            emit({"type": "debug", "step": "key_failed", "message": "Could not extract API key from page"})

        # Step 8: Extract account info
        emit({"type": "progress", "step": "extract", "message": "Extracting account info..."})
        info = await extract_account_info(page)

        emit({"type": "progress", "step": "done", "message": "Registration complete!"})

        return {
            "success": True,
            "email": email,
            "url": page.url,
            "api_key": api_key or "",
            "info": info,
        }

    except Exception as e:
        emit({"type": "error", "step": "fatal", "message": str(e)})
        try:
            await page.screenshot(path="/tmp/wavespeed_error.png")
        except Exception:
            pass
        return {"success": False, "error": str(e)}

    finally:
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            pass


async def main():
    parser = argparse.ArgumentParser(description="WaveSpeed AI registration via Google OAuth")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--headless", action="store_true", default=False)
    parser.add_argument("--proxy", default="", help="Proxy URL (http:// or socks5://)")
    args = parser.parse_args()

    result = await run(args.email, args.password, headless=args.headless, proxy_url=args.proxy)
    emit({"type": "result", **result})


if __name__ == "__main__":
    asyncio.run(main())
