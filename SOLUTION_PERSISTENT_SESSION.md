# Solusi: Session Chat Gumloop Tidak Persistent

## Masalah
Setiap kali prompting, chat membuat session baru (newchat terus) sehingga kehilangan konteks percakapan sebelumnya.

## Root Cause
Kode di `unified/proxy_gumloop.py` sudah mendukung persistent session melalui parameter `chat_session_id`, TAPI:
- Client tidak mengirim `chat_session_id` dalam request body
- Tanpa `chat_session_id`, setiap request generate `interaction_id` baru (line 288-290)
- Setiap `interaction_id` baru = chat session baru di Gumloop

## Alur Existing Code

```python
# File: unified/proxy_gumloop.py (line 274-290)

# 1. Cek apakah ada chat_session_id dari client
chat_session_id = body.get("chat_session_id")

if chat_session_id:
    # 2. Ambil/buat interaction_id dari database
    interaction_id = await db.get_or_create_gumloop_interaction_id(session_id_int)
else:
    # 3. Generate baru setiap request → MASALAH ADA DI SINI
    interaction_id = str(uuid.uuid4()).replace("-", "")[:22]
```

## Solusi: Auto-Persistent Session Per User

Modify `unified/proxy_gumloop.py` untuk otomatis track session per user, tanpa perlu client mengirim `chat_session_id`.

### Implementasi

**File**: `unified/proxy_gumloop.py`

Tambahkan tracking session per account di memory:

```python
# Tambah di bagian atas file (setelah _auth_cache)
# Session cache: account_id → session_id
_session_cache: dict[int, int] = {}

async def _get_or_create_session_for_account(account_id: int, db) -> int:
    """Get or create persistent chat session for account."""
    if account_id in _session_cache:
        session_id = _session_cache[account_id]
        # Verify session still exists
        session = await db.get_chat_session(session_id)
        if session:
            return session_id
    
    # Create new session
    session_id = await db.create_chat_session(
        title=f"Auto Session {account_id}",
        model="gl-claude-sonnet-4-5"
    )
    _session_cache[account_id] = session_id
    log.info("Created persistent session %s for account %s", session_id, account_id)
    return session_id
```

Kemudian modify fungsi `proxy_chat_completions` (line 274-290):

```python
# Handle persistent chat session
interaction_id = None
chat_session_id = body.get("chat_session_id")

if not chat_session_id:
    # AUTO-CREATE: Get/create persistent session for this account
    account_id = account.get("id")
    if account_id:
        session_id = await _get_or_create_session_for_account(account_id, db)
        chat_session_id = session_id
        log.info("Auto-assigned session %s for account %s", session_id, account_id)

if chat_session_id:
    try:
        session_id_int = int(chat_session_id)
        interaction_id = await db.get_or_create_gumloop_interaction_id(session_id_int)
        log.info("Using persistent interaction_id for chat_session_id=%s: %s", chat_session_id, interaction_id)
    except (ValueError, TypeError) as e:
        log.warning("Invalid chat_session_id '%s': %s", chat_session_id, e)

if not interaction_id:
    # Fallback: generate new interaction_id (should rarely happen now)
    interaction_id = str(uuid.uuid4()).replace("-", "")[:22]
    log.warning("Generated one-off interaction_id: %s", interaction_id)
```

## Alternatif: Per-User Per-Model Session

Jika ingin session terpisah per model (agar setiap model punya konteks sendiri):

```python
# Session cache: (account_id, model) → session_id
_session_cache: dict[tuple[int, str], int] = {}

async def _get_or_create_session_for_account(account_id: int, model: str, db) -> int:
    cache_key = (account_id, model)
    if cache_key in _session_cache:
        session_id = _session_cache[cache_key]
        session = await db.get_chat_session(session_id)
        if session:
            return session_id
    
    session_id = await db.create_chat_session(
        title=f"Auto Session {account_id} ({model})",
        model=model
    )
    _session_cache[cache_key] = session_id
    return session_id

# Di proxy_chat_completions:
if not chat_session_id and account_id:
    session_id = await _get_or_create_session_for_account(account_id, gl_model, db)
    chat_session_id = session_id
```

## Testing

Setelah implementasi, test dengan:

```bash
# Terminal 1: Start proxy
python start.py

# Terminal 2: Test persistent session
python test_persistent_chat.py
```

Test script:
```python
import openai

client = openai.OpenAI(
    api_key="test",
    base_url="http://localhost:1430/v1"
)

# Request 1
response1 = client.chat.completions.create(
    model="gl-claude-sonnet-4-5",
    messages=[{"role": "user", "content": "My name is Alice"}]
)
print("1:", response1.choices[0].message.content)

# Request 2 - harus ingat nama
response2 = client.chat.completions.create(
    model="gl-claude-sonnet-4-5",
    messages=[{"role": "user", "content": "What's my name?"}]
)
print("2:", response2.choices[0].message.content)
# Expected: Should remember "Alice"
```

## Files to Modify

1. `unified/proxy_gumloop.py` - Add session auto-creation logic
2. Test dengan existing `test_gumloop_session.py`
