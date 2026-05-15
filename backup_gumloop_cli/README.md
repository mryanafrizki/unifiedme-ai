# 🎓 Gumloop Automation Scripts

Automated tools for Gumloop account management and university course completion.

---

## 📁 Directory Structure

```
backup_gumloop_cli/
│
├── 📂 production/              ← USE THIS FOR PRODUCTION
│   ├── gumloop_university.py  # Main automation script
│   ├── config.py              # Production settings
│   ├── requirements.txt       # Dependencies
│   └── README.md              # Usage guide
│
├── 📂 archive/                # Backup files
│   ├── old_scripts/           # Previous script versions
│   └── logs/                  # Old log files
│
├── 📂 docs/                   # Documentation
│   └── CHANGELOG.md           # Version history
│
├── 📂 mcp_custom/             # Development scripts
│   ├── intercept_gumloop_university.py  # Source (with logs)
│   └── gumloop_university_automation.py
│
└── README.md                  # This file
```

---

## 🚀 Quick Start

### Production Use (Recommended)

```bash
cd production

pip install -r requirements.txt

python gumloop_university.py \
  --email your@email.com \
  --password yourpassword \
  --mcp-url http://localhost:1430
```

**Output:** `result.json` (credentials only, no logs)

---

## 📋 What It Does

1. ✅ Login to Gumloop via Google OAuth
2. ✅ Create gummie agent
3. ✅ Create & attach MCP server
4. ✅ Complete **Getting Started** (6 lessons) → 10,000 credits
5. ✅ Complete **AI Fundamentals** (7 lessons) → 5,000 credits
6. ✅ **Total: 15,000 credits + MCP-ready gummie**

**Time:** ~3-5 minutes per account

---

## 🔧 Configuration

### Production Mode (Default)
- ✅ No screenshots
- ✅ No request logs
- ✅ Only `result.json` output
- ✅ Console logging only

### Development Mode
Use scripts in `mcp_custom/` for debugging:
- Screenshots saved to `screenshots/`
- HTTP logs saved to `intercept_university_log.json`
- Verbose logging

---

## 📦 Requirements

```bash
pip install httpx camoufox[geoip]
```

**Python:** 3.10+  
**OS:** Windows, macOS, Linux

---

## 🐛 Troubleshooting

### Common Issues

**1. Stuck at /boarding page**
   - ✅ Fixed in v2.0 (auto-redirect to /home)

**2. Token extraction fails**
   - Check internet connection
   - Verify email/password correct
   - Script retries 8 times automatically

**3. MCP attachment fails**
   - Verify MCP server is running
   - Check URL is accessible
   - Use `http://localhost` for local dev

### Debug Mode

For detailed logs, use development scripts:
```bash
cd mcp_custom
python intercept_gumloop_university.py --email ... --password ... --mcp-url ...
```

This generates:
- `screenshots/` - Visual debugging
- `intercept_university_log.json` - HTTP request logs
- Verbose console output

---

## 📖 Documentation

- **Production Guide:** `production/README.md`
- **Changelog:** `docs/CHANGELOG.md`
- **Config Reference:** `production/config.py`

---

## 🔐 Security

- **Credentials:** `result.json` contains sensitive tokens
- **Gitignore:** Already configured in `production/.gitignore`
- **Token Expiry:** Tokens expire after ~1 hour
- **HTTPS:** Use HTTPS URLs for production MCP servers

---

## 🎯 Use Cases

### Batch Account Setup
```bash
for email in user1@example.com user2@example.com; do
  python production/gumloop_university.py \
    --email $email \
    --password secret123 \
    --mcp-url http://localhost:1430
done
```

### Single Account + Custom Answers
```bash
python production/gumloop_university.py \
  --email user@example.com \
  --password secret \
  --mcp-url http://localhost:1430 \
  --answers "2,3,3,2,2,2,2,2,2,2,2,2,2"
```

### Skip Getting Started Course
Edit `production/gumloop_university.py` line 49, comment out first course.

---

## ✨ Features

### Anti-Detection
- Camoufox browser (anti-fingerprinting)
- BrowserForge realistic fingerprints
- Human-like typing delays (60-70ms)
- Natural pauses between actions

### Smart Automation
- Account chooser auto-click
- Consent screen detection
- New account `/boarding` handling
- Auto-detect quiz answers from HTML
- Fallback to defaults/CLI/manual input

### Robust Error Handling
- 3 login retries
- 8 token extraction retries
- Graceful degradation
- Clear error messages

---

## 📞 Support

**Questions?** Check `production/README.md`  
**Bugs?** Enable debug mode in `mcp_custom/`  
**Updates?** See `docs/CHANGELOG.md`

---

## 📄 License

MIT License - Use freely for personal/commercial projects

---

**Version:** 2.0 Production  
**Last Updated:** 2026-05-15  
**Status:** ✅ Production Ready
