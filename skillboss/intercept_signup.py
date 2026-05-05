#!/usr/bin/env python3
"""
Intercept SkillBoss signup flow via Google OAuth (popup mode).

Opens a non-headless camoufox browser, captures ALL network traffic,
handles Google OAuth popup, and saves HAR.

You click through manually. Script captures everything including popup.

Usage:
    python skillboss/intercept_signup.py

Press Ctrl+C when done.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["BATCHER_CAMOUFOX_HEADLESS"] = "false"

captured: list[dict] = []
SKIP_EXTENSIONS = [".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2", ".ttf", ".ico", ".map"]


def _should_skip(url: str) -> bool:
    return any(url.lower().split("?")[0].endswith(ext) for ext in SKIP_EXTENSIONS)


def _is_interesting(url: str) -> bool:
    keywords = ["auth", "token", "session", "login", "signup", "register",
                "api-key", "apikey", "user", "account", "credential",
                "google", "oauth", "callback", "console", "skillboss"]
    return any(kw in url.lower() for kw in keywords)


async def main():
    from browserforge.fingerprints import Screen
    from camoufox.async_api import AsyncCamoufox

    print("=" * 60)
    print("  SkillBoss Signup Interceptor (Popup Mode)")
    print("  Browser will open - login with Google manually")
    print("  Popup window will be captured too")
    print("  Press Ctrl+C when done")
    print("=" * 60)

    manager = AsyncCamoufox(
        headless=False,
        os="windows",
        block_webrtc=True,
        humanize=False,
        screen=Screen(max_width=1920, max_height=1080),
    )

    browser = await manager.__aenter__()
    context = browser.contexts[0] if browser.contexts else await browser.new_context()

    # Track all pages (main + popups)
    all_pages = []

    def setup_page_listeners(page, label="main"):
        """Attach request/response listeners to a page."""

        async def on_request(request):
            url = request.url
            if _should_skip(url):
                return
            entry = {
                "timestamp": time.time(),
                "page": label,
                "method": request.method,
                "url": url,
                "headers": dict(request.headers),
                "post_data": None,
            }
            if request.method in ("POST", "PUT", "PATCH"):
                try:
                    entry["post_data"] = request.post_data
                except Exception:
                    pass
            captured.append(entry)

            if _is_interesting(url):
                print(f"  [{label}][REQ] {request.method} {url[:120]}")

        async def on_response(response):
            url = response.url
            if _should_skip(url):
                return

            for entry in reversed(captured):
                if entry["url"] == url and "response" not in entry:
                    entry["response"] = {
                        "status": response.status,
                        "headers": dict(response.headers),
                        "body": None,
                    }

                    if _is_interesting(url) or response.status in (301, 302, 303, 307, 308):
                        try:
                            body = await response.text()
                            entry["response"]["body"] = body[:10000]
                            if "sk-" in body or "api_key" in body.lower() or "token" in body.lower():
                                print(f"  [{label}][KEY!] {url[:80]}")
                                print(f"           {body[:300]}")
                        except Exception:
                            pass

                    if _is_interesting(url):
                        print(f"  [{label}][RESP] {response.status} {url[:100]}")
                    break

        page.on("request", on_request)
        page.on("response", on_response)

    # Listen for new pages (popups)
    def on_page(page):
        label = f"popup-{len(all_pages)}"
        all_pages.append(page)
        setup_page_listeners(page, label)
        print(f"\n  [NEW PAGE] {label} opened")

    context.on("page", on_page)

    # Main page
    page = await context.new_page()
    page.set_default_timeout(60000)
    all_pages.append(page)
    setup_page_listeners(page, "main")

    # Navigate to SkillBoss login
    print("\n  Opening SkillBoss login page...")
    await page.goto("https://www.skillboss.co/login", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)

    print("\n  Browser is open. Click 'Sign in with Google'.")
    print("  A popup will open - complete the Google login there.")
    print("  After you see the console/API key, press Ctrl+C.\n")

    try:
        while True:
            await asyncio.sleep(1)
            # Check all pages for API key
            try:
                for p in all_pages:
                    try:
                        current_url = p.url
                        if "console" in current_url or "dashboard" in current_url:
                            key = await p.evaluate(r"""() => {
                                const text = document.body?.innerText || '';
                                const m = text.match(/SKILLBOSS_API_KEY=(sk-[a-zA-Z0-9_=+\/-]{20,})/);
                                if (m) return m[1];
                                const m2 = text.match(/sk-gAAAAA[a-zA-Z0-9_=+\/-]{20,}/);
                                if (m2) return m2[0];
                                return null;
                            }""")
                            if key:
                                print(f"\n  [FOUND API KEY] {key}")
                    except Exception:
                        pass
            except Exception:
                pass
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    print(f"\n  Captured {len(captured)} requests")

    # Save HAR
    output_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intercept_skillboss_har.json")
    output = {
        "timestamp": time.time(),
        "total_requests": len(captured),
        "entries": captured,
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    print(f"  Saved to {output_file}")

    # Print findings
    print("\n  === AUTH/TOKEN FINDINGS ===")
    for entry in captured:
        url = entry.get("url", "")
        resp = entry.get("response", {})
        body = resp.get("body", "") or ""
        post = entry.get("post_data", "") or ""
        if "sk-" in body or "token" in url.lower() or "callback" in url.lower():
            print(f"\n  [{entry['page']}] {entry['method']} {url[:120]}")
            print(f"  Status: {resp.get('status', '?')}")
            if post:
                print(f"  POST: {str(post)[:300]}")
            if body and ("sk-" in body or "token" in body.lower()):
                print(f"  BODY: {body[:500]}")

    await manager.__aexit__(None, None, None)
    print("\n  Done.")


if __name__ == "__main__":
    asyncio.run(main())
