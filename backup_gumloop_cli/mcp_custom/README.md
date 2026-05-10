# Gumloop University Automation

Script automation untuk menjalankan flow lengkap Gumloop:
1. ✅ Create account / Login
2. ✅ Add MCP server
3. ✅ Navigate ke Gumloop University
4. ✅ Handle OAuth authorization
5. ✅ Complete quiz dengan 6 jawaban

## Features

- 🎯 Full automation dari account creation sampai quiz completion
- 📸 Auto screenshot di setiap step untuk debugging
- 📝 Detailed logging ke file dan console
- 🔍 Request/response interception untuk monitoring
- ⚙️ Configurable via JSON file
- 🖥️ Headless atau GUI mode

## Requirements

```bash
pip install playwright
playwright install chromium
```

## Setup

1. Copy config example:
```bash
cp config_example.json config.json
```

2. Edit `config.json` dengan data Anda:
```json
{
  "email": "your-email@example.com",
  "password": "your-password",
  "mcp_server_config": {
    "name": "My MCP Server",
    "endpoint": "http://localhost:3000",
    "description": "My custom MCP server"
  },
  "quiz_answers": [
    "Answer 1",
    "Answer 2",
    "Answer 3",
    "Answer 4",
    "Answer 5",
    "Answer 6"
  ],
  "headless": false,
  "skip_account_creation": false
}
```

## Usage

### Method 1: Via run script (recommended)
```bash
python run_automation.py
```

Atau dengan custom config:
```bash
python run_automation.py my_config.json
```

### Method 2: Direct import
```python
import asyncio
from gumloop_university_automation import GumloopUniversityAutomation

async def main():
    automation = GumloopUniversityAutomation(
        email="user@example.com",
        password="password",
        mcp_server_config={
            "name": "Test Server",
            "endpoint": "http://localhost:3000"
        },
        quiz_answers=["A1", "A2", "A3", "A4", "A5", "A6"],
        headless=False
    )
    
    await automation.run_full_flow(skip_account_creation=False)

asyncio.run(main())
```

## Configuration Options

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `email` | string | ✅ | Email untuk Gumloop account |
| `password` | string | ✅ | Password untuk account |
| `mcp_server_config` | object | ✅ | Config MCP server (name, endpoint, description) |
| `quiz_answers` | array | ✅ | Array of 6 answers untuk quiz |
| `headless` | boolean | ❌ | Run browser tanpa GUI (default: false) |
| `skip_account_creation` | boolean | ❌ | Skip create account, langsung login (default: false) |

## Quiz Answers

Script ini membutuhkan **6 jawaban** untuk quiz Gumloop University. 

Untuk mendapatkan jawaban:
1. Jalankan script dengan mode non-headless (`"headless": false`)
2. Lihat pertanyaan di browser
3. Update `quiz_answers` di config.json
4. Re-run script

Atau, jalankan manual sekali untuk capture pertanyaan, lalu:
```python
# Lihat log file untuk detail pertanyaan
cat gumloop_university_automation.log
```

## Outputs

### Screenshots
Semua screenshot disimpan di folder `screenshots/` dengan naming:
- `01_signup_page.png` - Halaman signup
- `02_signup_filled.png` - Form signup terisi
- `11_university_landing.png` - Halaman University
- `12_oauth_authorize.png` - OAuth authorization page
- `15_question_1.png` - Quiz question 1
- dll.

### Logs
Log detail disimpan di `gumloop_university_automation.log`:
- Request/response intercepted
- Screenshot timestamps
- Step completion status
- Errors and warnings

## Flow Diagram

```
┌─────────────────────────┐
│  Create Account/Login   │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│   Add MCP Server        │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  Navigate to University │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  OAuth Authorization    │
│  (Click "Allow")        │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  Complete Quiz          │
│  (6 questions)          │
└─────────────────────────┘
```

## Intercepted Data

Script ini intercept semua request/response yang mengandung:
- `api`
- `oauth`
- `authorize`
- `login`
- `mcp`

Check `gumloop_university_automation.log` untuk detail lengkap.

## Troubleshooting

### Browser tidak terbuka
- Pastikan Playwright sudah install: `playwright install chromium`
- Coba mode non-headless: set `"headless": false`

### OAuth page tidak muncul
- Check screenshot `11_university_landing.png`
- Manual inspect URL redirect di log file
- Pastikan account sudah terverifikasi

### Quiz answers tidak terdeteksi
- Run dengan `"headless": false` untuk visual debugging
- Check screenshots `15_question_*.png`
- Update selector di script jika UI berubah

### MCP server add gagal
- Verify `mcp_server_config` sudah benar
- Check Gumloop UI manual untuk field yang required
- Lihat screenshot `07_mcp_settings.png` dan `08_mcp_add_dialog.png`

## Advanced Usage

### Custom screenshot directory
```python
automation = GumloopUniversityAutomation(
    ...,
    screenshot_dir="my_screenshots"
)
```

### Run specific steps only
```python
await automation.setup_browser()
await automation.login()
await automation.add_mcp_server()
# Skip university flow
```

### Extend for more automation
Subclass `GumloopUniversityAutomation` dan override methods:
```python
class MyAutomation(GumloopUniversityAutomation):
    async def custom_step(self):
        # Your custom automation
        pass
        
    async def run_full_flow(self, **kwargs):
        await super().run_full_flow(**kwargs)
        await self.custom_step()
```

## Notes

- Browser tetap open setelah completion jika non-headless mode (untuk inspection)
- Press Ctrl+C untuk close browser
- Screenshots saved dengan timestamp untuk avoid overwrite
- All state logged untuk debugging

## License

Internal use only - OhMyOpenCode
