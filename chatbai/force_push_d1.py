"""Force push all local accounts to D1 (including cbai_* fields).

Run this on the device that has chatbai accounts in local SQLite.
Usage: python chatbai/force_push_d1.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

async def main():
    from unified import database as db
    from unified import license_client

    # Init DB
    await db.init_db()

    # Read license
    license_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "unified", "data", ".license")
    if os.path.exists(license_path):
        with open(license_path) as f:
            os.environ["LICENSE_KEY"] = f.read().strip()

    # Activate license
    ok = await license_client.activate()
    if not ok:
        print("License activation failed!")
        return

    # Get all accounts
    accounts = await db.get_accounts()
    cbai_accounts = [a for a in accounts if a.get("cbai_status") == "ok"]
    print(f"Total accounts: {len(accounts)}")
    print(f"CBAI accounts (status=ok): {len(cbai_accounts)}")

    if not cbai_accounts:
        print("\nNo CBAI accounts found in local DB. Nothing to push.")
        await db.close_db()
        return

    for a in cbai_accounts:
        print(f"  {a['email']}: cbai_status={a.get('cbai_status')} key={str(a.get('cbai_api_key', ''))[:20]}...")

    # Push ALL accounts (not just cbai ones — full sync)
    print(f"\nPushing {len(accounts)} accounts to D1...")
    result = await license_client.push_sync(accounts=accounts)
    print(f"Result: {result}")

    await db.close_db()

asyncio.run(main())
