#!/usr/bin/env python3
"""
SkillBoss auto-signup via Google OAuth (popup flow).

Flow (from HAR analysis):
1. Open skillboss.co/login
2. Click Google sign-in -> opens popup
3. In popup: fill Google email + pass
4. Handle Google consent
5. Popup redirects to /api/login/google/callback -> /login/success -> /console
6. GET /api/api-keys -> get key ID
7. GET /api/api-keys/{id}/reveal -> get full API key

Usage:
    python skillboss/register.py --email user@example.com --pass secret

Output (JSON to stdout):
    {"type": "result", "success": true, "api_key": "sk-...", "email": "..."}
"""

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def emit(data: dict):
    try:
        print(json.dumps(data), flush=True)
    except BrokenPipeError:
        pass


async def _fill_google_email(pg, email: str) -> bool:
    try:
        await pg.wait_for_selector("#identifierId", state="visible", timeout=10000)
        loc = pg.locator("#identifierId").first
        await loc.click(force=True)
        await asyncio.sleep(0.3)
        await loc.press_sequentially(email, delay=60)
        await asyncio.sleep(0.5)
        await pg.evaluate("() => { const b = document.querySelector('#identifierNext button'); if (b) b.click(); }")
        await asyncio.sleep(3)
        return True
    except Exception as e:
        emit({"type": "progress", "step": "error", "message": f"Email failed: {e}"})
        return False


async def _fill_google_pass(pg, secret: str) -> bool:
    try:
        await pg.wait_for_selector('input[name="Passwd"]', state="visible", timeout=10000)
        loc = pg.locator('input[name="Passwd"]').first
        await loc.click(force=True)
        await asyncio.sleep(0.3)
        await loc.press_sequentially(secret, delay=70)
        await asyncio.sleep(0.5)
        await pg.evaluate("() => { const b = document.querySelector('#passwordNext button'); if (b) b.click(); }")
        await asyncio.sleep(8)
        try:
            emit({"type": "progress", "step": "post_pass", "message": f"After pass: {pg.url[:60]}"})
        except Exception:
            emit({"type": "progress", "step": "post_pass", "message": "After pass: page navigated"})
        return True
    except Exception as e:
        emit({"type": "progress", "step": "error", "message": f"Pass failed: {e}"})
        return False


async def _handle_google_consent(pg) -> bool:
    try:
        # First scroll to bottom (Workspace TOS requires scroll before button enables)
        await pg.evaluate("""() => {
            window.scrollTo(0, document.body.scrollHeight);
            const containers = document.querySelectorAll('[class*="scroll"], [style*="overflow"]');
            containers.forEach(c => { c.scrollTop = c.scrollHeight; });
        }""")
        await asyncio.sleep(1)

        clicked = await pg.evaluate("""() => {
            const keywords = ['continue', 'allow', 'lanjut', 'i understand', 'accept', 'agree', 'got it', 'next'];
            for (const btn of document.querySelectorAll('button, div[role="button"], input[type="submit"]')) {
                const txt = (btn.textContent || btn.value || '').trim().toLowerCase();
                if (!txt || btn.offsetParent === null) continue;
                if (keywords.some(k => txt.includes(k))) {
                    btn.click(); return true;
                }
            }
            return false;
        }""")
        if clicked:
            await asyncio.sleep(3)
        return clicked
    except Exception:
        return False


