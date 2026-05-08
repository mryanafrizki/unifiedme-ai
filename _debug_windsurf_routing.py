"""Debug: why router can't find windsurf account."""
import asyncio
import sys
sys.path.insert(0, ".")
from unified import database as db


async def main():
    await db.init_db()

    # Check account 713 details
    acc = await db.get_account(713)
    if acc:
        print(f"Account 713:")
        print(f"  email: {acc['email']}")
        print(f"  status: {acc['status']}")  # MUST be 'active'
        print(f"  windsurf_status: {acc.get('windsurf_status')}")
        print(f"  windsurf_api_key: {bool(acc.get('windsurf_api_key'))}")
    else:
        print("Account 713 not found!")

    # Try the actual query the router uses
    print("\nTrying get_next_account_for_tier('windsurf')...")
    result = await db.get_next_account_for_tier("windsurf")
    if result:
        print(f"  Found: ID={result['id']} email={result['email']}")
    else:
        print("  None returned!")

    # Manual query to debug
    _db = await db.get_db()
    cur = await _db.execute(
        "SELECT id, email, status, windsurf_status FROM accounts WHERE windsurf_status = 'ok'"
    )
    rows = await cur.fetchall()
    print(f"\nDirect SQL (windsurf_status='ok'): {len(rows)} rows")
    for r in rows:
        print(f"  ID={r[0]} email={r[1]} status={r[2]} windsurf_status={r[3]}")

    # Check if status='active' is the issue
    cur2 = await _db.execute(
        "SELECT id, email, status, windsurf_status FROM accounts WHERE windsurf_status = 'ok' AND status = 'active'"
    )
    rows2 = await cur2.fetchall()
    print(f"\nWith status='active' filter: {len(rows2)} rows")
    for r in rows2:
        print(f"  ID={r[0]} email={r[1]} status={r[2]} windsurf_status={r[3]}")

    await db.close_db()


asyncio.run(main())
