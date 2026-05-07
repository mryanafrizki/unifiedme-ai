#!/usr/bin/env python3
"""Test SkillBoss signup with plain Playwright Chromium (no camoufox)."""

import asyncio
import json
import time

EMAIL = "TahaputraAdniputri@ggmel.com"
SECRET = "qwertyui"

async def main():
    from playwright.async_api import async_playwright

    print("Launching Chromium...")
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context()

    all_pages = []

    def on_page(pg):
        all_pages.append(pg)
        print(f"[NEW PAGE] {pg.url[:80]}")

    context.on("page", on_page)

    page = await context.new_page()
    page.set_default_timeout(120000)
    all_pages.append(page)

    # Step 1: Go to SkillBoss login
    print("Opening SkillBoss login...")
    await page.goto("https://www.skillboss.co/login", wait_until="domcontentloaded")
    await asyncio.sleep(3)

    # Step 2: Click Google
    print("Clicking Google sign-in...")
    await page.evaluate("""() => {
        for (const btn of document.querySelectorAll('button, a')) {
            if ((btn.textContent || '').toLowerCase().includes('google') && btn.offsetParent !== null) {
                btn.click(); return true;
            }
        }
        return false;
    }""")
    await asyncio.sleep(5)

    # Find auth page (the Google tab)
    auth_page = None
    for pg in all_pages:
        try:
            if "accounts.google.com" in pg.url:
                auth_page = pg
                break
        except:
            pass

    if not auth_page:
        auth_page = all_pages[-1] if len(all_pages) > 1 else page
    print(f"Auth page: {auth_page.url[:80]}")

    # Step 3: Fill email
    print("Filling email...")
    await auth_page.wait_for_selector("#identifierId", state="visible", timeout=15000)
    await auth_page.locator("#identifierId").first.click(force=True)
    await asyncio.sleep(0.3)
    await auth_page.locator("#identifierId").first.press_sequentially(EMAIL, delay=60)
    await asyncio.sleep(0.5)
    await auth_page.evaluate("() => { const b = document.querySelector('#identifierNext button'); if (b) b.click(); }")
    await asyncio.sleep(4)

    # Step 4: Fill pass
    print("Filling credentials...")
    await auth_page.wait_for_selector('input[name="Passwd"]', state="visible", timeout=15000)
    await auth_page.locator('input[name="Passwd"]').first.click(force=True)
    await asyncio.sleep(0.3)
    await auth_page.locator('input[name="Passwd"]').first.press_sequentially(SECRET, delay=70)
    await asyncio.sleep(0.5)
    await auth_page.evaluate("() => { const b = document.querySelector('#passwordNext button'); if (b) b.click(); }")
    print("Credentials submitted, waiting 8s...")
    await asyncio.sleep(8)

    # Step 5: Monitor and click consent/TOS
    print(f"After credentials: {auth_page.url[:80]}")
    print("\nStarting consent loop (60 ticks)...")

    for tick in range(60):
        # Check all pages
        for pg in all_pages:
            try:
                u = pg.url
            except:
                continue

            # Found SkillBoss?
            if "console" in u or "login/success" in u or ("skillboss" in u and "login" not in u):
                print(f"\n[DONE] Landed on: {u[:100]}")

                # Get API key
                await asyncio.sleep(3)
                try:
                    await pg.goto("https://www.skillboss.co/console", wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(3)
                    keys = await pg.evaluate("""async () => {
                        try {
                            const r = await fetch('/api/api-keys', { credentials: 'include' });
                            return await r.json();
                        } catch (e) { return { error: String(e) }; }
                    }""")
                    print(f"Keys: {json.dumps(keys)[:200]}")

                    key_list = keys.get("keys", [])
                    if key_list:
                        key_id = key_list[0].get("id", "")
                        reveal = await pg.evaluate("""async (kid) => {
                            try {
                                const r = await fetch('/api/api-keys/' + kid + '/reveal', { credentials: 'include' });
                                return await r.json();
                            } catch (e) { return { error: String(e) }; }
                        }""", key_id)
                        print(f"API Key: {reveal.get('api_key', 'NOT FOUND')}")
                except Exception as e:
                    print(f"Key fetch error: {e}")

                await browser.close()
                await pw.stop()
                return

            # On Google? Try clicking consent
            if "google.com" in u:
                if tick % 3 == 0:
                    print(f"  tick={tick} google page: {u[:70]}")

                # Scroll first
                try:
                    await pg.evaluate("""() => {
                        window.scrollTo(0, document.body.scrollHeight);
                        document.querySelectorAll('[class*="scroll"], [style*="overflow"]').forEach(c => {
                            c.scrollTop = c.scrollHeight;
                        });
                    }""")
                except:
                    pass

                # Click
                try:
                    clicked = await pg.evaluate("""() => {
                        const keywords = ['i understand', 'accept', 'agree', 'continue', 'got it', 'next', 'allow'];
                        for (const btn of document.querySelectorAll('button, input[type="submit"], div[role="button"]')) {
                            const txt = (btn.textContent || btn.value || '').trim().toLowerCase();
                            if (keywords.some(k => txt.includes(k)) && btn.offsetParent !== null) {
                                btn.click();
                                return 'clicked: ' + txt;
                            }
                        }
                        return '';
                    }""")
                    if clicked:
                        print(f"  [CLICK] {clicked}")
                        await asyncio.sleep(5)
                except Exception as e:
                    if tick % 10 == 0:
                        print(f"  click error: {e}")

        await asyncio.sleep(1)

    print("\nTimeout - did not reach SkillBoss console")
    for pg in all_pages:
        try:
            print(f"  Page: {pg.url[:100]}")
        except:
            pass

    await browser.close()
    await pw.stop()


asyncio.run(main())
