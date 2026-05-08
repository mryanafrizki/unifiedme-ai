#!/usr/bin/env python3
"""
Intercept Gumloop HTTP API calls during manual MCP setup.

Usage:
    python intercept_gumloop_mcp.py --email you@gmail.com --password yourpass

Browser opens NON-headless. You click through:
  1. Settings → Credentials → Add MCP Server → fill URL → save
  2. Agent settings → Add tools → select MCP Server → save

Script captures ALL HTTP requests to api.gumloop.com and writes to:
  - Console (live, filtered)
  - intercept_mcp_log.json (full dump on exit)

Press Ctrl+C when done.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime

# Force non-headless
os.environ["BATCHER_CAMOUFOX_HEADLESS"] = "false"

# Captured requests
captured: list[dict] = []
# Domains to capture
CAPTURE_DOMAINS = ["api.gumloop.com", "gumloop.com", "firebaseinstallations.googleapis.com"]
# Skip noisy endpoints
SKIP_PATTERNS = [
    "/ws/",
    "/__/auth/",
    "/securetoken.googleapis.com",
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".svg",
    ".woff",
    ".ico",
    "analytics",
    "gtag",
    "sentry",
    "hotjar",
    "intercom",
    "segment",
    "mixpanel",
    "amplitude",
]


def should_capture(url: str) -> bool:
    """Check if URL is worth capturing."""
    # Must match a capture domain
    if not any(d in url for d in CAPTURE_DOMAINS):
        return False
    # Skip noisy stuff
    url_lower = url.lower()
    if any(p in url_lower for p in SKIP_PATTERNS):
        return False
    return True


def format_entry(entry: dict) -> str:
    """Pretty-print a captured request for console."""
    method = entry.get("method", "?")
    url = entry.get("url", "?")
    status = entry.get("status", "?")
    
    # Shorten URL for display
    short_url = url.replace("https://api.gumloop.com", "")
    short_url = short_url.replace("https://www.gumloop.com", "")
    
    line = f"  [{method}] {short_url} → {status}"
    
    # Show request body if present
    req_body = entry.get("request_body")
    if req_body and req_body != "null":
        try:
            parsed = json.loads(req_body) if isinstance(req_body, str) else req_body
            body_str = json.dumps(parsed, indent=2, ensure_ascii=False)
            if len(body_str) > 500:
                body_str = body_str[:500] + "..."
            line += f"\n    REQ: {body_str}"
        except (json.JSONDecodeError, TypeError):
            if len(str(req_body)) < 500:
                line += f"\n    REQ: {req_body}"

    # Show response body if present
    resp_body = entry.get("response_body")
    if resp_body:
        try:
            parsed = json.loads(resp_body) if isinstance(resp_body, str) else resp_body
            body_str = json.dumps(parsed, indent=2, ensure_ascii=False)
            if len(body_str) > 1000:
                body_str = body_str[:1000] + "..."
            line += f"\n    RES: {body_str}"
        except (json.JSONDecodeError, TypeError):
            if len(str(resp_body)) < 1000:
                line += f"\n    RES: {resp_body}"

    return line


async def main(email: str, password: str):
    from browserforge.fingerprints import Screen
    from camoufox.async_api import AsyncCamoufox

    print()
    print("=" * 60)
    print("  Gumloop MCP Setup Interceptor")
    print("=" * 60)
    print()
    print("  Browser will open. Login will be automated.")
    print("  After login, manually do:")
    print("    1. Settings → Credentials → Add MCP Server")
    print("    2. Agent → Add tools → select MCP Server")
    print()
    print("  All API calls will be captured.")
    print("  Press Ctrl+C when done.")
    print()

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

    # ── Request/Response interceptor ──
    async def on_response(response):
        url = response.url
        if not should_capture(url):
            return

        request = response.request
        method = request.method

        # Skip GET for static-ish resources
        if method == "GET" and any(url.endswith(ext) for ext in [".json", ".html"]):
            pass  # still capture these

        entry = {
            "timestamp": datetime.now().isoformat(),
            "method": method,
            "url": url,
            "status": response.status,
            "request_headers": dict(request.headers),
            "request_body": None,
            "response_headers": dict(response.headers),
            "response_body": None,
        }

        # Capture request body (POST/PATCH/PUT/DELETE)
        if method in ("POST", "PATCH", "PUT", "DELETE"):
            try:
                entry["request_body"] = request.post_data
            except Exception:
                pass

        # Capture response body
        try:
            body = await response.text()
            entry["response_body"] = body
        except Exception:
            pass

        captured.append(entry)
        print(format_entry(entry))
        print()

    page.on("response", on_response)

    # ── Automated login ──
    print("  [1/5] Opening Gumloop...")
    await page.goto("https://www.gumloop.com/home", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    print("  [2/5] Clicking Get Started...")
    await page.evaluate("""() => {
        for (const b of document.querySelectorAll('button')) {
            if (b.textContent.trim().toLowerCase() === 'get started') { b.click(); return true; }
        }
        return false;
    }""")
    await asyncio.sleep(2)

    # Listen for popup
    popup_page = None
    popup_future = asyncio.get_event_loop().create_future()

    def on_popup(p):
        nonlocal popup_page
        popup_page = p
        if not popup_future.done():
            popup_future.set_result(p)

    page.context.on("page", on_popup)

    print("  [3/5] Clicking Continue with Google...")
    await page.evaluate("""() => {
        for (const b of document.querySelectorAll('button, a, div[role="button"]')) {
            if ((b.textContent||'').trim().toLowerCase().includes('continue with google')) { b.click(); return true; }
        }
        return false;
    }""")

    try:
        await asyncio.wait_for(popup_future, timeout=10)
    except asyncio.TimeoutError:
        pass

    google_page = popup_page or page
    if popup_page:
        await popup_page.wait_for_load_state("domcontentloaded", timeout=15000)
        # Also intercept on popup
        popup_page.on("response", on_response)

    await asyncio.sleep(2)

    print(f"  [4/5] Filling email: {email}")
    try:
        await google_page.wait_for_selector("#identifierId", state="visible", timeout=15000)
        loc = google_page.locator("#identifierId").first
        await loc.click(force=True)
        await asyncio.sleep(0.2)
        await loc.press("Control+a")
        await loc.press("Backspace")
        await loc.press_sequentially(email, delay=60)
        await asyncio.sleep(0.5)
        await google_page.evaluate("() => { const b = document.querySelector('#identifierNext button'); if (b) b.click(); }")
        await asyncio.sleep(3)
    except Exception as e:
        print(f"  ⚠ Email step failed: {e}")
        print("  → Please complete login manually in the browser")

    print("  [5/5] Filling password...")
    try:
        await google_page.wait_for_selector('input[name="Passwd"]', state="visible", timeout=15000)
        loc = google_page.locator('input[name="Passwd"]').first
        await loc.click(force=True)
        await asyncio.sleep(0.2)
        await loc.press("Control+a")
        await loc.press("Backspace")
        await loc.press_sequentially(password, delay=70)
        await asyncio.sleep(0.5)
        await google_page.evaluate("() => { const b = document.querySelector('#passwordNext button'); if (b) b.click(); }")
        await asyncio.sleep(5)
    except Exception as e:
        print(f"  ⚠ Password step failed: {e}")
        print("  → Please complete login manually in the browser")

    # Handle consent if needed
    for _ in range(10):
        try:
            current_url = page.url
            if "gumloop.com" in current_url and "accounts.google.com" not in current_url:
                break
            # Try clicking consent
            for target in [google_page, page]:
                try:
                    await target.evaluate("""() => {
                        for (const b of document.querySelectorAll('button, div[role="button"]')) {
                            const t = (b.textContent||'').trim().toLowerCase();
                            if ((t === 'continue' || t === 'allow' || t.includes('continue')) && b.offsetParent !== null) {
                                b.click(); return true;
                            }
                        }
                        return false;
                    }""")
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(2)

    await asyncio.sleep(3)

    print()
    print("=" * 60)
    print("  ✓ Login complete (or manual login needed)")
    print("  ✓ HTTP interceptor is ACTIVE")
    print()
    print("  Now manually do the MCP setup in the browser:")
    print("    1. Go to Settings → Credentials → Add MCP Server")
    print("    2. Go to your Agent → Settings → Add MCP tools")
    print()
    print("  All API calls will appear below.")
    print("  Press Ctrl+C when done to save the log.")
    print("=" * 60)
    print()

    # Keep alive until Ctrl+C
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    # Save full log
    out_file = "intercept_mcp_log.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(captured, f, indent=2, ensure_ascii=False)

    print()
    print(f"  Saved {len(captured)} captured requests to {out_file}")

    # Print summary of interesting endpoints
    print()
    print("  === SUMMARY (non-GET requests) ===")
    for entry in captured:
        if entry["method"] != "GET":
            print(f"    {entry['method']} {entry['url']}")
    print()

    try:
        await manager.__aexit__(None, None, None)
    except Exception:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Intercept Gumloop MCP setup API calls")
    parser.add_argument("--email", required=True, help="Google account email")
    parser.add_argument("--password", required=True, help="Google account password")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.email, args.password))
    except KeyboardInterrupt:
        # Save on Ctrl+C during asyncio.run
        if captured:
            out_file = "intercept_mcp_log.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(captured, f, indent=2, ensure_ascii=False)
            print(f"\n  Saved {len(captured)} captured requests to {out_file}")
