# Gumloop Batch Login - Complete Flow Documentation

## 🎯 TL;DR

Gumloop **TIDAK menggunakan signup form native**. Flow signup adalah:
- ✅ Google OAuth via popup/redirect
- ✅ Firebase Auth (extract tokens dari IndexedDB)
- ✅ Browser automation dengan Camoufox
- ❌ BUKAN POST ke `/api/signup` dengan email/password

---

## 📁 File Structure

```
unified/
├── batch_runner.py         # Main orchestrator untuk batch login
│   └── _run_gumloop_login()   # Entry point Gumloop login
│
├── gumloop/
│   └── auth.py             # GumloopAuth class untuk token refresh
│
gumloop_login.py            # Browser automation script (subprocess)
```

---

## 🔄 DUAL PATH SYSTEM

### Path A: Fast Token Refresh (Existing Accounts)
**When**: Account sudah punya `gl_refresh_token` di database

```python
# unified/batch_runner.py -> _run_gumloop_login()

account = await db.get_account(job.account_id)
if account and account.get("gl_refresh_token"):
    # ✅ FAST PATH - No browser needed!
    auth = GumloopAuth(
        refresh_token=refresh_token,
        user_id=user_id,
        proxy_url=proxy_url_used or None,
    )
    token = await auth.get_token()  # Calls Firebase securetoken API
    updated = auth.get_updated_tokens()
    
    # Update database dengan tokens baru
    await _store_gumloop_tokens(job, updated, proxy_url_used)
```

**Firebase Refresh Endpoint**:
```
POST https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}
Body: {
  "grant_type": "refresh_token",
  "refresh_token": "..."
}

Response: {
  "id_token": "...",
  "refresh_token": "...",
  "user_id": "...",
  "expires_in": 3600
}
```

---

### Path B: Browser Signup (New Accounts / Fallback)
**When**: 
- Account belum punya `gl_refresh_token`, ATAU
- Token refresh gagal

```python
# unified/batch_runner.py -> _run_gumloop_login()

# Spawn subprocess
proc = await asyncio.create_subprocess_exec(
    PYTHON_BIN, str(GUMLOOP_SCRIPT),
    "--email", job.email,
    "--password", job.password,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)

# Stream output untuk progress tracking
# Output format: JSON lines dengan type: "progress" | "debug" | "result"
```

---

## 🌐 Browser Automation Flow

**Script**: `gumloop_login.py`  
**Browser**: Camoufox (anti-detection browser)  
**Function**: `async def run_login(email: str, password: str)`

### Step-by-Step Breakdown

```python
# Step 1: Navigate to Gumloop homepage
await page.goto("https://www.gumloop.com/home")

# Step 2: Click "Get Started" → "Continue with Google"
async def click_google_login(page):
    # 1. Click Get Started button
    await page.click("text=Get Started")
    
    # 2. Wait for modal
    await page.wait_for_selector("text=Continue with Google")
    
    # 3. Click Google OAuth button
    await page.click("text=Continue with Google")
    
    # Returns: popup_page or uses main page

# Step 3: Fill Google Email
async def fill_google_email(page, email):
    await page.wait_for_selector("input[type=email]")
    await page.fill("input[type=email]", email)
    await page.click("button:has-text('Next')")

# Step 4: Fill Google Password  
async def fill_google_password(page, password):
    await page.wait_for_selector("input[type=password]")
    await page.fill("input[type=password]", password)
    await page.click("button:has-text('Next')")

# Step 5: Handle consent screens & speedbumps
async def handle_consent_and_redirect(google_page, main_page):
    # Auto-clicks:
    # - "Continue" / "Allow" buttons
    # - "I understand" pada workspace terms
    # Waits untuk redirect ke gumloop.com

# Step 6: Extract Firebase Tokens
async def extract_firebase_tokens(page):
    # JavaScript execution di browser:
    result = await page.evaluate("""() => {
        return new Promise((resolve) => {
            const request = indexedDB.open('firebaseLocalStorageDb');
            request.onsuccess = (event) => {
                const db = event.target.result;
                const tx = db.transaction('firebaseLocalStorage', 'readonly');
                const store = tx.objectStore('firebaseLocalStorage');
                const getAll = store.getAll();
                
                getAll.onsuccess = () => {
                    for (const item of getAll.result) {
                        const val = item.value || item;
                        const stm = val.stsTokenManager || {};
                        
                        resolve({
                            uid: val.uid,
                            email: val.email,
                            id_token: stm.accessToken,
                            refresh_token: stm.refreshToken
                        });
                    }
                };
            };
        });
    }""")

# Step 7: Extract Gummie ID (optional)
async def extract_gummie_id(page):
    await page.goto("https://www.gumloop.com/gummies")
    # Read dari localStorage untuk get gummie_id

# Step 8: Return hasil
return {
    "success": True,
    "id_token": "...",
    "refresh_token": "...",
    "user_id": "...",
    "email": "...",
    "gummie_id": "..."
}
```

