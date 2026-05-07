#!/usr/bin/env python3
"""
Gumloop login via Google OAuth using Camoufox.
Extracts Firebase id_token + refresh_token after Google login.
Outputs JSON result to stdout.
"""

import argparse
import asyncio
import json
import os
import sys
import time


def emit(data: dict):
    try:
        print(json.dumps(data), flush=True)
    except BrokenPipeError:
        pass


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
                                // Firebase stores as {fbase_key: "...", value: {uid, email, stsTokenManager: {accessToken, refreshToken}}}
                                const val = item.value || item;
                                if (!val || !val.uid) continue;
                                
                                const stm = val.stsTokenManager || {};
                                const idToken = stm.accessToken || val.accessToken || val.spiAccessToken || '';
                                const refreshToken = stm.refreshToken || val.refreshToken || val.spiRefreshToken || '';
                                
                                if (idToken) {
                                    resolve({
                                        source: 'indexeddb',
                                        idToken: idToken,
                                        refreshToken: refreshToken,
                                        uid: val.uid || '',
                                        email: val.email || '',
                                        displayName: val.displayName || '',
                                        expirationTime: stm.expirationTime || val.expirationTime || 0,
                                    });
                                    return;
                                }
                            }
                            resolve(null);
                        };
                        getAll.onerror = () => resolve(null);
                    } catch(e) {
                        resolve(null);
                    }
                };
                request.onerror = () => resolve(null);
            });
        }""")
        return result
    except Exception as exc:
        emit({"type": "debug", "message": f"extract_firebase_tokens error: {exc}"})
        return None


async def extract_gummie_id(page) -> str | None:
    """Try to find a gummie_id from the page or API."""
    try:
        # Navigate to gumloop dashboard to find agents
        await page.goto("https://www.gumloop.com/gummies", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3)

        # Try to extract from page content or URL
        gummie_id = await page.evaluate("""() => {
            // Check URL
            const match = window.location.href.match(/gummies\\/([a-zA-Z0-9_-]+)/);
            if (match) return match[1];
            
            // Check links on page
            const links = document.querySelectorAll('a[href*="/gummies/"]');
            for (const link of links) {
                const m = link.href.match(/gummies\\/([a-zA-Z0-9_-]+)/);
                if (m && m[1] !== 'new') return m[1];
            }
            return null;
        }""")
        return gummie_id
    except Exception as exc:
        emit({"type": "debug", "message": f"extract_gummie_id error: {exc}"})
        return None


async def click_google_login(page) -> bool:
    """Click Get Started to open modal, then click Continue with Google."""
    # First click "Get Started" to open the auth modal
    for _ in range(5):
        try:
            opened = await page.evaluate("""() => {
                for (const btn of document.querySelectorAll('button')) {
                    const txt = (btn.textContent || '').trim().toLowerCase();
                    if (txt === 'get started' && btn.offsetParent !== null) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
            if opened:
                emit({"type": "debug", "message": "Clicked 'Get Started' button"})
                await asyncio.sleep(2)
                break
        except Exception:
            pass
        await asyncio.sleep(1)

    # Now click "Continue with Google"
    for _ in range(10):
        try:
            clicked = await page.evaluate("""() => {
                for (const btn of document.querySelectorAll('button, a, div[role="button"]')) {
                    const txt = (btn.textContent || '').trim().toLowerCase();
                    if (txt.includes('continue with google') && btn.offsetParent !== null) {
                        btn.click();
                        return true;
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
    """Fill Google email step."""
    try:
        await page.wait_for_selector("#identifierId", state="visible", timeout=10000)
        locator = page.locator("#identifierId").first
        await locator.click(force=True)
        await asyncio.sleep(0.2)
        await locator.press("Control+a")
        await locator.press("Backspace")
        await locator.press_sequentially(email, delay=60)
        await asyncio.sleep(0.5)

        # Click Next
        await page.evaluate("""() => {
            const btn = document.querySelector('#identifierNext button');
            if (btn) btn.click();
        }""")
        await asyncio.sleep(2)
        return True
    except Exception as exc:
        emit({"type": "debug", "message": f"fill_google_email error: {exc}"})
        return False


async def fill_google_password(page, password: str) -> bool:
    """Fill Google password step."""
    try:
        await page.wait_for_selector('input[name="Passwd"]', state="visible", timeout=10000)
        locator = page.locator('input[name="Passwd"]').first
        await locator.click(force=True)
        await asyncio.sleep(0.2)
        await locator.press("Control+a")
        await locator.press("Backspace")
        await locator.press_sequentially(password, delay=70)
        await asyncio.sleep(0.5)

        # Remember URL before clicking Next
        url_before = page.url

        # Click Next
        await page.evaluate("""() => {
            const btn = document.querySelector('#passwordNext button');
            if (btn) btn.click();
        }""")

        # Wait for navigation away from password page (consent, redirect, or error)
        for _ in range(15):
            await asyncio.sleep(1)
            try:
                current_url = page.url
                # URL changed — password step is done
                if current_url != url_before:
                    emit({"type": "debug", "message": f"Password step navigated to: {current_url[:80]}"})
                    await asyncio.sleep(1)
                    return True
                # Check for password error on same page
                has_error = await page.evaluate("""() => {
                    const err = document.querySelector('.LXRPh');
                    return err && err.offsetParent !== null ? err.textContent : null;
                }""")
                if has_error:
                    emit({"type": "debug", "message": f"Password error: {has_error}"})
                    return False
            except Exception:
                pass

        # Fallback: 15s passed, assume it worked (slow connection)
        emit({"type": "debug", "message": "Password step: no navigation after 15s, proceeding anyway"})
        return True
    except Exception as exc:
        emit({"type": "debug", "message": f"fill_google_password error: {exc}"})
        return False


async def _try_click_consent(page) -> str:
    """Try to click Continue/Allow/I understand on a Google consent page. Returns click result string."""
    try:
        return str(await page.evaluate("""() => {
            // Strategy 1: Find button by text content
            const keywords = ['continue', 'allow', 'lanjutkan', 'lanjut', 'i understand', 'accept', 'agree', 'got it', 'next'];
            for (const btn of document.querySelectorAll('button, div[role="button"], a[role="button"]')) {
                const t = (btn.textContent||'').trim().toLowerCase();
                if (!t || btn.offsetParent === null) continue;
                if (keywords.some(k => t.includes(k))) {
                    btn.click(); return 'text:' + t;
                }
            }
            // Strategy 2: Find submit buttons/inputs
            for (const el of document.querySelectorAll('input[type="submit"], input[type="button"]')) {
                const v = (el.value||'').toLowerCase();
                if (keywords.some(k => v.includes(k))) {
                    el.click(); return 'input:' + v;
                }
            }
            // Strategy 3: Find by ID patterns common in Google consent
            const byId = document.querySelector('#submit_approve_access')
                || document.querySelector('#submit_deny_access')?.parentElement?.querySelector('button:last-child')
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


async def _try_click_speedbump(page) -> bool:
    """Handle Google speedbump pages (workspacetermsofservice, etc).

    These pages have 'I understand', 'Accept', 'Agree' buttons.
    Returns True if a button was clicked.
    """
    try:
        result = await page.evaluate("""() => {
            // Look for common speedbump buttons
            for (const btn of document.querySelectorAll('button, div[role="button"], a[role="button"], input[type="submit"], input[type="button"]')) {
                const t = (btn.textContent || btn.value || '').trim().toLowerCase();
                if (!t || btn.offsetParent === null) continue;
                if (t === 'i understand' || t === 'accept' || t === 'agree'
                    || t === 'i agree' || t === 'continue' || t === 'confirm'
                    || t === 'saya mengerti' || t === 'setuju' || t === 'lanjutkan'
                    || t.includes('i understand') || t.includes('accept')
                    || t.includes('agree') || t.includes('confirm')) {
                    btn.click();
                    return t;
                }
            }
            // Try #confirm or submit inputs
            const el = document.querySelector('#confirm')
                || document.querySelector('input[type="submit"]')
                || document.querySelector('[data-idom-class*="confirm"]');
            if (el) { el.click(); return 'id:' + (el.id || 'submit'); }
            return '';
        }""")
        return bool(result)
    except Exception:
        return False


async def handle_consent_and_redirect(google_page, main_page) -> bool:
    """Handle Google consent/gaplustos/speedbump screens and wait for redirect.

    Polls BOTH the popup page AND the main page for consent screens.
    Google may show consent in either location depending on the OAuth flow.
    Returns True if successfully redirected to Gumloop.
    """
    clicked_consent = False
    clicked_gaplustos = False
    clicked_speedbump = False
    failed_click_count = 0  # Track consecutive failed click attempts
    _MAX_FAILED_CLICKS = 10  # Bail after this many failed attempts on same page

    for tick in range(60):
        await asyncio.sleep(1)
        try:
            # ── Gather URLs from both pages ─────────────────────────
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

            emit({"type": "debug", "message": f"consent tick={tick} popup={'closed' if popup_closed else gurl[:60]} main={main_url[:60]} clicked={clicked_consent}"})

            # ── Too many failed clicks — bail ───────────────────────
            if failed_click_count >= _MAX_FAILED_CLICKS:
                emit({"type": "debug", "message": f"consent: bailing after {failed_click_count} failed click attempts"})
                return False

            # ── Gaplustos on popup ──────────────────────────────────
            if "/speedbump/gaplustos" in gurl and not clicked_gaplustos:
                await google_page.evaluate("""() => {
                    const el = document.querySelector('#confirm') || document.querySelector('input[type="submit"]');
                    if (el) { el.click(); return; }
                    const kw = ['continue', 'i agree', 'accept', 'i understand', 'got it', 'next'];
                    for (const btn of document.querySelectorAll('button')) {
                        const t = (btn.textContent||'').toLowerCase();
                        if (kw.some(k => t.includes(k)) && btn.offsetParent!==null) { btn.click(); return; }
                    }
                }""")
                clicked_gaplustos = True
                failed_click_count = 0
                emit({"type": "progress", "provider": "gumloop", "step": "consent", "message": "Clicked gaplustos confirm"})
                await asyncio.sleep(3)
                continue

            # ── Speedbump pages (workspacetermsofservice, etc) ──────
            if not popup_closed and "/speedbump/" in gurl and not clicked_speedbump:
                ok = await _try_click_speedbump(google_page)
                if ok:
                    clicked_speedbump = True
                    failed_click_count = 0
                    emit({"type": "progress", "provider": "gumloop", "step": "consent", "message": "Clicked speedbump button"})
                    await asyncio.sleep(3)
                    continue
                else:
                    failed_click_count += 1

            # ── Speedbump on MAIN page ──────────────────────────────
            if "/speedbump/" in main_url and not clicked_speedbump:
                ok = await _try_click_speedbump(main_page)
                if ok:
                    clicked_speedbump = True
                    failed_click_count = 0
                    emit({"type": "progress", "provider": "gumloop", "step": "consent", "message": "Clicked speedbump on main page"})
                    await asyncio.sleep(3)
                    continue
                else:
                    failed_click_count += 1

            # ── Try consent click on POPUP page ─────────────────────
            if not popup_closed and "accounts.google.com" in gurl and not clicked_consent:
                result = await _try_click_consent(google_page)
                emit({"type": "debug", "message": f"consent click (popup): {result}"})
                if result and not result.startswith("no_"):
                    clicked_consent = True
                    failed_click_count = 0
                    emit({"type": "progress", "provider": "gumloop", "step": "consent", "message": f"Clicked consent on popup: {result}"})
                    await asyncio.sleep(3)
                    continue
                else:
                    failed_click_count += 1

            # ── Try consent click on MAIN page ──────────────────────
            if "accounts.google.com" in main_url and not clicked_consent:
                result = await _try_click_consent(main_page)
                emit({"type": "debug", "message": f"consent click (main): {result}"})
                if result and not result.startswith("no_"):
                    clicked_consent = True
                    failed_click_count = 0
                    emit({"type": "progress", "provider": "gumloop", "step": "consent", "message": f"Clicked consent on main page: {result}"})
                    await asyncio.sleep(3)
                    continue
                else:
                    failed_click_count += 1

            # ── Check for successful redirect to Gumloop ────────────
            if clicked_consent or clicked_speedbump or clicked_gaplustos or popup_closed:
                try:
                    main_url = main_page.url if not main_page.is_closed() else ""
                except Exception:
                    main_url = ""

                if "gumloop.com" in main_url and "login" not in main_url and "accounts.google.com" not in main_url:
                    emit({"type": "progress", "provider": "gumloop", "step": "redirect", "message": f"Redirected to Gumloop: {main_url[:60]}"})
                    return True

        except Exception as exc:
            emit({"type": "debug", "message": f"consent loop error: {exc}"})

    return False


async def run_login(email: str, password: str) -> dict:
    from app.browser import create_stealth_browser

    emit({"type": "progress", "provider": "gumloop", "step": "init", "message": "Launching browser..."})

    manager, browser, page = await create_stealth_browser(
        headless=os.getenv("BATCHER_CAMOUFOX_HEADLESS", "true").lower() == "true",
        timeout=20000,
        humanize=True,
    )

    try:
        # Step 1: Go to Gumloop login
        emit({"type": "progress", "provider": "gumloop", "step": "navigate", "message": "Opening Gumloop..."})
        await page.goto("https://www.gumloop.com/home", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)

        # Step 2: Click Google sign-in (may open popup)
        emit({"type": "progress", "provider": "gumloop", "step": "google_click", "message": "Clicking Google sign-in..."})

        # Listen for popup before clicking
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
            return {"success": False, "error": "Could not find Google sign-in button"}

        # Wait for popup or check if navigated in same page
        try:
            await asyncio.wait_for(popup_future, timeout=8)
        except asyncio.TimeoutError:
            pass

        # Determine which page has the Google login
        if popup_page:
            emit({"type": "debug", "message": f"Google login opened in popup: {popup_page.url[:80]}"})
            await popup_page.wait_for_load_state("domcontentloaded", timeout=10000)
            google_page = popup_page
        else:
            emit({"type": "debug", "message": f"Google login in same page: {page.url[:80]}"})
            google_page = page

        await asyncio.sleep(2)

        # Step 3: Fill Google email
        emit({"type": "progress", "provider": "gumloop", "step": "email", "message": f"Filling email: {email}"})
        ok = await fill_google_email(google_page, email)
        if not ok:
            return {"success": False, "error": "Failed to fill Google email"}

        # Step 4: Fill Google password
        emit({"type": "progress", "provider": "gumloop", "step": "password", "message": "Filling password..."})
        ok = await fill_google_password(google_page, password)
        if not ok:
            return {"success": False, "error": "Failed to fill Google password"}

        # Step 5: Handle consent screens + wait for redirect
        emit({"type": "progress", "provider": "gumloop", "step": "consent", "message": "Handling consent & waiting for redirect..."})
        redirected = await handle_consent_and_redirect(google_page, page)
        if not redirected:
            # Last resort: check if we're already on gumloop
            if "gumloop.com" not in page.url:
                return {"success": False, "error": "Failed to redirect to Gumloop after consent"}

        await asyncio.sleep(3)  # Let Firebase settle

        # Step 7: Extract Firebase tokens (from main page, not popup)
        emit({"type": "progress", "provider": "gumloop", "step": "extract_tokens", "message": "Extracting Firebase tokens..."})

        # Reload main page to ensure Firebase is initialized
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
            emit({"type": "debug", "message": f"Token extraction attempt {attempt+1}/8 - not found yet, waiting..."})
            await asyncio.sleep(3)

        if not tokens or not tokens.get("idToken"):
            return {"success": False, "error": "Failed to extract Firebase tokens"}

        emit({"type": "progress", "provider": "gumloop", "step": "tokens_ok", "message": f"Got tokens (uid={tokens.get('uid', '?')[:8]}...)"})

        # Step 8: Try to find gummie_id
        emit({"type": "progress", "provider": "gumloop", "step": "find_agent", "message": "Looking for default agent..."})
        gummie_id = await extract_gummie_id(page)

        return {
            "success": True,
            "credentials": {
                "id_token": tokens["idToken"],
                "refresh_token": tokens.get("refreshToken", ""),
                "user_id": tokens.get("uid", ""),
                "email": tokens.get("email", email),
                "display_name": tokens.get("displayName", ""),
                "gummie_id": gummie_id or "",
            },
        }

    except Exception as exc:
        return {"success": False, "error": str(exc)}
    finally:
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            pass


async def main(email: str, password: str):
    emit({"type": "progress", "provider": "gumloop", "step": "start", "message": f"Starting Gumloop login for {email}..."})

    result = await run_login(email, password)

    emit({
        "type": "result",
        "gumloop": result,
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    asyncio.run(main(args.email, args.password))
