# 🔄 Batch Gumloop Automation - New Version

Automated batch processing for Gumloop accounts with university completion.

---

## 📋 Quick Comparison

| Feature | **NEW** (batch_gumloop_university.py) | OLD (gumloop_login.py) |
|---------|--------------------------------------|------------------------|
| **Script** | `batch_gumloop_university.py` | `gumloop_login.py` |
| **Boarding Fix** | ✅ Yes (handles new accounts) | ❌ No (gets stuck) |
| **University** | ✅ Auto-completes (15k credits) | ❌ No |
| **MCP Setup** | ✅ Creates + attaches | ❌ Manual |
| **Status** | 🟢 **PRODUCTION** | 🟡 **BACKUP** |

---

## 🚀 Usage - NEW Version

### Single Account
```bash
python batch_gumloop_university.py \
  --email user@example.com \
  --password secret123 \
  --mcp-url http://localhost:1430
```

### Batch Mode (Multiple Accounts)
```bash
# 1. Create accounts.txt:
cat > accounts.txt << EOF
user1@example.com:password1
user2@example.com:password2
user3@example.com:password3
EOF

# 2. Run batch:
python batch_gumloop_university.py \
  --batch accounts.txt \
  --mcp-url http://localhost:1430
```

### With Custom Answers
```bash
python batch_gumloop_university.py \
  --batch accounts.txt \
  --mcp-url http://localhost:1430 \
  --answers "2,3,3,2,2,2,2,2,2,2,2,2,2"
```

---

## 📦 Output

### Single Account
```
production/result.json              ← Credentials
```

### Batch Mode
```
batch_results.json                  ← Summary of all accounts
batch_results/
├── user1_at_example_com_result.json
├── user2_at_example_com_result.json
└── user3_at_example_com_result.json
```

---

## 🔄 Migration from Old Script

### Before (OLD - gumloop_login.py)
```bash
# Just login, no university
python gumloop_login.py user@example.com password123
```
**Result:** Login only, no MCP, no university

### After (NEW - batch_gumloop_university.py)
```bash
# Login + MCP + University
python batch_gumloop_university.py \
  --email user@example.com \
  --password password123 \
  --mcp-url http://localhost:1430
```
**Result:** Login ✅ + MCP attached ✅ + 15k credits ✅

---

## 🆚 When to Use Which Script

### Use **NEW** (batch_gumloop_university.py) when:
- ✅ Need university completion (15,000 credits)
- ✅ Need MCP server setup
- ✅ Working with new accounts (handles /boarding)
- ✅ Want full automation

### Use **OLD** (gumloop_login.py) when:
- 🔧 Only need simple login
- 🔧 Don't need university credits
- 🔧 Don't need MCP setup
- 🔧 Legacy compatibility

**Recommendation:** Use **NEW** script for all production! 🎯

---

## 📊 What NEW Script Does

```
1. Launch Browser (Camoufox anti-detect)
   ↓
2. Google OAuth Login
   ↓
3. Handle /boarding page (NEW!) ✅
   ↓
4. Extract Firebase tokens
   ↓
5. Create Gummie via API
   ↓
6. Create MCP credential
   ↓
7. Attach MCP to Gummie
   ↓
8. Navigate to University
   ↓
9. Complete Getting Started (6 lessons) → 10k credits
   ↓
10. Complete AI Fundamentals (7 lessons) → 5k credits
    ↓
11. Save credentials to result.json
    ↓
✅ DONE! (Total: ~3-5 minutes per account)
```

---

## 🐛 Troubleshooting

### Error: "Could not import university automation module"
```bash
# Fix: Ensure production folder exists
cd backup_gumloop_cli
ls production/gumloop_university.py

# If missing, copy:
cp mcp_custom/intercept_gumloop_university.py production/gumloop_university.py
```

### Stuck at /boarding
✅ **FIXED in new script!** Auto-detects and redirects to /home

### Batch file not found
```bash
# Check file path
cat accounts.txt

# Format should be:
# email1:password1
# email2:password2
```

---

## 📁 File Structure

```
backup_gumloop_cli/
│
├── batch_gumloop_university.py  ← NEW batch script ✅
├── gumloop_login.py             ← OLD batch script (backup)
│
├── production/
│   └── gumloop_university.py    ← Core automation (used by NEW)
│
├── archive/
│   └── old_scripts/
│       └── gumloop_login_original.py  ← OLD backup
│
├── batch_results/               ← Output folder (batch mode)
│   └── *.json
│
└── BATCH_MIGRATION.md           ← This file
```

---

## 🔐 Security

- **accounts.txt** - Add to `.gitignore`
- **batch_results/** - Contains credentials, secure storage!
- **result.json** - Single account output, also sensitive

---

## ✨ Benefits of NEW Script

1. ✅ **Handles /boarding** - No more stuck new accounts
2. ✅ **15,000 credits** - Auto university completion
3. ✅ **MCP ready** - Gummie with server attached
4. ✅ **Production tested** - Based on fixed script
5. ✅ **Batch support** - Process multiple accounts
6. ✅ **Clear logging** - See progress for each account

---

## 📝 Example Output

### Single Account
```
[20:30:15] Starting automation for user@example.com
[20:30:20] ✅ Redirected to boarding page (new account)
[20:30:21] 🆕 New account detected - redirecting to /home
[20:30:25] ✅ On /home page
[20:30:26] ✅ Got tokens!
[20:30:30] Gummie created: ABC123...
[20:30:35] MCP attached OK
[20:30:40] PHASE 2: Gumloop University
[20:33:25] ✅ SUCCESS for user@example.com
[20:33:25]    User ID: XYZ789...
[20:33:25]    Gummie ID: ABC123...
[20:33:25]    MCP: Random-MCP-1234
```

### Batch Mode
```
[20:30:00] 📋 Found 3 accounts to process

============================================================
ACCOUNT 1/3: user1@example.com
============================================================
... (automation output) ...
[20:33:20] ✅ SUCCESS for user1@example.com

============================================================
ACCOUNT 2/3: user2@example.com
============================================================
... (automation output) ...
[20:36:40] ✅ SUCCESS for user2@example.com

============================================================
ACCOUNT 3/3: user3@example.com
============================================================
... (automation output) ...
[20:40:00] ✅ SUCCESS for user3@example.com

============================================================
BATCH COMPLETE
============================================================
Total: 3 accounts
✅ Success: 3
❌ Failed: 0

Results saved to: batch_results.json
Individual results in: batch_results/
```

---

## 🎯 Next Steps

1. **Test single account:**
   ```bash
   python batch_gumloop_university.py \
     --email test@example.com \
     --password test123 \
     --mcp-url http://localhost:1430
   ```

2. **Test batch mode:**
   ```bash
   echo "test1@example.com:pass1" > test_accounts.txt
   echo "test2@example.com:pass2" >> test_accounts.txt
   
   python batch_gumloop_university.py \
     --batch test_accounts.txt \
     --mcp-url http://localhost:1430
   ```

3. **Deploy to production:**
   - Replace old batch calls with new script
   - Update cron jobs / schedulers
   - Monitor first few runs

---

**Status:** 🟢 **PRODUCTION READY**  
**Version:** 2.0 (New Batch System)  
**Replaces:** gumloop_login.py (now backup only)