---

## 💾 Token Storage & Persistence

```python
# unified/batch_runner.py -> _store_gumloop_tokens()

await db.update_account(
    account_id,
    gl_status="ok",
    gl_id_token=id_token,
    gl_refresh_token=refresh_token,
    gl_user_id=user_id,
    gl_gummie_id=gummie_id,  # Optional
    gl_error=""
)
```

**Database Fields**:
- `gl_status`: "ok" | "failed" | "pending"
- `gl_id_token`: Firebase access token (valid ~1 hour)
- `gl_refresh_token`: Refresh token (long-lived)
- `gl_user_id`: Firebase UID
- `gl_gummie_id`: Gumloop specific agent ID
- `gl_error`: Error message if login failed

---

## 🔑 Key Components

### 1. GumloopAuth Class (`unified/gumloop/auth.py`)

```python
class GumloopAuth:
    def __init__(
        self,
        refresh_token: str,
        user_id: str,
        id_token: str = "",
        proxy_url: str | None = None,
    ):
        self.refresh_token = refresh_token
        self.user_id = user_id
        self.id_token = id_token
        self.expires_at = 0  # Auto-refresh when expired
        
    async def refresh(self):
        """POST to Firebase securetoken endpoint"""
        # Updates: id_token, refresh_token, expires_at
        
    async def get_token(self):
        """Thread-safe token getter with auto-refresh"""
        # Returns valid id_token, refreshes jika expired
        
    def get_updated_tokens(self):
        """Returns dict untuk database update"""
        return {
            "gl_id_token": self.id_token,
            "gl_refresh_token": self.refresh_token,
            "gl_user_id": self.user_id,
        }
```

**Firebase Constants**:
```python
FIREBASE_API_KEY = "AIzaSyCYuXqbJ0YBNltoGS4-7Y6Hozrra8KKmaE"
FIREBASE_REFRESH_URL = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"
```

---

## 🔄 Complete Flow Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│  unified/batch_runner.py -> _run_gumloop_login(job, proxy)      │
└────────────────────────┬─────────────────────────────────────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │ Get account from DB  │
              └──────────┬───────────┘
                         │
         ┌───────────────┴───────────────┐
         │                               │
         ▼ YES                           ▼ NO
┌─────────────────────┐         ┌───────────────────┐
│ Has refresh_token?  │         │  Browser Signup   │
└──────────┬──────────┘         └─────────┬─────────┘
           │                               │
           ▼                               ▼
  ┌────────────────┐           ┌──────────────────────┐
  │ PATH A: FAST   │           │ PATH B: AUTOMATION   │
  │ Token Refresh  │           │  gumloop_login.py    │
  └────────┬───────┘           └──────────┬───────────┘
           │                               │
           │                               ▼
           │                   ┌─────────────────────────┐
           │                   │ 1. Navigate /home       │
           │                   │ 2. Click Google OAuth   │
           │                   │ 3. Fill email           │
           │                   │ 4. Fill password        │
           │                   │ 5. Handle consent       │
           │                   │ 6. Extract from IndexDB │
           │                   │ 7. Get gummie_id        │
           │                   └─────────┬───────────────┘
           │                             │
           └──────────┬──────────────────┘
                      │
                      ▼
        ┌──────────────────────────┐
        │ _store_gumloop_tokens()  │
        │ Update DB with new tokens│
        └──────────────────────────┘
                      │
                      ▼
              ┌──────────────┐
              │ Return result│
              └──────────────┘
