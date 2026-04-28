#!/usr/bin/env python3
"""
Batch attach MCP server to ALL existing Gumloop accounts in proxy DB.

No browser login needed — uses existing refresh_tokens from database.

Flow per account:
  1. Refresh Firebase token
  2. Check if MCP secret already exists → skip if yes
  3. Create MCP secret (POST //secret)
  4. Attach MCP + built-in tools to gummie (PATCH /gummies/{id})

Usage:
    python _tmp_mcp_server/batch_attach_mcp.py --mcp-url https://xxx.trycloudflare.com
    python _tmp_mcp_server/batch_attach_mcp.py --mcp-url https://mcp.yourdomain.com
"""

import argparse
import asyncio
import json
import random
import string
import time

import aiosqlite
import httpx

DB_PATH = r"C:\Users\User\unifiedme-ai\unified\data\unified.db"
API_BASE = "https://api.gumloop.com"
FIREBASE_API_KEY = "AIzaSyCYuXqbJ0YBNltoGS4-7Y6Hozrra8KKmaE"
FIREBASE_REFRESH_URL = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", flush=True)


def random_mcp_name() -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"mcp-{suffix}"


async def refresh_token(refresh_tok: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(FIREBASE_REFRESH_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
        })
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:100]}"}
        data = resp.json()
        return {
            "id_token": data.get("id_token", ""),
            "refresh_token": data.get("refresh_token", refresh_tok),
            "user_id": data.get("user_id", ""),
        }


def _headers(id_token: str, user_id: str) -> dict:
    return {
        "Authorization": f"Bearer {id_token}",
        "x-auth-key": user_id,
        "Content-Type": "application/json",
    }


async def get_existing_mcp_servers(id_token: str, user_id: str) -> list:
    """Check if account already has MCP servers."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{API_BASE}//secrets/mcp_servers",
            headers=_headers(id_token, user_id),
        )
        if resp.status_code == 200:
            return resp.json()
        return []


async def create_mcp_secret(id_token: str, user_id: str, mcp_url: str, mcp_name: str) -> str:
    """Create MCP server credential. Returns secret_id."""
    payload = {
        "secret_type": "mcp_server",
        "value": "",
        "metadata": [
            {"name": "URL", "value": mcp_url, "placeholder": "https://mcp.example.com"},
            {"name": "Label", "value": mcp_name, "placeholder": "slack-mcp-server"},
            {"name": "Access Token / API Key", "value": "", "isSecret": True, "isOptional": True,
             "description": "OAuth authentication token, if required by the MCP server.",
             "placeholder": "xxxxxxxxxxxxxxxxxxxxxxxx"},
            {"name": "Additional Header", "value": "", "isOptional": True,
             "description": "Additional Header",
             "placeholder": "Authorization: Basic xxxxxxxxxxxxxxxxxxxxxxxx"},
        ],
        "nickname": mcp_name,
        "user_id": user_id,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{API_BASE}//secret",
            json=payload,
            headers=_headers(id_token, user_id),
        )
        if resp.status_code not in (200, 201):
            return ""
        return resp.json().get("secret_id", "")


async def attach_tools_to_gummie(
    id_token: str, user_id: str,
    gummie_id: str, secret_id: str,
    mcp_url: str, mcp_name: str,
) -> bool:
    """Attach MCP + built-in tools to gummie."""
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
            json=payload,
            headers=_headers(id_token, user_id),
        )
        if resp.status_code == 200:
            tools = resp.json().get("gummie", {}).get("tools", [])
            mcp_count = sum(1 for t in tools if t.get("type") == "mcp_server")
            return mcp_count > 0
        return False


async def process_account(account: dict, mcp_url: str) -> dict:
    """Process a single account. Returns result dict."""
    email = account["email"]
    gummie_id = account["gl_gummie_id"]
    refresh_tok = account["gl_refresh_token"]

    # 1. Refresh token
    auth = await refresh_token(refresh_tok)
    if auth.get("error"):
        return {"email": email, "status": "FAIL", "reason": f"Token refresh: {auth['error']}"}

    id_token = auth["id_token"]
    user_id = auth["user_id"]

    # 2. Check existing MCP servers
    existing = await get_existing_mcp_servers(id_token, user_id)
    existing_urls = [s.get("url", "") for s in existing]

    if mcp_url in existing_urls:
        # Already has this MCP URL — just make sure it's attached to gummie
        secret_id = next((s["secret_id"] for s in existing if s.get("url") == mcp_url), "")
        mcp_name = next((s["nickname"] for s in existing if s.get("url") == mcp_url), "mcp")
        if secret_id:
            ok = await attach_tools_to_gummie(id_token, user_id, gummie_id, secret_id, mcp_url, mcp_name)
            if ok:
                return {"email": email, "status": "OK", "reason": "Already had MCP, re-attached to gummie"}
            return {"email": email, "status": "FAIL", "reason": "Had MCP but failed to attach to gummie"}

    # 3. Create new MCP secret
    mcp_name = random_mcp_name()
    secret_id = await create_mcp_secret(id_token, user_id, mcp_url, mcp_name)
    if not secret_id:
        return {"email": email, "status": "FAIL", "reason": "Failed to create MCP secret"}

    # 4. Attach to gummie
    ok = await attach_tools_to_gummie(id_token, user_id, gummie_id, secret_id, mcp_url, mcp_name)
    if not ok:
        return {"email": email, "status": "FAIL", "reason": "Created MCP but failed to attach to gummie"}

    return {"email": email, "status": "OK", "reason": f"Created {mcp_name} + attached", "secret_id": secret_id}


async def main(mcp_url: str):
    print()
    print("=" * 60)
    print("  Batch Attach MCP to Gumloop Accounts")
    print("=" * 60)
    print(f"  MCP URL: {mcp_url}")
    print()

    # Load all GL accounts from DB
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id, email, gl_status, gl_gummie_id, gl_refresh_token, gl_user_id "
        "FROM accounts WHERE gl_status = 'ok' AND gl_gummie_id != '' AND gl_refresh_token != ''"
    )
    rows = await cur.fetchall()
    accounts = [dict(r) for r in rows]
    await db.close()

    log(f"Found {len(accounts)} GL accounts with status=ok")
    print()

    if not accounts:
        print("  No accounts to process.")
        return

    # Process each account
    results = []
    for i, account in enumerate(accounts, 1):
        log(f"[{i}/{len(accounts)}] {account['email']}...")
        try:
            result = await process_account(account, mcp_url)
            results.append(result)
            status_color = "OK" if result["status"] == "OK" else "FAIL"
            log(f"  → {status_color}: {result['reason']}")
        except Exception as e:
            results.append({"email": account["email"], "status": "ERROR", "reason": str(e)})
            log(f"  → ERROR: {e}")

        # Small delay to avoid rate limiting
        await asyncio.sleep(0.5)

    # Summary
    ok_count = sum(1 for r in results if r["status"] == "OK")
    fail_count = sum(1 for r in results if r["status"] != "OK")

    print()
    print("=" * 60)
    print(f"  DONE: {ok_count} OK, {fail_count} FAILED")
    print("=" * 60)
    for r in results:
        marker = "✓" if r["status"] == "OK" else "✗"
        print(f"  {marker} {r['email']}: {r['reason']}")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch attach MCP to all GL accounts")
    parser.add_argument("--mcp-url", required=True, help="MCP server URL to attach")
    args = parser.parse_args()

    asyncio.run(main(args.mcp_url))
