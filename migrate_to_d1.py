"""Migrate local SQLite accounts to central D1 database.

Usage:
    python migrate_to_d1.py --license-key UNIF-XXXXX-... [--db-path unified/data/unified.db]

Reads all accounts from local SQLite and pushes them to D1 via the sync API.
Also migrates settings, filters, and proxies.
"""

import argparse
import asyncio
import json
import sqlite3
import sys

import httpx

CENTRAL_API_URL = "https://unified-api.roubot71.workers.dev"
BATCH_SIZE = 20  # accounts per push (avoid hitting D1 query limits)


def read_local_db(db_path: str) -> dict:
    """Read all data from local SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Accounts
    cur = conn.execute("SELECT * FROM accounts ORDER BY id ASC")
    accounts = [dict(row) for row in cur.fetchall()]

    # Settings
    cur = conn.execute("SELECT key, value FROM settings")
    settings = {row["key"]: row["value"] for row in cur.fetchall()}

    # Filters
    cur = conn.execute("SELECT * FROM filters ORDER BY id ASC")
    filters = [dict(row) for row in cur.fetchall()]

    # Proxies
    cur = conn.execute("SELECT * FROM proxies ORDER BY id ASC")
    proxies = [dict(row) for row in cur.fetchall()]

    conn.close()

    return {
        "accounts": accounts,
        "settings": settings,
        "filters": filters,
        "proxies": proxies,
    }


async def push_accounts(license_key: str, accounts: list[dict], device_fp: str, api_url: str = CENTRAL_API_URL) -> int:
    """Push accounts in batches to D1."""
    total = 0
    for i in range(0, len(accounts), BATCH_SIZE):
        batch = accounts[i:i + BATCH_SIZE]
        # Clean account data for D1 (remove local-only fields)
        clean = []
        for acc in batch:
            clean.append({
                "email": acc["email"],
                "password": acc["password"],
                "status": acc.get("status", "active"),
                "kiro_status": acc.get("kiro_status", "pending"),
                "kiro_access_token": acc.get("kiro_access_token", ""),
                "kiro_refresh_token": acc.get("kiro_refresh_token", ""),
                "kiro_profile_arn": acc.get("kiro_profile_arn", ""),
                "kiro_credits": acc.get("kiro_credits", 0),
                "kiro_credits_total": acc.get("kiro_credits_total", 0),
                "kiro_credits_used": acc.get("kiro_credits_used", 0),
                "kiro_error": acc.get("kiro_error", ""),
                "kiro_error_count": acc.get("kiro_error_count", 0),
                "kiro_expires_at": acc.get("kiro_expires_at", ""),
                "cb_status": acc.get("cb_status", "pending"),
                "cb_api_key": acc.get("cb_api_key", ""),
                "cb_credits": acc.get("cb_credits", 0),
                "cb_error": acc.get("cb_error", ""),
                "cb_error_count": acc.get("cb_error_count", 0),
                "cb_expires_at": acc.get("cb_expires_at", ""),
                "ws_status": acc.get("ws_status", "none"),
                "ws_api_key": acc.get("ws_api_key", ""),
                "ws_credits": acc.get("ws_credits", 0),
                "ws_error": acc.get("ws_error", ""),
                "ws_error_count": acc.get("ws_error_count", 0),
                "gl_status": acc.get("gl_status", "none"),
                "gl_refresh_token": acc.get("gl_refresh_token", ""),
                "gl_user_id": acc.get("gl_user_id", ""),
                "gl_gummie_id": acc.get("gl_gummie_id", ""),
                "gl_id_token": acc.get("gl_id_token", ""),
                "gl_credits": acc.get("gl_credits", 0),
                "gl_error": acc.get("gl_error", ""),
                "gl_error_count": acc.get("gl_error_count", 0),
            })

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{CENTRAL_API_URL}/api/sync/push",
                json={
                    "license_key": license_key,
                    "device_fingerprint": device_fp,
                    "accounts": clean,
                },
                headers={"Content-Type": "application/json"},
            )
            result = resp.json()
            if result.get("error"):
                print(f"  ERROR batch {i//BATCH_SIZE + 1}: {result['error']}")
            else:
                upserted = result.get("accounts_upserted", 0)
                total += upserted
                print(f"  Batch {i//BATCH_SIZE + 1}: {upserted} accounts pushed ({i+len(batch)}/{len(accounts)})")

    return total


async def push_settings(license_key: str, settings: dict, device_fp: str, api_url: str = CENTRAL_API_URL) -> int:
    """Push settings to D1."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CENTRAL_API_URL}/api/sync/push",
            json={
                "license_key": license_key,
                "device_fingerprint": device_fp,
                "settings": settings,
            },
            headers={"Content-Type": "application/json"},
        )
        result = resp.json()
        return result.get("settings_upserted", 0)


