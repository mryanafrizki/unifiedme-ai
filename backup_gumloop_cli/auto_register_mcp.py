#!/usr/bin/env python3
"""
Auto Register Gumloop + Setup MCP Server.

Flow:
  1. Login Google via Camoufox (browser visible)
  2. Extract Firebase tokens from IndexedDB
  3. Create gummie via REST API
  4. Create MCP credential (secret) via REST API
  5. Attach MCP to gummie via PATCH
  6. Output all credentials

Usage:
    python _tmp_auto_mcp/auto_register_mcp.py --email X@ggmel.com --password Y
    python _tmp_auto_mcp/auto_register_mcp.py --email X@ggmel.com --password Y --mcp-url https://your-tunnel.trycloudflare.com
"""

import argparse
import asyncio
import json
import os
import random
import string
import sys
import time
import uuid

import httpx

# Gumloop API
API_BASE = "https://api.gumloop.com"
FIREBASE_API_KEY = "AIzaSyCYuXqbJ0YBNltoGS4-7Y6Hozrra8KKmaE"
FIREBASE_REFRESH_URL = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", flush=True)


def random_mcp_name() -> str:
    """Generate random MCP server name like 'mcp-a3f9x2'."""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"mcp-{suffix}"


# ─── Browser Login ───────────────────────────────────────────────────────────


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
                                const val = item.value || item;
                                if (!val || !val.uid) continue;
                                const stm = val.stsTokenManager || {};
                                const idToken = stm.accessToken || val.accessToken || '';
                                const refreshToken = stm.refreshToken || val.refreshToken || '';
                                if (idToken) {
                                    resolve({
                                        idToken, refreshToken,
                                        uid: val.uid || '',
                                        email: val.email || '',
                                        displayName: val.displayName || '',
                                    });
                                    return;
                                }
                            }
                            resolve(null);
                        };
                        getAll.onerror = () => resolve(null);
                    } catch(e) { resolve(null); }
                };
                request.onerror = () => resolve(null);
            });
        }""")
        return result
    except Exception as e:
        log(f"extract_firebase_tokens error: {e}")
        return None


async def click_google_login(page) -> bool:
    """Click Get Started → Continue with Google."""
    # Click "Get Started"
    for _ in range(5):
        try:
            opened = await page.evaluate("""() => {
                for (const btn of document.querySelectorAll('button')) {
                    const txt = (btn.textContent || '').trim().toLowerCase();
                    if (txt === 'get started' && btn.offsetParent !== null) {
                        btn.click(); return true;
                    }
                }
                return false;
            }""")
            if opened:
                await asyncio.sleep(2)
                break
        except Exception:
            pass
        await asyncio.sleep(1)

    # Click "Continue with Google"
    for _ in range(10):
        try:
            clicked = await page.evaluate("""() => {
                for (const btn of document.querySelectorAll('button, a, div[role="button"]')) {
                    const txt = (btn.textContent || '').trim().toLowerCase();
                    if (txt.includes('continue with google') && btn.offsetParent !== null) {
                        btn.click(); return true;
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
    try:
        await page.wait_for_selector("#identifierId", state="visible", timeout=15000)
        loc = page.locator("#identifierId").first
        await loc.click(force=True)
        await asyncio.sleep(0.2)
        await loc.press("Control+a")
        await loc.press("Backspace")
        await loc.press_sequentially(email, delay=60)
        await asyncio.sleep(0.5)
        await page.evaluate("() => { const b = document.querySelector('#identifierNext button'); if (b) b.click(); }")
        await asyncio.sleep(3)
        return True
    except Exception as e:
        log(f"fill_google_email error: {e}")
        return False


async def fill_google_password(page, password: str) -> bool:
    try:
        await page.wait_for_selector('input[name="Passwd"]', state="visible", timeout=15000)
        loc = page.locator('input[name="Passwd"]').first
        await loc.click(force=True)
        await asyncio.sleep(0.2)
        await loc.press("Control+a")
        await loc.press("Backspace")
        await loc.press_sequentially(password, delay=70)
        await asyncio.sleep(0.5)
        url_before = page.url
        await page.evaluate("() => { const b = document.querySelector('#passwordNext button'); if (b) b.click(); }")
        # Wait for navigation
        for _ in range(15):
            await asyncio.sleep(1)
            try:
                if page.url != url_before:
                    return True
                has_error = await page.evaluate("""() => {
                    const err = document.querySelector('.LXRPh');
                    return err && err.offsetParent !== null ? err.textContent : null;
                }""")
                if has_error:
                    log(f"Password error: {has_error}")
                    return False
            except Exception:
                pass
        return True
    except Exception as e:
        log(f"fill_google_password error: {e}")
        return False


async def handle_consent(google_page, main_page) -> bool:
    """Handle Google consent screens and wait for redirect to Gumloop."""
    for tick in range(60):
        await asyncio.sleep(1)
        try:
            popup_closed = False
            gurl = ""
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

            # Try consent click on both pages
            for target in [google_page, main_page]:
                try:
                    if target.is_closed():
                        continue
                    target_url = target.url
                    if "accounts.google.com" not in target_url:
                        continue
                    await target.evaluate("""() => {
                        for (const b of document.querySelectorAll('button, div[role="button"]')) {
                            const t = (b.textContent||'').trim().toLowerCase();
                            if ((t==='continue'||t==='allow'||t.includes('continue')||t.includes('allow')||t==='lanjutkan') && b.offsetParent!==null) {
                                b.click(); return true;
                            }
                        }
                        // gaplustos
                        const el = document.querySelector('#confirm') || document.querySelector('input[type="submit"]');
                        if (el) { el.click(); return true; }
                        return false;
                    }""")
                except Exception:
                    pass

            # Check redirect
            if popup_closed or tick > 5:
                try:
                    main_url = main_page.url if not main_page.is_closed() else ""
                except Exception:
                    main_url = ""
                if "gumloop.com" in main_url and "login" not in main_url and "accounts.google.com" not in main_url:
                    return True

        except Exception:
            pass
    return False


async def browser_login(email: str, password: str) -> dict:
    """Full browser login flow. Returns tokens dict or error."""
    from browserforge.fingerprints import Screen
    from camoufox.async_api import AsyncCamoufox

    log("Launching browser (visible)...")
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

    try:
        # Navigate
        log("Opening Gumloop...")
        await page.goto("https://www.gumloop.com/home", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Click Google login
        log("Clicking Google sign-in...")
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
            return {"error": "Could not find Google sign-in button"}

        try:
            await asyncio.wait_for(popup_future, timeout=10)
        except asyncio.TimeoutError:
            pass

        google_page = popup_page or page
        if popup_page:
            await popup_page.wait_for_load_state("domcontentloaded", timeout=15000)
        await asyncio.sleep(2)

        # Email
        log(f"Filling email: {email}")
        ok = await fill_google_email(google_page, email)
        if not ok:
            return {"error": "Failed to fill Google email"}

        # Password
        log("Filling password...")
        ok = await fill_google_password(google_page, password)
        if not ok:
            return {"error": "Failed to fill Google password"}

        # Consent + redirect
        log("Handling consent & redirect...")
        redirected = await handle_consent(google_page, page)
        if not redirected:
            if "gumloop.com" not in page.url:
                return {"error": "Failed to redirect to Gumloop"}

        await asyncio.sleep(3)

        # Extract tokens
        log("Extracting Firebase tokens...")
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
            log(f"Token attempt {attempt+1}/8...")
            await asyncio.sleep(3)

        if not tokens or not tokens.get("idToken"):
            return {"error": "Failed to extract Firebase tokens"}

        log(f"Got tokens (uid={tokens.get('uid', '?')[:8]}...)")
        return {
            "id_token": tokens["idToken"],
            "refresh_token": tokens.get("refreshToken", ""),
            "user_id": tokens.get("uid", ""),
            "email": tokens.get("email", email),
            "display_name": tokens.get("displayName", ""),
        }

    except Exception as e:
        return {"error": str(e)}
    finally:
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            pass


# ─── API Calls ───────────────────────────────────────────────────────────────


def _api_headers(id_token: str, user_id: str) -> dict:
    return {
        "Authorization": f"Bearer {id_token}",
        "x-auth-key": user_id,
        "Content-Type": "application/json",
        "Origin": "https://www.gumloop.com",
        "Referer": "https://www.gumloop.com/",
    }


async def refresh_token(refresh_tok: str) -> dict:
    """Refresh Firebase token. Returns updated id_token + refresh_token."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(FIREBASE_REFRESH_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
        })
        resp.raise_for_status()
        data = resp.json()
        return {
            "id_token": data.get("id_token", ""),
            "refresh_token": data.get("refresh_token", refresh_tok),
            "user_id": data.get("user_id", ""),
        }


async def create_gummie(id_token: str, user_id: str, name: str = "Proxy Agent") -> str:
    """Create a new gummie. Returns gummie_id."""
    headers = _api_headers(id_token, user_id)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{API_BASE}/gummies", json={
            "name": name,
            "model_name": "claude-sonnet-4-5",
            "author_id": user_id,
        }, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        gummie_id = data.get("gummie", {}).get("gummie_id", "")
        if not gummie_id:
            raise ValueError(f"No gummie_id in response: {data}")
        return gummie_id


async def create_mcp_secret(
    id_token: str, user_id: str,
    mcp_url: str, mcp_name: str,
) -> str:
    """Create MCP server credential. Returns secret_id."""
    headers = _api_headers(id_token, user_id)
    payload = {
        "secret_type": "mcp_server",
        "value": "",
        "metadata": [
            {
                "name": "URL",
                "value": mcp_url,
                "placeholder": "https://mcp.example.com",
            },
            {
                "name": "Label",
                "value": mcp_name,
                "placeholder": "slack-mcp-server",
            },
            {
                "name": "Access Token / API Key",
                "value": "",
                "description": "OAuth authentication token, if required by the MCP server.",
                "placeholder": "xxxxxxxxxxxxxxxxxxxxxxxx",
                "isSecret": True,
                "isOptional": True,
            },
            {
                "name": "Additional Header",
                "value": "",
                "description": "Additional Header",
                "placeholder": "Authorization: Basic xxxxxxxxxxxxxxxxxxxxxxxx",
                "isOptional": True,
            },
        ],
        "nickname": mcp_name,
        "user_id": user_id,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{API_BASE}//secret", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        secret_id = data.get("secret_id", "")
        if not secret_id:
            raise ValueError(f"No secret_id in response: {data}")
        return secret_id


async def attach_mcp_to_gummie(
    id_token: str, user_id: str,
    gummie_id: str, secret_id: str,
    mcp_url: str, mcp_name: str,
) -> dict:
    """Attach MCP server + built-in tools to gummie via PATCH. Returns gummie data."""
    headers = _api_headers(id_token, user_id)
    payload = {
        "tools": [
            {
                "secret_id": secret_id,
                "mcp_server_url": mcp_url,
                "name": mcp_name,
                "type": "mcp_server",
                "restricted_tools": [],
            },
            {"metadata": {}, "type": "web_search"},
            {"metadata": {}, "type": "web_fetch"},
            {"metadata": {"model": "gemini-3.1-flash-image-preview"}, "type": "image_generator"},
            {"type": "interaction_search"},
        ],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.patch(
            f"{API_BASE}/gummies/{gummie_id}",
            json=payload, headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


async def verify_mcp_servers(id_token: str, user_id: str) -> list:
    """List MCP servers for verification."""
    headers = _api_headers(id_token, user_id)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{API_BASE}//secrets/mcp_servers", headers=headers)
        resp.raise_for_status()
        return resp.json()


# ─── Main ────────────────────────────────────────────────────────────────────


async def main(email: str, password: str, mcp_url: str):
    print()
    print("=" * 60)
    print("  Gumloop Auto Register + MCP Setup")
    print("=" * 60)
    print()

    # Step 1: Browser login
    log("STEP 1: Browser login...")
    login_result = await browser_login(email, password)

    if "error" in login_result:
        log(f"FAILED: {login_result['error']}")
        return

    id_token = login_result["id_token"]
    refresh_tok = login_result["refresh_token"]
    user_id = login_result["user_id"]
    log(f"Login OK — user_id={user_id}")

    # Refresh token to get a fresh one
    log("Refreshing token...")
    try:
        refreshed = await refresh_token(refresh_tok)
        id_token = refreshed["id_token"] or id_token
        refresh_tok = refreshed["refresh_token"] or refresh_tok
        user_id = refreshed["user_id"] or user_id
        log("Token refreshed OK")
    except Exception as e:
        log(f"Token refresh failed (using original): {e}")

    # Step 2: Create gummie
    log("STEP 2: Creating gummie...")
    try:
        gummie_id = await create_gummie(id_token, user_id)
        log(f"Gummie created: {gummie_id}")
    except Exception as e:
        log(f"FAILED to create gummie: {e}")
        return

    # Step 3: Create MCP credential
    mcp_name = random_mcp_name()
    log(f"STEP 3: Creating MCP credential '{mcp_name}' → {mcp_url}")
    try:
        secret_id = await create_mcp_secret(id_token, user_id, mcp_url, mcp_name)
        log(f"MCP secret created: {secret_id}")
    except Exception as e:
        log(f"FAILED to create MCP secret: {e}")
        return

    # Step 4: Attach MCP to gummie
    log("STEP 4: Attaching MCP to gummie...")
    try:
        result = await attach_mcp_to_gummie(
            id_token, user_id, gummie_id, secret_id, mcp_url, mcp_name,
        )
        # Verify tools in response
        tools = result.get("gummie", {}).get("tools", [])
        mcp_tools = [t for t in tools if t.get("type") == "mcp_server"]
        if mcp_tools:
            log(f"MCP attached OK — {len(mcp_tools)} MCP tool(s) on gummie")
        else:
            log("WARNING: MCP not found in gummie tools after PATCH")
    except Exception as e:
        log(f"FAILED to attach MCP: {e}")
        return

    # Step 5: Verify
    log("STEP 5: Verifying MCP servers...")
    try:
        servers = await verify_mcp_servers(id_token, user_id)
        log(f"MCP servers: {json.dumps(servers, indent=2)}")
    except Exception as e:
        log(f"Verify failed: {e}")

    # Output
    output = {
        "email": email,
        "user_id": user_id,
        "gummie_id": gummie_id,
        "mcp_secret_id": secret_id,
        "mcp_name": mcp_name,
        "mcp_url": mcp_url,
        "id_token": id_token[:50] + "...",
        "refresh_token": refresh_tok[:50] + "...",
        "refresh_token_full": refresh_tok,
    }

    print()
    print("=" * 60)
    print("  RESULT")
    print("=" * 60)
    for k, v in output.items():
        if k == "refresh_token_full":
            continue
        print(f"  {k}: {v}")
    print("=" * 60)

    # Save to file
    out_file = os.path.join(os.path.dirname(__file__), "result.json")
    full_output = {
        "email": email,
        "user_id": user_id,
        "gummie_id": gummie_id,
        "mcp_secret_id": secret_id,
        "mcp_name": mcp_name,
        "mcp_url": mcp_url,
        "id_token": id_token,
        "refresh_token": refresh_tok,
    }
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(full_output, f, indent=2, ensure_ascii=False)
    log(f"Full credentials saved to {out_file}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto Register Gumloop + Setup MCP")
    parser.add_argument("--email", required=True, help="Google account email")
    parser.add_argument("--password", required=True, help="Google account password")
    parser.add_argument("--mcp-url", default="http://google.com", help="MCP server URL (default: http://google.com)")
    args = parser.parse_args()

    asyncio.run(main(args.email, args.password, args.mcp_url))
