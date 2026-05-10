# Manual Testing Guide - Gumloop Session Persistence

## Prerequisites

1. ✅ Proxy server running (`python start.py`)
2. ✅ Valid Gumloop account configured
3. ✅ API key for authentication
4. ✅ Database migrations applied (automatic on startup)

## Test 1: Basic Persistence Test

### Objective
Verify that AI remembers context when using the same `chat_session_id`.

### Steps

1. **First Request - Teach the AI**
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "My name is Alice and I love Python programming."}
    ],
    "chat_session_id": 1
  }'
```

**Expected**: AI responds with acknowledgment

2. **Second Request - Test Memory**
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "What is my name and what programming language do I like?"}
    ],
    "chat_session_id": 1
  }'
```

**Expected**: AI correctly responds "Your name is Alice and you love Python programming"

✅ **PASS**: AI remembers from first request
❌ **FAIL**: AI says it doesn't know

---

## Test 2: Multiple Independent Sessions

### Objective
Verify that different session IDs maintain separate contexts.

### Steps

1. **Session 1 - Favorite Color: Blue**
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "My favorite color is blue."}
    ],
    "chat_session_id": 1
  }'
```

2. **Session 2 - Favorite Color: Red**
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "My favorite color is red."}
    ],
    "chat_session_id": 2
  }'
```

3. **Back to Session 1 - Check Memory**
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "What is my favorite color?"}
    ],
    "chat_session_id": 1
  }'
```

**Expected**: AI responds "blue"

4. **Check Session 2**
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "What is my favorite color?"}
    ],
    "chat_session_id": 2
  }'
```

**Expected**: AI responds "red"

✅ **PASS**: Each session maintains its own context
❌ **FAIL**: Sessions interfere with each other

---

## Test 3: Backward Compatibility

### Objective
Verify that requests without `chat_session_id` still work (one-off chats).

### Steps

1. **Request 1 - No Session ID**
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "Remember: my secret code is FALCON"}
    ]
  }'
```

**Expected**: AI responds normally

2. **Request 2 - No Session ID (Should NOT Remember)**
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "What is my secret code?"}
    ]
  }'
```

**Expected**: AI says it doesn't know (no memory between requests)

✅ **PASS**: Backward compatibility maintained
❌ **FAIL**: AI remembers without session_id (unexpected behavior)

---

## Test 4: Database Verification

### Objective
Verify that `interaction_id` is correctly saved in database.

### Steps

1. **Create a chat session via database**
```python
import asyncio
from unified import database as db

async def test():
    await db.init_db()
    session_id = await db.create_chat_session(
        title="Test Session",
        model="gl-claude-sonnet-4-5"
    )
    print(f"Created session: {session_id}")
    return session_id

session_id = asyncio.run(test())
```

2. **Make a request with that session_id**
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ],
    "chat_session_id": <session_id>
  }'
```

3. **Check database**
```python
async def check():
    session = await db.get_chat_session(<session_id>)
    print(f"Session: {session}")
    print(f"Interaction ID: {session.get('gumloop_interaction_id')}")
    
asyncio.run(check())
```

**Expected**: 
- `gumloop_interaction_id` is populated
- It's a 22-character alphanumeric string

✅ **PASS**: Database correctly stores interaction_id
❌ **FAIL**: interaction_id is empty or missing

---

## Test 5: Persistence Across Server Restarts

### Objective
Verify that sessions persist after server restart.

### Steps

1. **Create a persistent session**
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "My lucky number is 42."}
    ],
    "chat_session_id": 99
  }'
```

2. **Stop the server**
```bash
# Press Ctrl+C or:
python stop.sh
```

3. **Start the server again**
```bash
python start.py
```

4. **Test memory**
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "What is my lucky number?"}
    ],
    "chat_session_id": 99
  }'
```

**Expected**: AI responds "42"

✅ **PASS**: Session persists across restarts
❌ **FAIL**: AI loses memory after restart

---

## Test 6: Streaming Support

### Objective
Verify that persistence works with streaming responses.

### Python Script
```python
import openai

client = openai.OpenAI(
    api_key="YOUR_API_KEY",
    base_url="http://localhost:1430/v1"
)

# Request 1
print("Request 1: Teaching AI...")
response = client.chat.completions.create(
    model="gl-claude-sonnet-4-5",
    messages=[{"role": "user", "content": "My hobby is photography"}],
    chat_session_id=10,
    stream=True
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print("\n")

# Request 2
print("Request 2: Testing memory...")
response = client.chat.completions.create(
    model="gl-claude-sonnet-4-5",
    messages=[{"role": "user", "content": "What is my hobby?"}],
    chat_session_id=10,
    stream=True
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print("\n")
```

**Expected**: AI correctly responds "photography" in streaming mode

✅ **PASS**: Streaming works with persistence
❌ **FAIL**: Streaming breaks or loses context

---

## Test 7: Invalid Session ID Handling

### Objective
Verify graceful handling of invalid session IDs.

### Steps

1. **Non-existent session ID**
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ],
    "chat_session_id": 99999
  }'
```

**Expected**: Request succeeds (fallback to new UUID)

2. **Invalid session ID (string)**
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ],
    "chat_session_id": "invalid"
  }'
```

**Expected**: 
- Request succeeds (fallback to new UUID)
- Warning logged in server logs

✅ **PASS**: Invalid inputs handled gracefully
❌ **FAIL**: Server crashes or errors

---

## Troubleshooting

### Issue: AI doesn't remember context

**Possible causes**:
1. Different session_id used in requests
2. Database migration didn't run
3. Server using old code (restart needed)

**Solution**:
```bash
# Check database schema
sqlite3 unified/data/database.db
> .schema chat_sessions
# Should show gumloop_interaction_id column

# Restart server
python stop.sh
python start.py
```

### Issue: Database error

**Possible causes**:
1. Database locked (multiple processes)
2. Permission issues
3. Corrupted database

**Solution**:
```bash
# Stop all Python processes
taskkill /IM python.exe /F

# Restart server
python start.py
```

### Issue: Request times out

**Possible causes**:
1. Gumloop API slow
2. Network issues
3. Invalid credentials

**Solution**:
```bash
# Check logs
tail -f unified/logs/*.log

# Verify account status
python -c "
import asyncio
from unified import database as db
async def check():
    accounts = await db.get_accounts()
    for acc in accounts:
        print(acc)
asyncio.run(check())
"
```

---

## Success Criteria

✅ All 7 tests pass
✅ No errors in server logs
✅ Database contains interaction_id for used sessions
✅ Multiple sessions work independently
✅ Backward compatibility maintained

## Reporting Issues

If tests fail, collect:
1. Server logs
2. Database schema (`.schema chat_sessions`)
3. Request/response examples
4. Steps to reproduce

---

**Note**: Remember to replace `YOUR_API_KEY` with actual API key and `<session_id>` with actual session ID in examples above.
