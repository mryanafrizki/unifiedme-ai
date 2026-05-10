#!/usr/bin/env python3
"""Test Gumloop chat session persistence.

Tests:
1. Database helper function get_or_create_gumloop_interaction_id
2. Interaction ID persistence across multiple calls
3. Different sessions have different interaction IDs
"""

import asyncio
import sys
from pathlib import Path

# Add unified to path
sys.path.insert(0, str(Path(__file__).parent))

from unified import database as db


async def test_database_helper():
    """Test the database helper function."""
    print("=" * 70)
    print("TEST 1: Database Helper Function")
    print("=" * 70)
    
    # Initialize database
    await db.init_db()
    
    # Create a test chat session
    session_id = await db.create_chat_session(
        title="Test Persistent Session",
        model="gl-claude-sonnet-4-5"
    )
    print(f"✓ Created chat session ID: {session_id}")
    
    # Get interaction_id (should create new one)
    interaction_id_1 = await db.get_or_create_gumloop_interaction_id(session_id)
    print(f"✓ First call - Interaction ID: {interaction_id_1}")
    assert len(interaction_id_1) == 22, "Interaction ID should be 22 chars"
    
    # Get again (should return same ID)
    interaction_id_2 = await db.get_or_create_gumloop_interaction_id(session_id)
    print(f"✓ Second call - Interaction ID: {interaction_id_2}")
    assert interaction_id_1 == interaction_id_2, "Should return same interaction_id"
    
    print("✅ TEST 1 PASSED: Interaction ID persists!\n")
    return session_id


async def test_multiple_sessions():
    """Test that different sessions get different interaction IDs."""
    print("=" * 70)
    print("TEST 2: Multiple Sessions")
    print("=" * 70)
    
    # Create two sessions
    session_1 = await db.create_chat_session(title="Session 1", model="gl-claude-sonnet-4-5")
    session_2 = await db.create_chat_session(title="Session 2", model="gl-claude-sonnet-4-5")
    
    print(f"✓ Created session 1: {session_1}")
    print(f"✓ Created session 2: {session_2}")
    
    # Get interaction IDs
    interaction_1 = await db.get_or_create_gumloop_interaction_id(session_1)
    interaction_2 = await db.get_or_create_gumloop_interaction_id(session_2)
    
    print(f"✓ Session 1 interaction_id: {interaction_1}")
    print(f"✓ Session 2 interaction_id: {interaction_2}")
    
    assert interaction_1 != interaction_2, "Different sessions should have different interaction_ids"
    print("✅ TEST 2 PASSED: Each session has unique interaction_id!\n")


async def test_session_retrieval():
    """Test retrieving session with interaction_id."""
    print("=" * 70)
    print("TEST 3: Session Retrieval")
    print("=" * 70)
    
    session_id = await db.create_chat_session(
        title="Retrieval Test",
        model="gl-claude-sonnet-4-5"
    )
    
    # Generate interaction_id
    interaction_id = await db.get_or_create_gumloop_interaction_id(session_id)
    print(f"✓ Created session {session_id} with interaction_id: {interaction_id}")
    
    # Retrieve session
    session = await db.get_chat_session(session_id)
    assert session is not None, "Session should exist"
    assert session["gumloop_interaction_id"] == interaction_id, "Should have correct interaction_id"
    
    print(f"✓ Retrieved session: {session['title']}")
    print(f"✓ Confirmed interaction_id: {session['gumloop_interaction_id']}")
    print("✅ TEST 3 PASSED: Session retrieval works!\n")


async def test_nonexistent_session():
    """Test behavior with non-existent session ID."""
    print("=" * 70)
    print("TEST 4: Non-existent Session")
    print("=" * 70)
    
    # Try to get interaction_id for non-existent session
    interaction_id = await db.get_or_create_gumloop_interaction_id(99999)
    
    assert interaction_id == "", "Should return empty string for non-existent session"
    print("✓ Non-existent session returns empty string")
    print("✅ TEST 4 PASSED: Handles non-existent sessions gracefully!\n")


async def main():
    """Run all tests."""
    print("\n")
    print("=" * 70)
    print(" " * 15 + "GUMLOOP SESSION PERSISTENCE TESTS")
    print("=" * 70)
    print()
    
    try:
        await test_database_helper()
        await test_multiple_sessions()
        await test_session_retrieval()
        await test_nonexistent_session()
        
        print("=" * 70)
        print("*** ALL TESTS PASSED! ***")
        print("=" * 70)
        print("\nGumloop chat sessions are now persistent!")
        print("\nTo use in your application:")
        print("  1. Create a chat session: session_id = await db.create_chat_session(...)")
        print("  2. Send requests with: body['chat_session_id'] = session_id")
        print("  3. All requests with same session_id will share the same conversation!")
        print()
        
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
