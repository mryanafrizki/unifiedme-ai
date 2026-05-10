# 🚀 Quick Start Guide

## Setup (First Time Only)

### Windows
```bash
setup.bat
```

### Linux/Mac
```bash
chmod +x setup.sh
./setup.sh
```

## Configure

Edit `config.json`:
```json
{
  "email": "your-email@example.com",      # Ganti dengan email Anda
  "password": "your-password",            # Ganti dengan password Anda
  "quiz_answers": [
    "Answer 1",  # Jawaban untuk pertanyaan 1
    "Answer 2",  # Jawaban untuk pertanyaan 2
    "Answer 3",  # Jawaban untuk pertanyaan 3
    "Answer 4",  # Jawaban untuk pertanyaan 4
    "Answer 5",  # Jawaban untuk pertanyaan 5
    "Answer 6"   # Jawaban untuk pertanyaan 6
  ]
}
```

## Run

### Test Setup First
```bash
# Activate venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate.bat # Windows

# Test
python quick_test.py
```

### Run Full Automation
```bash
python run_automation.py
```

## What Happens

1. ✅ Browser opens (Chromium)
2. ✅ Creates/logs into Gumloop account
3. ✅ Adds MCP server
4. ✅ Navigates to University
5. ✅ Clicks "Allow" on OAuth
6. ✅ Completes quiz with your answers
7. ✅ Screenshots saved to `screenshots/`
8. ✅ Logs saved to `gumloop_university_automation.log`

## Tips

- **See the browser**: Set `"headless": false` in config.json
- **Already have account?**: Set `"skip_account_creation": true`
- **Check logs**: `cat gumloop_university_automation.log`
- **Check screenshots**: `ls screenshots/`

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Browser doesn't open | Run `playwright install chromium` |
| Wrong quiz answers | Set `"headless": false` and watch browser |
| OAuth page not found | Check screenshots folder |
| MCP server failed | Verify `mcp_server_config` in config.json |

## Support

Check `README.md` for detailed documentation.
