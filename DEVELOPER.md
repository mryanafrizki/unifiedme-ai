# Developer Guide — Unified AI Proxy

How to update code, deploy new versions, and manage the central infrastructure.

## Project Structure

```
fix-unpath/                 Main project root
  unified/                  Python proxy (FastAPI)
  worker/                   Cloudflare Worker (D1 API + Admin Panel)
    src/index.js            Worker API source
    src/admin.html          Super Admin Panel HTML
    build.js                Build script (embeds HTML + install.sh into Worker)
    wrangler.json           Cloudflare deployment config
    schema.sql              D1 database schema
  app/                      Browser automation (Camoufox + Playwright)
  VERSION                   Current version (single source of truth)
  install.sh                Auto-installer for users
  README.md                 User documentation
  DEVELOPER.md              This file
```

## Releasing a New Version

### Step 1: Make your code changes

Edit files in `unified/`, `worker/`, `app/`, etc.

### Step 2: Bump version

```bash
# Edit VERSION file
echo "1.1.0" > VERSION
```

### Step 3: Update Worker version constant

Edit `worker/src/index.js`, find these lines near the top:

```javascript
const LATEST_VERSION = '1.1.0';  // <-- bump this
const CHANGELOG = 'Fix: account rotation, error logging improvements';  // <-- update
```

### Step 4: Deploy Worker

```bash
cd worker
npm install          # first time only
node build.js        # embeds admin.html + install.sh
npx wrangler deploy  # deploys to Cloudflare
```

You need these env vars (or wrangler login):
```bash
export CLOUDFLARE_API_TOKEN="your-token"
export CLOUDFLARE_ACCOUNT_ID="your-account-id"
```

### Step 5: Commit and push

```bash
cd ..  # back to project root
git add -A
git commit -m "v1.1.0: description of changes"
git push origin main
```

### Step 6: Done

Users will see:
```
$ unifiedme status
  Version:   1.0.0
  Update:    1.1.0 available! Run: unifiedme update

$ unifiedme update
  Current version: 1.0.0
  Pulling latest code...
  Updated: v1.0.0 -> v1.1.0
  Update complete!
```

## Infrastructure

### Cloudflare D1 Database

- **Name**: `unified-proxy`
- **ID**: `dd9e909d-7673-447a-91dc-4a54d1cbca6d`
- **Region**: APAC (Singapore)
- **Tables**: 14 (licenses, accounts, device_bindings, api_keys, settings, proxies, filters, watchwords, watchword_alerts, global_stats, model_usage_stats, usage_summary, usage_logs, banned_devices)

### Cloudflare Worker

- **Name**: `unified-api`
- **URL**: `https://unified-api.roubot71.workers.dev`
- **Cron**: Daily 3 AM UTC (purge old logs)

### D1 Schema Changes

If you need to add columns:

```bash
cd worker
npx wrangler d1 execute unified-proxy --remote --command="ALTER TABLE accounts ADD COLUMN new_field TEXT DEFAULT '';"
```

Also update `schema.sql` for fresh installs.

### Direct D1 Queries

```bash
# List all licenses
npx wrangler d1 execute unified-proxy --remote --command="SELECT id, owner_name, tier FROM licenses;"

# Check account count per license
npx wrangler d1 execute unified-proxy --remote --command="SELECT license_id, COUNT(*) as cnt FROM accounts GROUP BY license_id;"

# Check banned devices
npx wrangler d1 execute unified-proxy --remote --command="SELECT * FROM banned_devices;"
```

## Admin Panel

- **URL**: `https://unified-api.roubot71.workers.dev/admin`
- **Password**: Set in `wrangler.json` → `vars.ADMIN_PASSWORD`

### Updating Admin Panel

1. Edit `worker/src/admin.html`
2. Run `node build.js` (embeds into Worker)
3. Run `npx wrangler deploy`

## User Dashboard

- **URL**: `http://localhost:1430/dashboard` (on user's device)
- **File**: `unified/dashboard.html`

Changes to dashboard.html are picked up by users on `unifiedme update`.

## Key Files

| File | What it does | When to edit |
|------|-------------|--------------|
| `VERSION` | Version number | Every release |
| `worker/src/index.js` | Central API (all endpoints) | API changes, version bump |
| `worker/src/admin.html` | Super Admin Panel | Admin UI changes |
| `unified/dashboard.html` | User Dashboard | User UI changes |
| `unified/router_proxy.py` | Request routing + retry logic | Provider changes |
| `unified/license_client.py` | D1 sync client | Sync logic changes |
| `unified/cli.py` | CLI commands | New commands |
| `unified/config.py` | Model routing table | New models |
| `install.sh` | Auto-installer | Dependency changes |
| `requirements.txt` | Python dependencies | New packages |

## Creating a License

### Via Admin Panel
1. Go to `https://unified-api.roubot71.workers.dev/admin`
2. Login with admin password
3. Licenses tab → "+ New License"

### Via API
```bash
curl -X POST https://unified-api.roubot71.workers.dev/api/admin/licenses \
  -H "Content-Type: application/json" \
  -H "X-Admin-Password: your-password" \
  -d '{"owner_name":"Name","owner_email":"email@test.com","tier":"pro","max_devices":5,"max_accounts":200}'
```

### Via CLI (D1 direct)
```bash
npx wrangler d1 execute unified-proxy --remote \
  --command="INSERT INTO licenses (id, owner_name, owner_email, tier, max_devices, max_accounts) VALUES ('UNIF-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX', 'Name', 'email', 'pro', 5, 200);"
```

## Banning a Device

### Via Admin Panel
1. Licenses tab → click license → Devices table → "Ban" button

### Via API
```bash
curl -X POST https://unified-api.roubot71.workers.dev/api/admin/devices/DEVICE_ID/ban \
  -H "Content-Type: application/json" \
  -H "X-Admin-Password: your-password" \
  -d '{"reason":"Policy violation"}'
```

Ban is **global by machine_id** — device cannot use ANY license.

## Troubleshooting

### User can't connect
1. Check license is active: Admin Panel → Licenses
2. Check device count vs max_devices
3. Check if device is banned: Admin Panel → license detail → devices

### Accounts not syncing
1. Check proxy logs: `unified/data/proxy.log`
2. Check D1 connectivity: `curl https://unified-api.roubot71.workers.dev/health`
3. Force sync: restart proxy (`unifiedme stop && unifiedme start`)

### All accounts exhausted
1. Check Admin Panel → Logs tab for error details
2. Add new accounts via batch login
3. Or manually restore: Dashboard → account card → "Use" button (force-set as active)
