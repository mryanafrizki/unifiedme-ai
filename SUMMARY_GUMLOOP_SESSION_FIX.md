# Summary: Gumloop Chat Session Persistence Fix

## 🎯 Masalah yang Diperbaiki
**Sebelum**: Setiap request ke Gumloop membuat chat session baru (newchat terus)
**Sesudah**: Chat session persisten - AI mengingat konteks percakapan sebelumnya

## 📝 Perubahan yang Dilakukan

### 1. Database Schema (`unified/database.py`)

#### a. Tambah Kolom ke Tabel `chat_sessions`
```python
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT DEFAULT 'New Chat',
    model       TEXT DEFAULT '',
    endpoint    TEXT DEFAULT '',
    api_key     TEXT DEFAULT '',
    gumloop_interaction_id TEXT DEFAULT '',  # ← BARU!
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);
```

#### b. Migration
```python
"ALTER TABLE chat_sessions ADD COLUMN gumloop_interaction_id TEXT DEFAULT ''"
```

#### c. Helper Function Baru
```python
async def get_or_create_gumloop_interaction_id(session_id: int) -> str:
    """
    Get existing Gumloop interaction_id for session, or create new one if missing.
    
    Returns:
        - interaction_id (22 chars) jika session exists
        - Empty string jika session tidak ditemukan
    """
```

### 2. Proxy Modification (`unified/proxy_gumloop.py`)

#### a. Support Parameter `chat_session_id`
```python
async def proxy_chat_completions(
    body: dict,
    account: dict,
    client_wants_stream: bool,
    proxy_url: str | None = None,
):
    # ...
    
    # Handle persistent chat session via chat_session_id
    interaction_id = None
    chat_session_id = body.get("chat_session_id")
    
    if chat_session_id:
        try:
            session_id_int = int(chat_session_id)
            interaction_id = await db.get_or_create_gumloop_interaction_id(session_id_int)
            log.info("Using persistent interaction_id for chat_session_id=%s: %s", 
                     chat_session_id, interaction_id)
        except (ValueError, TypeError) as e:
            log.warning("Invalid chat_session_id '%s': %s", chat_session_id, e)
    
    if not interaction_id:
        # Generate new interaction_id for one-off requests
        interaction_id = str(uuid.uuid4()).replace("-", "")[:22]
```

#### b. Selalu Kirim `interaction_id`
**Sebelum**:
```python
# Hanya kirim interaction_id jika ada gambar
interaction_id=interaction_id if has_images else None
```

**Sesudah**:
```python
# SELALU kirim interaction_id untuk maintain session
interaction_id=interaction_id
```

## 🚀 Cara Penggunaan

### Python OpenAI Client
```python
import openai

client = openai.OpenAI(
    api_key="YOUR_API_KEY",
    base_url="http://localhost:1430/v1"
)

# Request 1: Beri informasi
response1 = client.chat.completions.create(
    model="gl-claude-sonnet-4-5",
    messages=[
        {"role": "user", "content": "My name is Alice and I love Python"}
    ],
    chat_session_id=1  # ← ID session
)

# Request 2: Test memory (akan mengingat nama)
response2 = client.chat.completions.create(
    model="gl-claude-sonnet-4-5",
    messages=[
        {"role": "user", "content": "What is my name?"}
    ],
    chat_session_id=1  # ← Session yang sama
)
# AI akan jawab: "Your name is Alice"
```

### cURL
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [{"role": "user", "content": "Hello"}],
    "chat_session_id": 1
  }'
```

### JavaScript/TypeScript
```typescript
const response = await fetch('http://localhost:1430/v1/chat/completions', {
  method: 'POST',
  headers: {
    'Authorization': 'Bearer YOUR_KEY',
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    model: 'gl-claude-sonnet-4-5',
    messages: [{role: 'user', content: 'Hello'}],
    chat_session_id: 1
  })
});
```

## 🔄 Alur Kerja

```
┌─────────────────────────────────────┐
│ Client Request                      │
│ - model: gl-claude-sonnet-4-5       │
│ - messages: [...]                   │
│ - chat_session_id: 1  ←─────────────┼── KUNCI!
└────────────┬────────────────────────┘
             │
             ▼
┌─────────────────────────────────────┐
│ proxy_gumloop.py                    │
│ 1. Extract chat_session_id          │
│ 2. Call get_or_create_gumloop...()  │
└────────────┬────────────────────────┘
             │
             ▼
┌─────────────────────────────────────┐
│ Database                            │
│ SELECT gumloop_interaction_id       │
│ FROM chat_sessions                  │
│ WHERE id = 1                        │
└────────────┬────────────────────────┘
             │
    ┌────────┴────────┐
    │                 │
    ▼                 ▼
┌─────────┐    ┌──────────────┐
│ Found?  │    │ Not found?   │
│ Return  │    │ Generate new │
│ existing│    │ UUID & save  │
└────┬────┘    └──────┬───────┘
     │                │
     └────────┬───────┘
              │
              ▼
    ┌──────────────────────┐
    │ interaction_id       │
    │ (22 chars UUID)      │
    └──────────┬───────────┘
               │
               ▼
    ┌──────────────────────┐
    │ Send to Gumloop WS   │
    │ with interaction_id  │
    └──────────────────────┘
```

## ✅ Hasil

### Sebelum
```
Request 1: "My name is Alice"
→ interaction_id: abc123... (baru)

Request 2: "What is my name?"
→ interaction_id: xyz789... (baru lagi!)
→ AI: "I don't know your name" ❌
```

### Sesudah
```
Request 1: "My name is Alice"  [chat_session_id=1]
→ interaction_id: abc123... (dibuat & disimpan)

Request 2: "What is my name?"  [chat_session_id=1]
→ interaction_id: abc123... (yang sama!)
→ AI: "Your name is Alice" ✅
```

## 📋 Testing

Untuk test manual:
```bash
# 1. Create chat session di database
# 2. Kirim request dengan chat_session_id
# 3. Verify interaction_id tetap sama di request berikutnya
```

Script test tersedia di:
- `test_gumloop_session.py` - Full test suite
- `test_interaction_id_simple.py` - Simple verification

## 🔒 Backward Compatibility

✅ **Fully backward compatible!**

- Request **TANPA** `chat_session_id` → Berfungsi seperti biasa (one-off chat)
- Request **DENGAN** `chat_session_id` → Persistent session

Tidak ada breaking changes untuk client yang sudah ada.

## 📁 Files Modified

1. **unified/database.py**
   - Schema update (chat_sessions table)
   - Migration script
   - New helper function

2. **unified/proxy_gumloop.py**
   - Parameter handling
   - interaction_id logic
   - Always send interaction_id (not conditional)

## 📚 Dokumentasi

Lihat `GUMLOOP_SESSION_FIX.md` untuk detail lengkap.

---

**Status**: ✅ COMPLETE & TESTED
**Migration**: ✅ AUTOMATIC (runs on startup)
**Breaking Changes**: ❌ NONE (fully backward compatible)
