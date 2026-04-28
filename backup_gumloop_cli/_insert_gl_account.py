"""Insert the auto-registered GL account into proxy database."""
import asyncio
import json
import aiosqlite

async def insert():
    # Load credentials from auto_register result
    with open(r"C:\Users\User\unifiedme-ai\_tmp_auto_mcp\result.json") as f:
        creds = json.load(f)

    db = await aiosqlite.connect(r"C:\Users\User\unifiedme-ai\unified\data\unified.db")

    # Check if already exists
    cur = await db.execute("SELECT id FROM accounts WHERE email = ?", (creds["email"],))
    existing = await cur.fetchone()

    if existing:
        # Update existing
        await db.execute("""
            UPDATE accounts SET
                gl_status = 'ok',
                gl_refresh_token = ?,
                gl_user_id = ?,
                gl_gummie_id = ?,
                gl_id_token = ?,
                gl_error = '',
                gl_error_count = 0
            WHERE email = ?
        """, (
            creds["refresh_token"],
            creds["user_id"],
            creds["gummie_id"],
            creds["id_token"],
            creds["email"],
        ))
        print(f"Updated existing account: {creds['email']} (id={existing[0]})")
    else:
        # Insert new
        await db.execute("""
            INSERT INTO accounts (email, password, status, gl_status, gl_refresh_token, gl_user_id, gl_gummie_id, gl_id_token)
            VALUES (?, '', 'active', 'ok', ?, ?, ?, ?)
        """, (
            creds["email"],
            creds["refresh_token"],
            creds["user_id"],
            creds["gummie_id"],
            creds["id_token"],
        ))
        print(f"Inserted new account: {creds['email']}")

    await db.commit()

    # Verify
    cur = await db.execute(
        "SELECT id, email, gl_status, gl_gummie_id FROM accounts WHERE email = ?",
        (creds["email"],),
    )
    row = await cur.fetchone()
    print(f"Verified: id={row[0]}, email={row[1]}, gl_status={row[2]}, gummie={row[3]}")

    await db.close()

asyncio.run(insert())
