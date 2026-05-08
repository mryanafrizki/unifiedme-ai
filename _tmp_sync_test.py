"""Test D1 sync: push local → pull back → compare counts."""
import asyncio, sys, os
sys.path.insert(0, "C:/Users/User/unifiedme-ai")
os.environ["LICENSE_KEY"] = open("C:/Users/User/unifiedme-ai/unified/data/.license").read().strip()

async def main():
    from unified import database as db
    from unified import license_client as lc

    await db.init_db()
    ok = await lc.activate()
    if not ok:
        print("License failed")
        return

    # Current local counts
    local = await db.get_accounts()
    kr = sum(1 for a in local if a.get("kiro_status") == "ok")
    cb = sum(1 for a in local if a.get("cb_status") == "ok")
    ws = sum(1 for a in local if a.get("ws_status") == "ok")
    gl = sum(1 for a in local if a.get("gl_status") == "ok")
    active = sum(1 for a in local if a.get("status") == "active")
    print(f"LOCAL BEFORE: KR:{kr} CB:{cb} WS:{ws} GL:{gl} Active:{active} Total:{len(local)}")

    # Push to D1
    push = await lc.push_sync(accounts=local)
    pushed = push.get("accounts_upserted", 0)
    print(f"PUSHED to D1: {pushed} accounts")

    # Pull from D1 (full replace)
    pull = await lc.full_pull_replace_local()
    print(f"PULLED from D1: +{pull.get('added',0)} ~{pull.get('updated',0)} -{pull.get('deleted',0)} = {pull.get('total',0)}")

    # Local counts after pull
    local2 = await db.get_accounts()
    kr2 = sum(1 for a in local2 if a.get("kiro_status") == "ok")
    cb2 = sum(1 for a in local2 if a.get("cb_status") == "ok")
    ws2 = sum(1 for a in local2 if a.get("ws_status") == "ok")
    gl2 = sum(1 for a in local2 if a.get("gl_status") == "ok")
    active2 = sum(1 for a in local2 if a.get("status") == "active")
    print(f"LOCAL AFTER:  KR:{kr2} CB:{cb2} WS:{ws2} GL:{gl2} Active:{active2} Total:{len(local2)}")

    # Compare
    match = (kr == kr2 and cb == cb2 and ws == ws2 and gl == gl2 and len(local) == len(local2))
    if match:
        print("\nMATCH - D1 and local are in sync!")
    else:
        print("\nMISMATCH:")
        if kr != kr2: print(f"  KR: {kr} -> {kr2}")
        if cb != cb2: print(f"  CB: {cb} -> {cb2}")
        if ws != ws2: print(f"  WS: {ws} -> {ws2}")
        if gl != gl2: print(f"  GL: {gl} -> {gl2}")
        if len(local) != len(local2): print(f"  Total: {len(local)} -> {len(local2)}")

    await db.close_db()

asyncio.run(main())
