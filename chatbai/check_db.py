"""Check chatbai accounts in DB."""
import asyncio
import aiosqlite

async def main():
    db = await aiosqlite.connect("unified/data/unified.db")
    db.row_factory = aiosqlite.Row
    
    # Check columns exist
    cur = await db.execute("PRAGMA table_info(accounts)")
    cols = [r["name"] for r in await cur.fetchall()]
    cbai_cols = [c for c in cols if "cbai" in c]
    print(f"CBAI columns: {cbai_cols}")
    
    # Check accounts
    cur = await db.execute(
        "SELECT id, email, cbai_status, cbai_api_key FROM accounts WHERE email LIKE '%gemuel%' ORDER BY id DESC LIMIT 10"
    )
    rows = await cur.fetchall()
    if rows:
        for r in rows:
            print(f"  id={r['id']} email={r['email']} cbai_status={r['cbai_status']} api_key={str(r['cbai_api_key'])[:20]}")
    else:
        print("  No gemuel accounts found")
    
    await db.close()

asyncio.run(main())
