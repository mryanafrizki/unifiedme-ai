#!/usr/bin/env python3
"""Simple test for interaction_id persistence."""

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from unified import database as db


async def test():
    print("\n=== Testing Gumloop Interaction ID Persistence ===\n")
    
    # Initialize
    await db.init_db()
    print("1. Database initialized")
    
    # Create session
    session_id = await db.create_chat_session(
        title="Test Session",
        model="gl-claude-sonnet-4-5"
    )
    print(f"2. Created chat session: {session_id}")
    
    # Get interaction_id (first time)
    iid1 = await db.get_or_create_gumloop_interaction_id(session_id)
    print(f"3. First call - Interaction ID: {iid1} (length: {len(iid1)})")
    
    # Get interaction_id (second time - should be same)
    iid2 = await db.get_or_create_gumloop_interaction_id(session_id)
    print(f"4. Second call - Interaction ID: {iid2}")
    
    if iid1 == iid2:
        print("\n>>> SUCCESS! Interaction ID persists across calls.")
    else:
        print("\n>>> FAILED! IDs don't match.")
        return False
    
    # Verify in database
    session = await db.get_chat_session(session_id)
    if session and session.get("gumloop_interaction_id") == iid1:
        print(f"5. Verified in database: {session.get('gumloop_interaction_id')}")
        print("\n*** ALL CHECKS PASSED ***\n")
        return True
    else:
        print("\n>>> FAILED! Not saved correctly in database.")
        return False


if __name__ == "__main__":
    result = asyncio.run(test())
    sys.exit(0 if result else 1)
