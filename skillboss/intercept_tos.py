#!/usr/bin/env python3
"""
Intercept Google Workspace TOS speedbump page.

Opens browser, navigates to SkillBoss login. You do the Google login manually.
When it hits the "Welcome to your new account" page, script will try clicking
and log everything it finds on the page.

Usage:
    python skillboss/intercept_tos.py
"""

import asyncio
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BATCHER_CAMOUFOX_HEADLESS"] = "false"

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tos_debug.log")
_log_lines = []

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    _log_lines.append(line)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(_log_lines))


async def main():
    from browserforge.fingerprints import Screen
    from camoufox.async_api import AsyncCamoufox

    log("Opening browser... login manually, stop at the Welcome/TOS page.")
    log("Script will monitor and try to click 'I understand'.")

    manager = AsyncCamoufox(
        headless=False,
        os="windows",
        block_webrtc=True,
        humanize=False,
        screen=Screen(max_width=1920, max_height=1080),
        i_know_what_im_doing=True,
    )

    browser = await manager.__aenter__()
    context = browser.contexts[0] if browser.contexts else await browser.new_context()

    all_pages = []

    def on_page(page):
        all_pages.append(page)
        log(f"  [NEW PAGE] {page.url[:80]}")

    context.on("page", on_page)

    page = await context.new_page()
    page.set_default_timeout(60000)
    all_pages.append(page)

    await page.goto("https://www.skillboss.co/login", wait_until="domcontentloaded")
    log("  Login page loaded. Do Google login manually now.")

    try:
        while True:
            await asyncio.sleep(2)

            for pg in all_pages:
                try:
                    if pg.is_closed():
                        continue
                    url = pg.url
                except Exception:
                    continue

                if "speedbump" in url or "workspacetermsofservice" in url or "Welcome" in url:
                    log(f"[TOS PAGE DETECTED] {url[:100]}")

                    # Dump page info
                    info = await pg.evaluate("""() => {
                        const btns = [];
                        for (const el of document.querySelectorAll('button, input[type="submit"], div[role="button"], a[role="button"]')) {
                            btns.push({
                                tag: el.tagName,
                                text: (el.textContent || el.value || '').trim().substring(0, 50),
                                visible: el.offsetParent !== null,
                                disabled: el.disabled || false,
                                id: el.id || '',
                                classes: el.className || '',
                            });
                        }
                        return {
                            url: window.location.href,
                            title: document.title,
                            scrollHeight: document.body.scrollHeight,
                            clientHeight: document.documentElement.clientHeight,
                            buttons: btns,
                        };
                    }""")

                    log(f"Title: {info['title']}")
                    log(f"ScrollHeight: {info['scrollHeight']} ClientHeight: {info['clientHeight']}")
                    log(f"Buttons found: {len(info['buttons'])}")
                    for b in info['buttons']:
                        log(f"  [{b['tag']}] text='{b['text']}' visible={b['visible']} disabled={b['disabled']} id='{b['id']}' class='{b['classes'][:60]}'")

                    # Try scroll
                    log("Scrolling to bottom...")
                    await pg.evaluate("""() => {
                        window.scrollTo(0, document.body.scrollHeight);
                        document.querySelectorAll('[class*="scroll"], [style*="overflow"]').forEach(c => {
                            c.scrollTop = c.scrollHeight;
                        });
                    }""")
                    await asyncio.sleep(2)

                    # Check buttons again after scroll
                    info2 = await pg.evaluate("""() => {
                        const btns = [];
                        for (const el of document.querySelectorAll('button, input[type="submit"], div[role="button"], a[role="button"]')) {
                            btns.push({
                                tag: el.tagName,
                                text: (el.textContent || el.value || '').trim().substring(0, 80),
                                visible: el.offsetParent !== null,
                                disabled: el.disabled || false,
                                rect: el.getBoundingClientRect ? JSON.stringify({t:el.getBoundingClientRect().top,l:el.getBoundingClientRect().left,w:el.getBoundingClientRect().width,h:el.getBoundingClientRect().height}) : '',
                            });
                        }
                        return { buttons: btns };
                    }""")
                    log(f"After scroll - Buttons: {len(info2['buttons'])}")
                    for b in info2['buttons']:
                        log(f"  [{b['tag']}] text='{b['text']}' visible={b['visible']} disabled={b['disabled']} rect={b.get('rect','')}")

                    # Try clicking
                    log("Attempting click...")
                    clicked = await pg.evaluate("""() => {
                        const keywords = ['i understand', 'accept', 'agree', 'continue', 'got it', 'next'];
                        for (const btn of document.querySelectorAll('button, input[type="submit"], div[role="button"]')) {
                            const txt = (btn.textContent || btn.value || '').trim().toLowerCase();
                            if (keywords.some(k => txt.includes(k)) && btn.offsetParent !== null) {
                                btn.click();
                                return 'clicked: ' + txt;
                            }
                        }
                        // Try force click even if not visible
                        for (const btn of document.querySelectorAll('button, input[type="submit"]')) {
                            const txt = (btn.textContent || btn.value || '').trim().toLowerCase();
                            if (keywords.some(k => txt.includes(k))) {
                                btn.click();
                                return 'force-clicked (hidden): ' + txt;
                            }
                        }
                        return 'nothing to click';
                    }""")
                    log(f"Result: {clicked}")

                    if "clicked" in clicked:
                        log("Waiting 5s for navigation...")
                        await asyncio.sleep(5)
                        try:
                            log(f"New URL: {pg.url[:100]}")
                        except:
                            log("Page closed/navigated")

    except KeyboardInterrupt:
        pass

    await manager.__aexit__(None, None, None)
    log("Done.")
    log(f"Output saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
