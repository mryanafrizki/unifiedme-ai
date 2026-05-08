#!/usr/bin/env python3
"""
Intercept Windsurf Google OAuth login — capture HAR + console logs.
Uses Playwright Chromium (not Camoufox) for better Google OAuth compat.

Usage:
  python intercept_windsurf.py

Opens a visible Chromium browser at windsurf.com/account/login.
You login manually via Google. Script captures ALL network requests + responses.
Saves to: intercept_windsurf_results.json
"""

import asyncio
import json
import sys
import time
from pathlib import Path

OUTPUT_FILE = Path("intercept_windsurf_results.json")


async def main():
    from playwright.async_api import async_playwright

    print()
    print("=" * 60)
    print("  Windsurf Login Interceptor (Chromium)")
    print("=" * 60)
    print()
    print("  1. Browser will open at windsurf.com/account/login")
    print("  2. Login via Google manually")
    print("  3. After login, script auto-detects redirect")
    print("  4. All network traffic saved to intercept_windsurf_results.json")
    print()

    har_entries = []
    console_logs = []
    captured_tokens = []

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.55 Safari/537.36",
    )
    page = await context.new_page()

    # Capture ALL network responses
    async def on_response(response):
        url = response.url
        status = response.status

        entry = {
            "url": url,
            "status": status,
            "method": response.request.method,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "body": None,
            "request_body": None,
            "request_headers": None,
        }

        # Capture body for interesting requests
        try:
            if any(k in url for k in [
                "windsurf.com", "codeium.com", "_devin-auth", "PostAuth",
                "register_user", "token", "auth", "login", "session",
                "identitytoolkit", "securetoken", "firebase", "googleapis",
            ]):
                try:
                    body = await response.text()
                    entry["body"] = body[:5000]
                except Exception:
                    pass

                # Request headers
                try:
                    entry["request_headers"] = dict(response.request.headers)
                except Exception:
                    pass

                # Request body (POST)
                try:
                    if response.request.method == "POST" and response.request.post_data:
                        entry["request_body"] = response.request.post_data[:3000]
                except Exception:
                    pass

                # Extract tokens
                if entry["body"] and entry["body"].strip().startswith("{"):
                    try:
                        data = json.loads(entry["body"])
                        for key in ["sessionToken", "session_token", "apiKey", "api_key",
                                    "idToken", "id_token", "token", "access_token",
                                    "refreshToken", "refresh_token", "oauthIdToken"]:
                            val = data.get(key, "")
                            if val and len(str(val)) > 10:
                                captured_tokens.append({
                                    "key": key,
                                    "value": str(val)[:200],
                                    "full_value": str(val),
                                    "url": url,
                                    "timestamp": entry["timestamp"],
                                })
                                print(f"  [TOKEN] {key} = {str(val)[:50]}... (from {url.split('?')[0].split('/')[-1]})")
                    except (json.JSONDecodeError, ValueError):
                        pass

                # Print interesting requests
                short_url = url.split("?")[0]
                if any(k in url for k in ["_devin-auth", "PostAuth", "register_user",
                                           "codeium.com", "identitytoolkit", "securetoken",
                                           "WindsurfPostAuth", "CheckUserLoginMethod"]):
                    print(f"  [{status}] {response.request.method} {short_url[-80:]}")
        except Exception:
            pass

        har_entries.append(entry)

    # Capture console
    def on_console(msg):
        console_logs.append({
            "type": msg.type,
            "text": msg.text[:500],
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    page.on("response", on_response)
    page.on("console", on_console)

    # Navigate
    print("  Opening windsurf.com/account/login...")
    await page.goto("https://windsurf.com/account/login", wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)
    print(f"  Page loaded: {page.url}")
    print()
    print("  >>> LOGIN VIA GOOGLE MANUALLY <<<")
    print("  >>> Script captures all network traffic <<<")
    print()

    # Wait for login completion
    login_done = False
    for tick in range(300):
        await asyncio.sleep(1)
        try:
            url = page.url
            if "windsurf.com" in url and "/account/login" not in url and "accounts.google.com" not in url:
                print(f"  [REDIRECT] Landed on: {url[:100]}")
                login_done = True
                print("  Waiting 10s for background requests...")
                await asyncio.sleep(10)
                break
        except Exception:
            pass
        if tick % 30 == 0 and tick > 0:
            print(f"  ... waiting ({tick}s, {len(har_entries)} requests captured)")

    if not login_done:
        print("  [WARN] Login not detected — saving what we have")

    # Try show-auth-token
    print()
    print("  Navigating to show-auth-token...")
    try:
        await page.goto("https://windsurf.com/show-auth-token", wait_until="networkidle", timeout=15000)
        await asyncio.sleep(3)
        page_text = await page.evaluate("() => document.body.innerText")
        if page_text and len(page_text.strip()) > 10:
            captured_tokens.append({
                "key": "show-auth-token-page",
                "value": page_text.strip()[:200],
                "full_value": page_text.strip(),
                "url": "windsurf.com/show-auth-token",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            print(f"  [TOKEN] show-auth-token: {page_text.strip()[:60]}...")
    except Exception as e:
        print(f"  show-auth-token failed: {e}")

    await browser.close()
    await pw.stop()

    # Save
    results = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_requests": len(har_entries),
        "total_console_logs": len(console_logs),
        "captured_tokens": captured_tokens,
        "interesting_requests": [e for e in har_entries if e.get("body") and any(k in e["url"] for k in ["_devin-auth", "PostAuth", "register_user", "codeium", "identitytoolkit", "securetoken", "WindsurfPostAuth", "CheckUserLoginMethod", "firebase"])],
        "all_windsurf_requests": [e for e in har_entries if "windsurf.com" in e["url"]],
        "console_logs": console_logs[-100:],
    }

    OUTPUT_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 60)
    print(f"  Saved: {OUTPUT_FILE}")
    print(f"  Requests:  {len(har_entries)}")
    print(f"  Tokens:    {len(captured_tokens)}")
    if captured_tokens:
        print()
        for t in captured_tokens:
            print(f"    {t['key']}: {t['value'][:60]}...")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