async def run_signup(email: str, secret: str) -> dict:
    from app.browser import create_stealth_browser

    emit({"type": "progress", "step": "init", "message": "Launching browser..."})

    manager, browser, page = await create_stealth_browser(
        headless=os.getenv("BATCHER_CAMOUFOX_HEADLESS", "true").lower() == "true",
        timeout=120000,
        humanize=True,
    )
    page.set_default_navigation_timeout(120000)

    context = page.context
    popup_page = None
    popup_future = asyncio.get_event_loop().create_future()

    def on_page(new_page):
        nonlocal popup_page
        popup_page = new_page
        if not popup_future.done():
            popup_future.set_result(new_page)

    context.on("page", on_page)

    try:
        # Step 1: Navigate to login
        emit({"type": "progress", "step": "navigate", "message": "Opening SkillBoss login..."})
        await page.goto("https://www.skillboss.co/login", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Step 2: Click Google sign-in (opens popup)
        emit({"type": "progress", "step": "google_click", "message": "Clicking Google sign-in..."})
        clicked = False
        for _ in range(5):
            clicked = await page.evaluate("""() => {
                const sels = ['button[class*="google"]', 'a[class*="google"]', '[class*="googleButton"]'];
                for (const sel of sels) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) { el.click(); return true; }
                }
                for (const btn of document.querySelectorAll('button, a')) {
                    const txt = (btn.textContent || '').toLowerCase();
                    if (txt.includes('google') && btn.offsetParent !== null) { btn.click(); return true; }
                }
                return false;
            }""")
            if clicked:
                break
            await asyncio.sleep(1)

        if not clicked:
            return {"success": False, "error": "Could not find Google sign-in button"}

        # Step 3: Wait for popup
        emit({"type": "progress", "step": "popup_wait", "message": "Waiting for Google popup..."})
        try:
            popup = await asyncio.wait_for(popup_future, timeout=15)
        except asyncio.TimeoutError:
            popup = None

        auth_page = popup if popup else page

        if popup:
            popup.set_default_timeout(120000)
            popup.set_default_navigation_timeout(120000)
            await popup.wait_for_load_state("domcontentloaded", timeout=30000)
            emit({"type": "progress", "step": "popup_opened", "message": "Google popup opened"})
        else:
            for _ in range(10):
                if "accounts.google.com" in page.url:
                    break
                await asyncio.sleep(1)

        # Step 4: Fill email
        emit({"type": "progress", "step": "google_email", "message": "Filling email..."})
        if not await _fill_google_email(auth_page, email):
            return {"success": False, "error": "Failed to fill Google email"}

        # Step 5: Fill pass
        emit({"type": "progress", "step": "google_pass", "message": "Filling credentials..."})
        if not await _fill_google_pass(auth_page, secret):
            return {"success": False, "error": "Failed to fill Google credentials"}

        # Step 6+7: Handle consent/welcome/speedbump and wait for redirect
        emit({"type": "progress", "step": "consent", "message": "Handling consent/welcome..."})
        target_page = page
        for tick in range(60):
            # Check main page
            try:
                main_url = page.url
                if "console" in main_url or "login/success" in main_url:
                    target_page = page
                    break
                if "skillboss" in main_url and "login" not in main_url:
                    target_page = page
                    break
            except Exception:
                main_url = ""

            # Check popup
            popup_closed = False
            if popup:
                try:
                    popup_closed = popup.is_closed()
                    if not popup_closed:
                        pu = popup.url
                        if "console" in pu or "login/success" in pu:
                            target_page = popup
                            break
                        if "skillboss" in pu and "login" not in pu:
                            target_page = popup
                            break
                except Exception:
                    popup_closed = True

            # Popup closed = session transferred to main
            if popup and popup_closed:
                target_page = page
                await asyncio.sleep(2)
                break

            # Click consent/welcome/speedbump on all available pages
            for pg_try in [auth_page, popup, page]:
                if pg_try is None:
                    continue
                try:
                    if pg_try.is_closed():
                        continue
                    pg_url = pg_try.url
                    if "google.com" in pg_url:
                        if tick % 5 == 0:
                            emit({"type": "progress", "step": "consent_try", "message": f"tick={tick} url={pg_url[:60]}"})
                        result = await _handle_google_consent(pg_try)
                        if result:
                            emit({"type": "progress", "step": "consent_clicked", "message": f"Clicked on {pg_url[:60]}"})
                            await asyncio.sleep(3)
                except Exception as exc:
                    if tick % 10 == 0:
                        emit({"type": "progress", "step": "consent_error", "message": f"Error: {exc}"})
                    continue

            await asyncio.sleep(1)

        # Step 8: Go to /console
        current = target_page.url
        if "console" not in current:
            emit({"type": "progress", "step": "console_nav", "message": "Navigating to console..."})
            await target_page.goto("https://www.skillboss.co/console", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)

        # Step 9: GET /api/api-keys
        emit({"type": "progress", "step": "get_key", "message": "Fetching API key..."})
        keys_result = await target_page.evaluate("""async () => {
            try {
                const r = await fetch('/api/api-keys', { credentials: 'include' });
                return await r.json();
            } catch (e) { return { error: String(e) }; }
        }""")

        if not keys_result or "error" in keys_result:
            if target_page != page:
                keys_result = await page.evaluate("""async () => {
                    try {
                        const r = await fetch('https://www.skillboss.co/api/api-keys', { credentials: 'include' });
                        return await r.json();
                    } catch (e) { return { error: String(e) }; }
                }""")

        keys = keys_result.get("keys", [])
        if not keys:
            return {"success": False, "error": f"No API keys found: {json.dumps(keys_result)[:200]}"}

        key_id = keys[0].get("id", "")
        if not key_id:
            return {"success": False, "error": "Key ID missing"}

        # Step 10: GET /api/api-keys/{id}/reveal
        reveal_result = await target_page.evaluate("""async (keyId) => {
            try {
                const r = await fetch('/api/api-keys/' + keyId + '/reveal', { credentials: 'include' });
                return await r.json();
            } catch (e) { return { error: String(e) }; }
        }""", key_id)

        api_key = reveal_result.get("api_key", "")
        if not api_key:
            return {"success": False, "error": f"Could not reveal key: {json.dumps(reveal_result)[:200]}"}

        emit({"type": "progress", "step": "done", "message": f"Got key: {api_key[:25]}..."})
        return {"success": True, "api_key": api_key, "email": email}

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await manager.__aexit__(None, None, None)


async def main():
    parser = argparse.ArgumentParser(description="SkillBoss auto-signup via Google")
    parser.add_argument("--email", required=True)
    parser.add_argument("--secret", required=True, help="Google account credential")
    args = parser.parse_args()

    result = await run_signup(args.email, args.secret)
    emit({"type": "result", **result})
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    asyncio.run(main())
