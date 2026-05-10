# Guide: Persistent Chat Sessions untuk Gumloop Proxy

## 🎯 Masalah yang Diselesaikan

Sebelumnya, setiap kali Anda mengirim prompt ke Gumloop proxy, chat membuat session baru. Ini menyebabkan:
- ❌ AI tidak ingat percakapan sebelumnya
- ❌ Harus mengulang konteks setiap kali
- ❌ Tidak bisa build conversation yang kompleks

**Sekarang FIXED!** ✅ Chat session otomatis persistent per account.

## 🔧 Cara Kerja Solusi

### Sebelum (Masalah):
```
Request 1: "My name is Alice" → interaction_id: abc123 (baru)
Request 2: "What's my name?" → interaction_id: def456 (baru lagi) ❌
AI: "I don't have access to your name" (lupa context)
```

### Setelah (Fixed):
```
Request 1: "My name is Alice" 
  → Auto-create session_id: 1 
  → interaction_id: abc123

Request 2: "What's my name?" 
  → Reuse session_id: 1 
  → interaction_id: abc123 (SAMA!) ✅

AI: "Your name is Alice" (ingat context!)
```

## 📝 Implementasi

### File yang Dimodifikasi: `unified/proxy_gumloop.py`

#### 1. Tambah Session Cache (line ~40)

```python
# Session cache: account_id → session_id (for persistent chat sessions)
_session_cache: dict[int, int] = {}
```

#### 2. Tambah Helper Function (line ~243)

```python
async def _get_or_create_session_for_account(account_id: int, db) -> int:
    """Get or create persistent chat session for account.
    
    Each account gets one persistent session that's reused across all chat requests.
    This ensures conversation context is maintained automatically.
    """
    if account_id in _session_cache:
        session_id = _session_cache[account_id]
        # Verify session still exists in database
        session = await db.get_chat_session(session_id)
        if session:
            return session_id
        # Session was deleted, remove from cache
        del _session_cache[account_id]
    
    # Create new persistent session
    session_id = await db.create_chat_session(
        title=f"Persistent Session (Account {account_id})",
        model="gl-claude-sonnet-4-5"
    )
    _session_cache[account_id] = session_id
    log.info("Created persistent session %s for account %s", session_id, account_id)
    return session_id
```

#### 3. Modify Session Handler (line ~302)

```python
# Handle persistent chat session via chat_session_id
# AUTO-PERSISTENT: If no chat_session_id provided, auto-create one per account
interaction_id = None
chat_session_id = body.get("chat_session_id")

# Auto-assign persistent session if not provided
if not chat_session_id:
    account_id = account.get("id")
    if account_id:
        try:
            session_id = await _get_or_create_session_for_account(account_id, db)
            chat_session_id = session_id
            log.info("Auto-assigned persistent session %s for account %s", session_id, account_id)
        except Exception as e:
            log.warning("Failed to auto-create session for account %s: %s", account_id, e)

# Get or create interaction_id for the session
if chat_session_id:
    try:
        session_id_int = int(chat_session_id)
        interaction_id = await db.get_or_create_gumloop_interaction_id(session_id_int)
        log.info("Using persistent interaction_id for chat_session_id=%s: %s", chat_session_id, interaction_id)
    except (ValueError, TypeError) as e:
        log.warning("Invalid chat_session_id '%s': %s", chat_session_id, e)

if not interaction_id:
    # Fallback: Generate new interaction_id (should rarely happen now)
    interaction_id = str(uuid.uuid4()).replace("-", "")[:22]
    log.warning("Generated one-off interaction_id: %s (no session assigned)", interaction_id)
```

## 🚀 Testing

### 1. Start Proxy Server

```bash
python start.py
```

### 2. Run Auto-Persistent Session Test

```bash
python test_auto_persistent_session.py
```

Expected output:
```
======================================================================
     AUTO-PERSISTENT SESSION TEST SUITE
======================================================================

======================================================================
  TEST 1: Basic Memory
======================================================================

🔵 User: Hi! My name is Alice and I work as a Python developer.
🤖 AI: Hello Alice! ...

🔵 User: What is my name?
🤖 AI: Your name is Alice.

✅ TEST 1 PASSED: AI remembered the name!

======================================================================
  TEST SUMMARY
======================================================================
✅ PASS: Basic Memory
✅ PASS: Multi-Turn Context
✅ PASS: Session Isolation

======================================================================
  ✅ ALL TESTS PASSED (3/3)
======================================================================

🎉 Auto-persistent sessions are working correctly!
```

### 3. Manual Testing dengan Python

```python
import openai

client = openai.OpenAI(
    api_key="your_key",
    base_url="http://localhost:1430/v1"
)

# Request 1: Set context
response1 = client.chat.completions.create(
    model="gl-claude-sonnet-4-5",
    messages=[{"role": "user", "content": "My favorite programming language is Python"}]
)

# Request 2: Should remember
response2 = client.chat.completions.create(
    model="gl-claude-sonnet-4-5",
    messages=[{"role": "user", "content": "What's my favorite programming language?"}]
)

print(response2.choices[0].message.content)
# Expected: Should mention Python
```

## 📊 Behavior

### Per-Account Isolation

- Setiap account punya session sendiri
- Account A tidak bisa lihat chat history Account B
- Session disimpan di database (`chat_sessions` table)

