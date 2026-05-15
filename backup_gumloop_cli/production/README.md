# 🎓 Gumloop University Automation - Production

Automated completion of Gumloop University courses + MCP server setup.

---

## 📁 Quick Start

```bash
cd production

# Run university automation
python gumloop_university.py \
  --email your@email.com \
  --password yourpassword \
  --mcp-url http://localhost:1430
```

**Output:** `result.json` with credentials

---

## 📋 What It Does

1. ✅ Login via Google OAuth (Camoufox anti-detect browser)
2. ✅ Create gummie agent
3. ✅ Create MCP credential
4. ✅ Attach MCP server to gummie
5. ✅ Complete **Getting Started** course (6 lessons) → 10,000 credits
6. ✅ Complete **AI Fundamentals** course (7 lessons) → 5,000 credits
7. ✅ Total: **15,000 credits** + Gummie with MCP attached

---

## 🚀 Usage

### Basic (Auto-detect answers)
```bash
python gumloop_university.py \
  --email user@example.com \
  --password secret123 \
  --mcp-url http://localhost:1430
```

### Custom Answers
```bash
python gumloop_university.py \
  --email user@example.com \
  --password secret123 \
  --mcp-url http://localhost:1430 \
  --answers "2,3,3,2,2,2,2,2,2,2,2,2,2"
#          ↑_________↑ ↑___________↑
#          Getting Started  AI Fundamentals
#          (6 answers)      (7 answers)
```

---

## 📦 Dependencies

```bash
pip install httpx camoufox[geoip]
```

Optional (if Camoufox fails):
```bash
pip install playwright
playwright install chromium
```

---

## 📊 Output

### `result.json`
```json
{
  "email": "user@example.com",
  "user_id": "ABC123...",
  "gummie_id": "XYZ789...",
  "mcp_secret_id": "secret_...",
  "mcp_name": "Random-MCP-1234",
  "mcp_url": "http://localhost:1430",
  "id_token": "eyJhbGci...",
  "refresh_token": "AMf-v..."
}
```

**Security:** Add `result.json` to `.gitignore`!

---

## ⚙️ Production Settings

**File:** `config.py`

```python
PRODUCTION_MODE = True
SAVE_SCREENSHOTS = False  # No screenshots
SAVE_REQUEST_LOG = False  # No request logs
SAVE_RESULT_JSON = True   # Only credentials
```

**Behavior:**
- ✅ No `screenshots/` directory
- ✅ No `intercept_university_log.json`
- ✅ Only `result.json` output
- ✅ Console logging only

---

## 🔧 Customization

### Change Courses

**Edit** `gumloop_university.py` line 49:

```python
COURSE_PLAN = [
    # Comment out to skip:
    # {
    #     "name": "getting-started-with-gumloop",
    #     ...
    # },
    {
        "name": "ai-fundamentals",
        "start_path": "/ai-fundamentals/what-is-an-ai-model",
        "lesson_paths": [...],
        "expected_reward_credits": 5000,
    },
]
```

### Custom Default Answers

**Edit** line 80:

```python
COURSE_DEFAULT_ANSWERS = {
    "getting-started-with-gumloop": [2, 3, 3, 2, 2, 2],
    "ai-fundamentals": [2, 2, 2, 2, 2, 2, 2],
}
```

---

## 🐛 Troubleshooting

### Stuck at /boarding page
**Fixed!** Script auto-detects new accounts and redirects to `/home`.

### Token extraction fails
- Check internet connection
- Verify email/password correct
- Try running again (browser state may need refresh)

### MCP attachment fails
- Verify MCP server is running at specified URL
- Check firewall/network settings
- Ensure URL is accessible from browser

---

## 🔐 Security Notes

- `result.json` contains sensitive tokens → Add to `.gitignore`
- Tokens expire after ~1 hour (auto-refreshed by Firebase)
- Use HTTPS for production MCP servers
- Local development: `http://localhost` only

---

## 📞 Support

**Logs:** Check console output for detailed progress  
**Errors:** Script logs each step with ✅/❌ indicators  
**Debug:** Set `VERBOSE_LOGGING = True` in `config.py`

---

## ✨ Features

### Anti-Detection
- ✅ Camoufox browser (anti-fingerprinting)
- ✅ BrowserForge for realistic fingerprints
- ✅ Human-like typing (60-70ms delays)
- ✅ Natural pauses (1-3s between actions)

### Robust Flow
- ✅ Account chooser auto-click
- ✅ Consent screen multi-strategy detection
- ✅ `/boarding` page handling for new accounts
- ✅ Retry logic with exponential backoff
- ✅ Error handling at every step

### Answer Detection
1. Auto-detect from HTML (primary)
2. Fallback to default answers
3. Fallback to CLI `--answers`
4. Fallback to manual input prompt

---

## 📂 Related Files

- `gumloop_university.py` - Main script (production ready)
- `config.py` - Configuration settings
- `requirements.txt` - Python dependencies
- `../archive/old_scripts/` - Previous versions (backup)
- `../docs/` - Extended documentation

---

**Version:** 2.0 (Production)  
**Last Updated:** 2026-05-15  
**Status:** ✅ Production Ready
