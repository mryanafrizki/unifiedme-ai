# Gumloop Chat Session Persistence Fix

## Masalah
Sebelumnya, setiap request ke Gumloop proxy membuat chat session baru (interaction_id baru). Ini menyebabkan:
- AI kehilangan konteks dari percakapan sebelumnya
- Setiap prompt dianggap sebagai percakapan baru
- Tidak ada kontinuitas dalam sesi chat

## Penyebab
Di `unified/proxy_gumloop.py` baris 270-271, setiap request membuat `interaction_id` baru:
```python
# Generate interaction_id per request (each OpenAI call = fresh Gumloop turn)
interaction_id = str(uuid.uuid4()).replace("-", "")[:22]
```

Dan `interaction_id` hanya dikirim ke Gumloop jika ada gambar:
```python
interaction_id=interaction_id if has_images else None
```

## Solusi

### 1. Database Schema Update
Menambahkan kolom `gumloop_interaction_id` ke tabel `chat_sessions`:

```sql
ALTER TABLE chat_sessions ADD COLUMN gumloop_interaction_id TEXT DEFAULT '';
```

### 2. Helper Function
Fungsi baru di `unified/database.py`:

```python
async def get_or_create_gumloop_interaction_id(session_id: int) -> str:
    """Get existing Gumloop interaction_id for session, or create new one if missing."""
```

### 3. Proxy Modification
Update `unified/proxy_gumloop.py`:
- Accept `chat_session_id` dari request body
- Retrieve/create persistent `interaction_id` untuk session tersebut
- **SELALU** kirim `interaction_id` ke Gumloop (bukan hanya untuk gambar)

## Cara Menggunakan

### Option 1: Via OpenAI API Client
Kirim `chat_session_id` di body request:

```python
import openai

client = openai.OpenAI(
    api_key="YOUR_API_KEY",
    base_url="http://localhost:1430/v1"
)

# Create atau retrieve session ID dari database chat_sessions
session_id = 1  # ID dari database

response = client.chat.completions.create(
    model="gl-claude-sonnet-4-5",
    messages=[
        {"role": "user", "content": "Hello! Remember this: my favorite color is blue"}
    ],
    chat_session_id=session_id  # ← Kunci persistence!
)

# Request berikutnya dengan session_id yang sama akan mengingat konteks
response2 = client.chat.completions.create(
    model="gl-claude-sonnet-4-5",
    messages=[
        {"role": "user", "content": "What is my favorite color?"}
    ],
    chat_session_id=session_id  # ← Session yang sama
)
# AI akan menjawab "blue" karena mengingat percakapan sebelumnya
```

### Option 2: Via HTTP Request
```bash
curl -X POST http://localhost:1430/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gl-claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ],
    "chat_session_id": 1
  }'
```

## Alur Kerja

1. **Tanpa `chat_session_id`**: 
   - Membuat `interaction_id` baru setiap request
   - Percakapan one-off (tidak persisten)

2. **Dengan `chat_session_id`**:
   - Check database untuk session tersebut
   - Jika sudah ada `gumloop_interaction_id` → gunakan yang ada
   - Jika belum ada → buat baru dan simpan ke database
   - Semua request dengan `chat_session_id` yang sama akan menggunakan `interaction_id` yang sama
   - → Percakapan persisten! ✅

## Testing

```python
# Test 1: Create new session
import aiosqlite
from unified import database as db

# Initialize database
await db.init_db()

# Create chat session
session_id = await db.create_chat_session(title="Test Session", model="gl-claude-sonnet-4-5")
print(f"Created session: {session_id}")

# Get interaction_id (will be created)
interaction_id = await db.get_or_create_gumloop_interaction_id(session_id)
print(f"Interaction ID: {interaction_id}")

# Get again (should return same ID)
interaction_id2 = await db.get_or_create_gumloop_interaction_id(session_id)
assert interaction_id == interaction_id2
print("✅ Interaction ID persists!")
```

## Files Modified

1. **`unified/database.py`**:
   - Added `gumloop_interaction_id` column to `chat_sessions` table
   - Added migration for existing databases
   - Added `get_or_create_gumloop_interaction_id()` function

2. **`unified/proxy_gumloop.py`**:
   - Added `chat_session_id` parameter support
   - Always pass `interaction_id` to Gumloop (removed conditional `if has_images`)
   - Use persistent `interaction_id` when `chat_session_id` provided

## Migration

Database akan otomatis migrasi saat startup. Tidak perlu action manual.

Existing sessions akan mendapat `gumloop_interaction_id` kosong dan akan di-generate saat pertama kali digunakan.

## Log Example

```
2026-05-10 16:45:12 [unified.proxy_gumloop] INFO: Using persistent interaction_id for chat_session_id=1: abc123def456789012
```

## Kesimpulan

Sekarang Gumloop proxy mendukung:
- ✅ **Persistent chat sessions** via `chat_session_id`
- ✅ **Backward compatible** (request tanpa `chat_session_id` tetap berfungsi)
- ✅ **Automatic interaction_id management**
- ✅ **Database-backed session storage**

Tidak ada lagi "newchat terus" - setiap session akan mempertahankan konteks percakapan! 🎉