```

---

## 🎭 Why This Design?

### Google OAuth Only
- Gumloop menggunakan **Firebase Authentication**
- Support provider: **Google OAuth saja**
- TIDAK ada email/password signup native

### IndexedDB Token Storage
- Firebase Auth menyimpan tokens di **browser IndexedDB**
- Path: `firebaseLocalStorageDb` → `firebaseLocalStorage`
- Script extract via `page.evaluate()` (JavaScript execution)

### Dual Path untuk Performance
- **Fast refresh**: Existing accounts → API call saja (< 1 second)
- **Browser automation**: New accounts → Full OAuth flow (30-60 seconds)
- Setelah first login → semua subsequent logins gunakan fast path

---

## 🚨 Common Pitfalls

### ❌ WRONG Assumptions

```python
# ❌ SALAH: Gumloop punya signup API endpoint
await httpx.post("https://api.gumloop.com/signup", {
    "email": email,
    "password": password
})

# ❌ SALAH: Fill form signup di gumloop.com/signup
await page.goto("https://gumloop.com/signup")
await page.fill("input[name=email]", email)
```

### ✅ CORRECT Approach

```python
# ✅ BENAR: Check refresh_token first
if account.gl_refresh_token:
    # Fast path
    auth = GumloopAuth(refresh_token=...)
    token = await auth.get_token()
else:
    # Browser automation
    subprocess: gumloop_login.py → Google OAuth → IndexedDB

# ✅ BENAR: Extract dari IndexedDB
tokens = await page.evaluate("""
    indexedDB.open('firebaseLocalStorageDb')...
""")
```

---

## 📊 Performance Metrics

| Scenario | Method | Time | Browser Required |
|----------|--------|------|------------------|
| First login | Browser automation | 30-60s | ✅ Yes (Camoufox) |
| Subsequent login | Token refresh API | < 1s | ❌ No |
| Token expired | Auto-refresh | < 1s | ❌ No |
| Refresh failed | Fallback to browser | 30-60s | ✅ Yes |

---

## 🔧 Debugging Tips

### Check Token Status
```python
account = await db.get_account(account_id)
print(f"gl_refresh_token: {account.get('gl_refresh_token')[:20]}...")
print(f"gl_status: {account.get('gl_status')}")
```

### Enable Browser Debugging
```python
# Set headless=False in batch_runner.py
headless = batch_state.headless  # Controlled by dashboard toggle
```

### Monitor SSE Events
```python
# batch_state.broadcast() sends real-time progress
{
    "type": "job_log",
    "job_id": "abc123",
    "provider": "gumloop",
    "step": "google_click",
    "message": "Clicking Google sign-in..."
}
```

---

## 📚 Related Files

- `unified/batch_runner.py` - Main batch login orchestrator
- `gumloop_login.py` - Browser automation subprocess
- `unified/gumloop/auth.py` - GumloopAuth class
- `batch_dashboard.py` - FastAPI dashboard untuk monitoring
- `unified/database.py` - SQLite database operations

---

## 🎓 Summary

**Gumloop batch login workflow**:

1. **Check database**: Ada refresh_token?
2. **Fast path** → Refresh via Firebase API
3. **Slow path** → Browser automation → Google OAuth → Extract IndexedDB
4. **Store tokens** → Database update
5. **Future logins** → Always fast path (unless refresh fails)

**Key Technologies**:
- Firebase Authentication
- Google OAuth 2.0
- Camoufox (stealth browser)
- Playwright (browser automation)
- IndexedDB extraction
- SSE (Server-Sent Events) untuk progress tracking

---

**Created**: 2026-05-10  
**Purpose**: Documentation untuk Gumloop batch login flow  
**Audience**: Developers working on UnifiedMe-AI batch automation