async def push_proxies(license_key: str, proxies: list[dict], device_fp: str, api_url: str = CENTRAL_API_URL) -> int:
    """Push proxies to D1."""
    clean = []
    for p in proxies:
        clean.append({
            "url": p.get("url", ""),
            "label": p.get("label", ""),
            "type": p.get("type", "http"),
            "purpose": p.get("purpose", "api"),
            "checked": p.get("checked", 0),
            "active": p.get("active", 1),
            "last_latency_ms": p.get("last_latency_ms", -1),
            "last_tested": p.get("last_tested", ""),
            "last_error": p.get("last_error", ""),
        })

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CENTRAL_API_URL}/api/sync/push",
            json={
                "license_key": license_key,
                "device_fingerprint": device_fp,
                "proxies": clean,
            },
            headers={"Content-Type": "application/json"},
        )
        result = resp.json()
        return result.get("proxies_upserted", 0)


async def main():
    parser = argparse.ArgumentParser(description="Migrate local SQLite to D1")
    parser.add_argument("--license-key", required=True, help="License key (UNIF-XXXXX-...)")
    parser.add_argument("--db-path", default="unified/data/unified.db", help="Path to local SQLite DB")
    parser.add_argument("--api-url", default=CENTRAL_API_URL, help="Central API URL")
    args = parser.parse_args()

    api_url = args.api_url

    print(f"Reading local DB: {args.db_path}")
    data = read_local_db(args.db_path)
    print(f"  Found: {len(data['accounts'])} accounts, {len(data['settings'])} settings, "
          f"{len(data['filters'])} filters, {len(data['proxies'])} proxies")

    # First activate to get device binding
    print(f"\nActivating license {args.license_key[:15]}...")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{api_url}/api/auth/activate",
            json={
                "license_key": args.license_key,
                "device_fingerprint": "migration-script",
                "device_name": "Migration Script",
            },
            headers={"Content-Type": "application/json"},
        )
        result = resp.json()
        if result.get("error"):
            print(f"  ERROR: {result['error']}")
            sys.exit(1)
        print(f"  License OK: {result.get('license', {}).get('tier')} tier, device_id={result.get('device_id')}")

    device_fp = "migration-script"

    # Push accounts
    print(f"\nPushing {len(data['accounts'])} accounts...")
    acc_count = await push_accounts(args.license_key, data["accounts"], device_fp, api_url)
    print(f"  Total: {acc_count} accounts migrated")

    # Push settings
    if data["settings"]:
        print(f"\nPushing {len(data['settings'])} settings...")
        set_count = await push_settings(args.license_key, data["settings"], device_fp, api_url)
        print(f"  Total: {set_count} settings migrated")

    # Push proxies
    if data["proxies"]:
        print(f"\nPushing {len(data['proxies'])} proxies...")
        prx_count = await push_proxies(args.license_key, data["proxies"], device_fp, api_url)
        print(f"  Total: {prx_count} proxies migrated")

    print(f"\nMigration complete!")
    print(f"   License: {args.license_key}")
    print(f"   Accounts: {acc_count}")
    print(f"   Admin panel: {api_url}/admin")


if __name__ == "__main__":
    asyncio.run(main())
