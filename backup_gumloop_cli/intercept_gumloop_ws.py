#!/usr/bin/env python3
"""Intercept Gumloop WebSocket traffic to discover correct payload format."""
import asyncio
import json
import os

os.environ["BATCHER_CAMOUFOX_HEADLESS"] = "true"


async def main():
    from browserforge.fingerprints import Screen
    from camoufox.async_api import AsyncCamoufox

    manager = AsyncCamoufox(
        headless=True, os="windows", block_webrtc=True, humanize=False,
        screen=Screen(max_width=1920, max_height=1080),
    )
    browser = await manager.__aenter__()
    page = await browser.new_page()
    page.set_default_timeout(20000)

    ws_frames = []

    def on_ws(ws):
        ws.on("framesent", lambda p: ws_frames.append({"dir": "sent", "data": p.payload[:3000]}))
        ws.on("framereceived", lambda p: ws_frames.append({"dir": "recv", "data": p.payload[:500]}))

    page.on("websocket", on_ws)

    # Login
    await page.goto("https://www.gumloop.com/home", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(3)
    await page.evaluate("""() => { for (const b of document.querySelectorAll('button')) { if (b.textContent.trim().toLowerCase() === 'get started') { b.click(); return; } } }""")
    await asyncio.sleep(2)

    popup = None
    def on_popup(p):
        nonlocal popup
        popup = p
    page.context.on("page", on_popup)

    await page.evaluate("""() => { for (const b of document.querySelectorAll('button')) { if (b.textContent.trim().toLowerCase().includes('continue with google')) { b.click(); return; } } }""")
    await asyncio.sleep(5)

    if popup:
        await popup.wait_for_load_state("domcontentloaded", timeout=10000)
        try:
            await popup.wait_for_selector("#identifierId", state="visible", timeout=10000)
            loc = popup.locator("#identifierId").first
            await loc.click(force=True)
            await loc.press_sequentially("ksnuwo@gminol.com", delay=60)
            await asyncio.sleep(0.5)
            await popup.evaluate("""() => { const b = document.querySelector('#identifierNext button'); if(b) b.click(); }""")
            await asyncio.sleep(3)
            await popup.wait_for_selector('input[name="Passwd"]', state="visible", timeout=10000)
            loc2 = popup.locator('input[name="Passwd"]').first
            await loc2.click(force=True)
            await loc2.press_sequentially("qwertyui", delay=70)
            await asyncio.sleep(0.5)
            await popup.evaluate("""() => { const b = document.querySelector('#passwordNext button'); if(b) b.click(); }""")
            await asyncio.sleep(5)
            try:
                await popup.evaluate("""() => { const el = document.querySelector('#confirm'); if (el) el.click(); }""")
            except Exception:
                pass
        except Exception as e:
            print(f"Login error: {e}")

    await asyncio.sleep(8)
    print(f"URL: {page.url}")

    # Go to gummie chat
    await page.goto("https://www.gumloop.com/gummies/cSxSvDnv6YQasxzUKAfF4V", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(5)
    print(f"Gummie URL: {page.url}")

    # Find and fill chat input
    typed = await page.evaluate("""() => {
        const inputs = document.querySelectorAll('textarea, input[type="text"], div[contenteditable="true"]');
        for (const el of inputs) {
            if (el.offsetParent !== null) {
                el.focus();
                if (el.tagName === 'TEXTAREA') { el.value = 'Say hello'; }
                else { el.textContent = 'Say hello'; }
                el.dispatchEvent(new Event('input', {bubbles: true}));
                return el.tagName + ' ' + (el.placeholder || '').substring(0, 50);
            }
        }
        return 'none';
    }""")
    print(f"Input: {typed}")
    await asyncio.sleep(1)
    await page.keyboard.press("Enter")
    await asyncio.sleep(15)

    print(f"\n=== WS Frames: {len(ws_frames)} ===")
    for i, f in enumerate(ws_frames[:15]):
        print(f"[{f['dir']}] {f['data'][:800]}")
        print("---")

    await manager.__aexit__(None, None, None)


asyncio.run(main())
