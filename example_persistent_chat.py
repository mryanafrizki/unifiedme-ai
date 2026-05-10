#!/usr/bin/env python3
"""
Example: Persistent Chat Session with Gumloop

Demonstrates how to use chat_session_id to maintain conversation context.
"""

import openai
import time

# Configure OpenAI client to use local proxy
client = openai.OpenAI(
    api_key="YOUR_API_KEY_HERE",  # Replace with your API key
    base_url="http://localhost:1430/v1"
)

# Session ID - use different numbers for different conversations
# In a real app, you'd store this in your database
SESSION_ID = 1

def chat(message: str, session_id: int = SESSION_ID):
    """Send a message in a persistent chat session."""
    print(f"\n👤 You: {message}")
    print("🤖 AI: ", end="", flush=True)
    
    response = client.chat.completions.create(
        model="gl-claude-sonnet-4-5",
        messages=[
            {"role": "user", "content": message}
        ],
        chat_session_id=session_id,  # ← This makes the session persistent!
        stream=True
    )
    
    full_response = ""
    for chunk in response:
        if chunk.choices[0].delta.content:
            content = chunk.choices[0].delta.content
            print(content, end="", flush=True)
            full_response += content
    
    print()  # New line after response
    return full_response


def example_conversation():
    """Example conversation showing memory persistence."""
    
    print("\n" + "="*70)
    print("  PERSISTENT CHAT SESSION EXAMPLE")
    print("="*70)
    print(f"\nSession ID: {SESSION_ID}")
    print("All messages in this session will share the same context.\n")
    
    # Turn 1: Introduce yourself
    print("\n--- Turn 1: Teaching the AI ---")
    chat("Hi! My name is Alice and I'm a Python developer. I love working on AI projects.")
    time.sleep(1)
    
    # Turn 2: Ask AI to remember
    print("\n--- Turn 2: Testing Memory ---")
    chat("What is my name and what do I do?")
    # Expected: AI should remember "Alice" and "Python developer"
    time.sleep(1)
    
    # Turn 3: Continue the context
    print("\n--- Turn 3: Building on Context ---")
    chat("Can you suggest a good Python library for my AI projects?")
    # Expected: AI knows we're talking about Python & AI
    time.sleep(1)
    
    print("\n" + "="*70)
    print("  ✅ Conversation Complete!")
    print("="*70)
    print("\nNotice how the AI remembered:")
    print("  - Your name (Alice)")
    print("  - Your profession (Python developer)")
    print("  - The context (AI projects)")
    print("\nThis is because all messages used the same chat_session_id!")


def example_multiple_sessions():
    """Example showing different sessions are independent."""
    
    print("\n" + "="*70)
    print("  MULTIPLE SESSIONS EXAMPLE")
    print("="*70)
    
    # Session 1: Personal chat
    print("\n--- Session 1: Personal Chat ---")
    chat("My favorite color is blue", session_id=1)
    time.sleep(1)
    
    # Session 2: Different topic
    print("\n--- Session 2: Work Chat ---")
    chat("My favorite color is red", session_id=2)
    time.sleep(1)
    
    # Back to Session 1
    print("\n--- Back to Session 1 ---")
    response1 = chat("What's my favorite color?", session_id=1)
    # Expected: "blue"
    time.sleep(1)
    
    # Back to Session 2
    print("\n--- Back to Session 2 ---")
    response2 = chat("What's my favorite color?", session_id=2)
    # Expected: "red"
    
    print("\n" + "="*70)
    print("  ✅ Multiple Sessions Working!")
    print("="*70)
    print("\nEach session maintains its own separate context:")
    print(f"  - Session 1 remembers: blue")
    print(f"  - Session 2 remembers: red")


def example_onetime_chat():
    """Example of one-time chat (no session persistence)."""
    
    print("\n" + "="*70)
    print("  ONE-TIME CHAT (No Session ID)")
    print("="*70)
    
    # Without chat_session_id - each request is independent
    print("\n--- Request 1 ---")
    response = client.chat.completions.create(
        model="gl-claude-sonnet-4-5",
        messages=[
            {"role": "user", "content": "Remember: my code name is Falcon"}
        ]
        # No chat_session_id!
    )
    print(f"🤖 AI: {response.choices[0].message.content}")
    
    print("\n--- Request 2 (Should NOT remember) ---")
    response = client.chat.completions.create(
        model="gl-claude-sonnet-4-5",
        messages=[
            {"role": "user", "content": "What is my code name?"}
        ]
        # No chat_session_id!
    )
    print(f"🤖 AI: {response.choices[0].message.content}")
    
    print("\n" + "="*70)
    print("  ✅ One-Time Chat (No Persistence)")
    print("="*70)
    print("\nWithout chat_session_id, each request is independent.")
    print("The AI won't remember previous messages.")


if __name__ == "__main__":
    print("\n🚀 Starting Gumloop Persistent Chat Examples")
    print("=" * 70)
    
    try:
        # Example 1: Single persistent conversation
        example_conversation()
        
        input("\n\nPress Enter to continue to next example...")
        
        # Example 2: Multiple independent sessions
        example_multiple_sessions()
        
        input("\n\nPress Enter to continue to next example...")
        
        # Example 3: One-time chat (no persistence)
        example_onetime_chat()
        
        print("\n\n✨ All examples complete!")
        print("\nTo use in your app:")
        print("  1. Create a chat session (or use existing ID)")
        print("  2. Add 'chat_session_id' to your API requests")
        print("  3. All requests with same ID will share context!")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("\nMake sure:")
        print("  1. The proxy server is running (python start.py)")
        print("  2. You've set the correct API key")
        print("  3. You have a Gumloop account configured")
