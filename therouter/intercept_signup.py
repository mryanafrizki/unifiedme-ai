#!/usr/bin/env python3
"""
Intercept TheRouter signup + login + API key creation flow.

Opens a visible Chromium browser at dashboard.therouter.ai/register.
Captures ALL network traffic (requests + responses + headers + bodies).
Handles popup windows (Google OAuth, etc.).

Output: therouter/intercept_therouter_har.json

Usage:
    python therouter/intercept_signup.py
    python therouter/intercept_signup.py --login          # Start at login page
    python therouter/intercept_signup.py --url <custom>   # Start at custom URL

Flow:
    1. Browser opens at /register (or /login)
    2. You signup/login manually (email, Google, GitHub, etc.)
    3. Navigate to API key creation page
    4. Script captures everything: auth tokens, session cookies, API keys
    5. Press Ctrl+C when done — HAR saved

Press Ctrl+C to stop.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

THEROUTER_BASE = "https://dashboard.therouter.ai"
SKIP_EXTENSIONS = frozenset([
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".woff", ".woff2", ".ttf", ".eot", ".ico", ".map",
    ".webp", ".avif", ".mp4", ".webm",
])

# Keywords that signal interesting auth/API traffic
AUTH_KEYWORDS = [
    "auth", "token", "session", "login", "signup", "register",
    "api-key", "apikey", "api_key", "credential", "password",
    "oauth", "callback", "google", "github", "firebase",
    "identitytoolkit", "securetoken", "supabase",
    "therouter", "user", "account", "profile", "key",
    "create", "generate", "dashboard",
]

captured: list[dict] = []
captured_tokens: list[dict] = []
captured_cookies: list[dict] = []


def _should_skip(url: str) -> bool:
    path = url.split("?")[0].lower()
    return any(path.endswith(ext) for ext in SKIP_EXTENSIONS)


def _is_interesting(url: str) -> bool:
    lower = url.lower()
    return any(kw in lower for kw in AUTH_KEYWORDS)


def _extract_tokens_from_body(body: str, url: str, timestamp: str) -> None:
    """Try to extract auth tokens, API keys, session tokens from response body."""
    if not body or not body.strip().startswith(("{", "[")):
        return
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return

    if not isinstance(data, dict):
        return

    # Common token field names
    token_keys = [
        "token", "access_token", "accessToken",
        "refresh_token", "refreshToken",
        "id_token", "idToken",
        "session_token", "sessionToken",
        "api_key", "apiKey", "api-key",
        "key", "secret", "secret_key",
        "jwt", "bearer",
        "authorization",
        "cookie", "set-cookie",
    ]

    def _scan(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                full_key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, str) and len(v) > 8:
                    lower_k = k.lower()
                    if any(tk in lower_k for tk in token_keys) or v.startswith(("sk-", "pk-", "eyJ", "Bearer ")):
                        captured_tokens.append({
                            "key": full_key,
                            "value": v[:200],
                            "full_value": v,
                            "url": url,
                            "timestamp": timestamp,
                        })
                        print(f"  [TOKEN] {full_key} = {v[:60]}...")
                elif isinstance(v, (dict, list)):
                    _scan(v, full_key)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _scan(item, f"{prefix}[{i}]")

    _scan(data)


def _extract_tokens_from_headers(headers: dict, url: str, timestamp: str) -> None:
    """Extract tokens from response headers (Set-Cookie, Authorization, etc.)."""
    for k, v in headers.items():
        lower_k = k.lower()
        if lower_k in ("set-cookie", "authorization", "x-auth-token", "x-session-token"):
            if len(str(v)) > 8:
                captured_tokens.append({
                    "key": f"header:{k}",
                    "value": str(v)[:200],
                    "full_value": str(v),
                    "url": url,
                    "timestamp": timestamp,
                })
                print(f"  [HEADER-TOKEN] {k} = {str(v)[:80]}...")


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Intercept TheRouter signup/login flow")
    parser.add_argument("--login", action="store_true", help="Start at login page instead of register")
    parser.add_argument("--url", type=str, help="Custom start URL")
    parser.add_argument("--chromium", action="store_true", help="Use Playwright Chromium instead of Camoufox")
    args = parser.parse_args()

    start_url = args.url or (f"{THEROUTER_BASE}/login" if args.login else f"{THEROUTER_BASE}/register")

    print()
    print("=" * 70)
    print("  TheRouter Signup/Login Interceptor")
    print("=" * 70)
    print()
    print(f"  Start URL:  {start_url}")
    print(f"  Browser:    {'Chromium (Playwright)' if args.chromium else 'Camoufox (anti-detect)'}")
    print()
    print("  Steps:")
    print("    1. Browser opens — signup or login manually")
    print("    2. After login, navigate to API key creation")
    print("    3. Create an API key — script captures it")
    print("    4. Press Ctrl+C when done")
    print()

    if args.chromium:
        await _run_chromium(start_url)
    else:
        await _run_camoufox(start_url)


async def _run_chromium(start_url: str):
    """Run with Playwright Chromium — better Google OAuth compat."""
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        viewport={"width": 1366, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.6778.86 Safari/537.36"
        ),
    )

    all_pages = []

    def setup_page(page, label="main"):
        all_pages.append(page)
        _attach_listeners(page, label)

    context.on("page", lambda p: setup_page(p, f"popup-{len(all_pages)}"))

    page = await context.new_page()
    setup_page(page, "main")

    print(f"  Opening {start_url}...")
    await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)
    print(f"  Page loaded: {page.url}")
    print()
    print("  >>> SIGNUP / LOGIN MANUALLY <<<")
    print("  >>> Then navigate to API key creation <<<")
    print("  >>> Press Ctrl+C when done <<<")
    print()

    try:
        tick = 0
        while True:
            await asyncio.sleep(1)
            tick += 1

            # Periodic status
            if tick % 30 == 0:
                print(f"  ... {tick}s elapsed, {len(captured)} requests, {len(captured_tokens)} tokens captured")

            # Check for API keys on page
            if tick % 5 == 0:
                for p in all_pages:
                    try:
                        if p.is_closed():
                            continue
                        await _check_page_for_keys(p)
                    except Exception:
                        pass

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    # Capture cookies before closing
    try:
        cookies = await context.cookies()
        for c in cookies:
            if "therouter" in c.get("domain", ""):
                captured_cookies.append(c)
    except Exception:
        pass

    # Save FIRST, before browser close (which can crash on Ctrl+C)
    _save_results()

    try:
        await browser.close()
    except Exception:
        pass
    try:
        await pw.stop()
    except Exception:
        pass


async def _run_camoufox(start_url: str):
    """Run with Camoufox — anti-detection Firefox."""
    try:
        from browserforge.fingerprints import Screen
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        print("  [WARN] camoufox not installed, falling back to Chromium")
        await _run_chromium(start_url)
        return

    manager = AsyncCamoufox(
        headless=False,
        os="windows",
        block_webrtc=True,
        humanize=False,
        screen=Screen(max_width=1920, max_height=1080),
    )

    browser = await manager.__aenter__()
    context = browser.contexts[0] if browser.contexts else await browser.new_context()

    all_pages = []

    def setup_page(page, label="main"):
        all_pages.append(page)
        _attach_listeners(page, label)

    context.on("page", lambda p: setup_page(p, f"popup-{len(all_pages)}"))

    page = await context.new_page()
    page.set_default_timeout(60000)
    setup_page(page, "main")

    print(f"  Opening {start_url}...")
    await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)
    print(f"  Page loaded: {page.url}")
    print()
    print("  >>> SIGNUP / LOGIN MANUALLY <<<")
    print("  >>> Then navigate to API key creation <<<")
    print("  >>> Press Ctrl+C when done <<<")
    print()

    try:
        tick = 0
        while True:
            await asyncio.sleep(1)
            tick += 1

            if tick % 30 == 0:
                print(f"  ... {tick}s elapsed, {len(captured)} requests, {len(captured_tokens)} tokens captured")

            if tick % 5 == 0:
                for p in all_pages:
                    try:
                        if p.is_closed():
                            continue
                        await _check_page_for_keys(p)
                    except Exception:
                        pass

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    # Save FIRST, before browser close (which can crash on Ctrl+C)
    _save_results()

    try:
        await manager.__aexit__(None, None, None)
    except Exception:
        pass


def _attach_listeners(page, label: str):
    """Attach request + response listeners to a page."""

    async def on_request(request):
        url = request.url
        if _should_skip(url):
            return

        entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": time.time(),
            "page": label,
            "method": request.method,
            "url": url,
            "request_headers": dict(request.headers),
            "post_data": None,
            "response": None,
        }

        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                entry["post_data"] = request.post_data
            except Exception:
                pass

        captured.append(entry)

        if _is_interesting(url):
            short = url.split("?")[0]
            post_preview = ""
            if entry["post_data"]:
                post_preview = f" | body={str(entry['post_data'])[:120]}"
            print(f"  [{label}][REQ] {request.method} {short[-100:]}{post_preview}")

    async def on_response(response):
        url = response.url
        if _should_skip(url):
            return

        ts = time.strftime("%Y-%m-%d %H:%M:%S")

        # Find matching request entry (most recent with same URL)
        for entry in reversed(captured):
            if entry["url"] == url and entry["response"] is None:
                resp_headers = {}
                try:
                    resp_headers = dict(response.headers)
                except Exception:
                    pass

                entry["response"] = {
                    "status": response.status,
                    "headers": resp_headers,
                    "body": None,
                }

                # Capture body for interesting requests or redirects
                if _is_interesting(url) or response.status in (301, 302, 303, 307, 308) or response.status >= 400:
                    try:
                        body = await response.text()
                        entry["response"]["body"] = body[:10000]

                        # Extract tokens from body
                        _extract_tokens_from_body(body, url, ts)

                        # Check for raw API key patterns in body
                        if any(pat in body for pat in ["sk-", "pk-", "api_key", "apiKey", "API_KEY"]):
                            print(f"  [{label}][KEY!] {url.split('?')[0][-80:]}")
                            print(f"           {body[:300]}")
                    except Exception:
                        pass

                # Extract tokens from headers
                _extract_tokens_from_headers(resp_headers, url, ts)

                if _is_interesting(url):
                    print(f"  [{label}][RESP] {response.status} {url.split('?')[0][-100:]}")

                break

    page.on("request", on_request)
    page.on("response", on_response)


async def _check_page_for_keys(page) -> None:
    """Check current page DOM for visible API keys."""
    try:
        url = page.url
        if not any(kw in url.lower() for kw in ["dashboard", "console", "key", "setting", "api", "account"]):
            return

        result = await page.evaluate(r"""() => {
            const text = document.body?.innerText || '';
            const found = [];

            // Common API key patterns
            const patterns = [
                /sk-[a-zA-Z0-9_\-]{20,}/g,
                /pk-[a-zA-Z0-9_\-]{20,}/g,
                /tr-[a-zA-Z0-9_\-]{20,}/g,
                /key-[a-zA-Z0-9_\-]{20,}/g,
                /eyJ[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{20,}/g,
            ];

            for (const pat of patterns) {
                const matches = text.match(pat);
                if (matches) {
                    for (const m of matches) {
                        found.push(m);
                    }
                }
            }

            // Check input fields with key-like values
            for (const input of document.querySelectorAll('input[type="text"], input[type="password"], textarea')) {
                const val = input.value || '';
                if (val.length > 15 && (val.startsWith('sk-') || val.startsWith('pk-') || val.startsWith('tr-') || val.startsWith('key-'))) {
                    found.push(val);
                }
            }

            // Check code/pre blocks
            for (const el of document.querySelectorAll('code, pre, [class*="key"], [class*="token"], [class*="secret"]')) {
                const t = (el.textContent || '').trim();
                if (t.length > 15 && t.length < 500) {
                    for (const pat of patterns) {
                        pat.lastIndex = 0;
                        const m = t.match(pat);
                        if (m) found.push(...m);
                    }
                }
            }

            return [...new Set(found)];
        }""")

        if result:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            for key_val in result:
                # Deduplicate
                if not any(t["full_value"] == key_val for t in captured_tokens):
                    captured_tokens.append({
                        "key": "dom-api-key",
                        "value": key_val[:200],
                        "full_value": key_val,
                        "url": url,
                        "timestamp": ts,
                    })
                    print(f"\n  [FOUND KEY] {key_val[:80]}...")
                    print(f"              from: {url[:80]}")

    except Exception:
        pass


def _save_results():
    """Save captured data to JSON file."""
    output_file = Path(__file__).parent / "intercept_therouter_har.json"

    # Separate interesting vs all requests
    interesting = []
    all_therouter = []
    for entry in captured:
        url = entry.get("url", "")
        if _is_interesting(url):
            interesting.append(entry)
        if "therouter" in url:
            all_therouter.append(entry)

    results = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": THEROUTER_BASE,
        "total_requests": len(captured),
        "total_tokens": len(captured_tokens),
        "total_cookies": len(captured_cookies),

        "captured_tokens": captured_tokens,
        "captured_cookies": captured_cookies,

        "interesting_requests": interesting,
        "all_therouter_requests": all_therouter,
        "all_requests": captured[-500:],  # Last 500
    }

    output_file.write_text(
        json.dumps(results, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    print()
    print("=" * 70)
    print(f"  Saved: {output_file}")
    print(f"  Total requests:  {len(captured)}")
    print(f"  TheRouter reqs:  {len(all_therouter)}")
    print(f"  Interesting:     {len(interesting)}")
    print(f"  Tokens found:    {len(captured_tokens)}")
    print(f"  Cookies:         {len(captured_cookies)}")

    if captured_tokens:
        print()
        print("  === CAPTURED TOKENS / KEYS ===")
        for t in captured_tokens:
            print(f"    [{t['key']}] {t['value'][:80]}...")
            print(f"      from: {t['url'].split('?')[0][-80:]}")

    if captured_cookies:
        print()
        print("  === THEROUTER COOKIES ===")
        for c in captured_cookies:
            print(f"    {c.get('name', '?')} = {str(c.get('value', ''))[:60]}...")

    # Print auth flow summary
    print()
    print("  === AUTH FLOW (chronological) ===")
    auth_entries = [e for e in captured if _is_interesting(e.get("url", "")) and e.get("response")]
    for e in auth_entries[:50]:
        status = e["response"]["status"]
        method = e["method"]
        url = e["url"].split("?")[0]
        post = str(e.get("post_data", ""))[:80] if e.get("post_data") else ""
        body_preview = ""
        resp_body = e["response"].get("body", "") or ""
        if resp_body:
            body_preview = resp_body[:120].replace("\n", " ")
        print(f"    [{status}] {method:6s} {url[-80:]}")
        if post:
            print(f"           POST: {post}")
        if body_preview:
            print(f"           RESP: {body_preview}")

    print()
    print("=" * 70)
    print("  Next steps:")
    print("    1. Review intercept_therouter_har.json")
    print("    2. Identify signup/login/apikey endpoints + payloads")
    print("    3. Build therouter/register.py (automatic flow)")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