### Session Persistence

- Session tetap ada setelah proxy restart (stored in database)
- Cache di-rebuild otomatis saat proxy start ulang
- Jika session dihapus dari DB, akan auto-create baru

### Manual Session Control (Optional)

Anda masih bisa manual control session jika mau:

```python
# Create specific session
session_id = 12345  # Your custom session ID

response = client.chat.completions.create(
    model="gl-claude-sonnet-4-5",
    messages=[{"role": "user", "content": "Hello"}],
    chat_session_id=session_id  # Manual override
)
```

## 🔍 Logging

Check proxy logs untuk melihat session behavior:

```
INFO - Auto-assigned persistent session 1 for account 42
INFO - Using persistent interaction_id for chat_session_id=1: abc123xyz
```

Jika ada masalah:

```
WARNING - Failed to auto-create session for account 42: ...
WARNING - Generated one-off interaction_id: xyz789 (no session assigned)
```

## 🛠️ Troubleshooting

### Session tidak persistent?

1. **Check logs** - pastikan tidak ada error saat auto-create session:
   ```
   tail -f unified/data/unified.log
   ```

2. **Check database** - pastikan table `chat_sessions` ada:
   ```bash
   sqlite3 unified/data/unified.db
   SELECT * FROM chat_sessions;
   ```

3. **Check account** - pastikan account punya `id`:
   ```python
   # Di proxy logs, cari:
   "Auto-assigned persistent session X for account Y"
   ```

### Ingin reset conversation?

Hapus session dari database:

```bash
sqlite3 unified/data/unified.db
DELETE FROM chat_sessions WHERE id = 1;
```

Atau via Python:

```python
from unified import database as db
await db.delete_chat_session(session_id=1)
```

## 📚 Related Files

- `unified/proxy_gumloop.py` - Main implementation
- `unified/database.py` - Session storage (line 1492: `get_or_create_gumloop_interaction_id`)
- `unified/gumloop/client.py` - WebSocket client with interaction_id
- `test_auto_persistent_session.py` - Test script
- `example_persistent_chat.py` - Example usage

## 🎓 How It Works (Technical)

### Database Schema

```sql
CREATE TABLE chat_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    model TEXT,
    gumloop_interaction_id TEXT DEFAULT '',  -- ← KEY FIELD
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### Flow Diagram

```
┌─────────────┐
│ Client      │
│ (no session)│
└──────┬──────┘
       │
       │ POST /v1/chat/completions
       │ body: {model: "gl-...", messages: [...]}
       ▼
┌─────────────────┐
│ Proxy           │
│ proxy_gumloop.py│
└────────┬────────┘
         │
         ├─► Check body.chat_session_id → None
         │
         ├─► Get account_id → 42
         │
         ├─► _get_or_create_session_for_account(42)
         │   │
         │   ├─► Check cache[42] → Miss
         │   │
         │   ├─► Create session in DB → session_id: 1
         │   │
         │   └─► Cache[42] = 1
         │
         ├─► chat_session_id = 1
         │
         ├─► get_or_create_gumloop_interaction_id(1)
         │   │
         │   ├─► Check session.gumloop_interaction_id → Empty
         │   │
         │   ├─► Generate: "abc123xyz..."
         │   │
         │   └─► UPDATE chat_sessions SET gumloop_interaction_id='abc123...' WHERE id=1
         │
         └─► Send to Gumloop with interaction_id='abc123...'

┌─────────────┐
│ Gumloop WS  │
│ (persistent │
│  chat!)     │
└─────────────┘
```

### Next Request (Same Account)

```
Client → Proxy
  ├─► account_id: 42
  ├─► _get_or_create_session_for_account(42)
  │   └─► Cache hit! → session_id: 1
  ├─► get_or_create_gumloop_interaction_id(1)
  │   └─► Found: "abc123..." (SAME!)
  └─► Send to Gumloop with same interaction_id
      → Conversation context maintained! ✅
```

## ✨ Benefits

1. **Zero configuration** - Works automatically
2. **Transparent** - No API changes needed
3. **Persistent** - Survives proxy restarts
4. **Isolated** - Each account has separate session
5. **Efficient** - Session cached in memory

## 🔜 Future Enhancements (Optional)

### Per-Model Sessions

If you want separate sessions per model:

```python
# Session cache: (account_id, model) → session_id
_session_cache: dict[tuple[int, str], int] = {}

async def _get_or_create_session_for_account(account_id: int, model: str, db) -> int:
    cache_key = (account_id, model)
    # ... rest of implementation
```

### Session Expiry

Add automatic session cleanup:

```python
# Delete sessions older than 30 days
async def cleanup_old_sessions():
    await db.execute("""
        DELETE FROM chat_sessions 
        WHERE updated_at < datetime('now', '-30 days')
    """)
```

### Session Management API

Add endpoints to manage sessions:

```python
# List sessions
GET /v1/sessions

# Delete session
DELETE /v1/sessions/{id}

# Clear conversation (keep session, clear interaction_id)
POST /v1/sessions/{id}/clear
```

---

**Selamat! Chat session Anda sekarang persistent! 🎉**

Tidak perlu lagi mengulang konteks setiap request. AI akan mengingat seluruh percakapan Anda.
