/**
 * Unified AI Proxy — Cloudflare Worker API Gateway
 * Central license management, account sync, usage tracking.
 */

// ─── Helpers ────────────────────────────────────────────────────────────────

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, X-Admin-Password, Authorization',
    },
  });
}

function err(message, status = 400) {
  return json({ error: message }, status);
}

function generateLicenseKey() {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  const group = () => { let s = ''; for (let i = 0; i < 5; i++) s += chars[Math.floor(Math.random() * chars.length)]; return s; };
  return `UNIF-${group()}-${group()}-${group()}-${group()}-${group()}`;
}

// ─── Version (update this when releasing) ───────────────────────────────────
// Developer workflow: bump LATEST_VERSION here + VERSION file in repo, deploy Worker, push repo.
// Users run: unifiedme update
const LATEST_VERSION = '1.0.0';
const CHANGELOG = 'Initial release: license system, central D1 database, admin panel, CLI daemon.';

function parsePagination(url) {
  const page = Math.max(1, parseInt(url.searchParams.get('page') || '1'));
  const limit = Math.min(200, Math.max(1, parseInt(url.searchParams.get('limit') || '50')));
  const offset = (page - 1) * limit;
  return { page, limit, offset };
}

// ─── Auth helpers ───────────────────────────────────────────────────────────

async function validateLicense(db, licenseKey) {
  if (!licenseKey) return null;
  const row = await db.prepare('SELECT * FROM licenses WHERE id = ? AND active = 1').bind(licenseKey).first();
  if (!row) return null;
  if (row.expires_at && new Date(row.expires_at) < new Date()) return null;
  return row;
}

async function resolveDevice(db, licenseId, fingerprint) {
  return db.prepare(
    'SELECT * FROM device_bindings WHERE license_id = ? AND device_fingerprint = ?'
  ).bind(licenseId, fingerprint).first();
}

function validateAdmin(request, env) {
  const url = new URL(request.url);
  const pw = request.headers.get('X-Admin-Password') || url.searchParams.get('password') || '';
  return pw === env.ADMIN_PASSWORD;
}

// ─── Router ─────────────────────────────────────────────────────────────────

async function handleRequest(request, env) {
  const url = new URL(request.url);
  const method = request.method;
  const path = url.pathname;

  if (method === 'OPTIONS') return json({ ok: true });

  // Health check
  if (path === '/' || path === '/health') {
    return json({ service: 'Unified AI Proxy API', version: LATEST_VERSION, status: 'ok' });
  }

  // Version check (called by CLI to check for updates)
  if (path === '/api/version') {
    return json({ version: LATEST_VERSION, changelog: CHANGELOG });
  }

  // Admin panel (HTML)
  if (path === '/admin' || path === '/admin/') {
    return new Response(ADMIN_HTML, {
      status: 200,
      headers: { 'Content-Type': 'text/html; charset=utf-8' },
    });
  }

  // Install script (served for: curl -sSL .../install | bash)
  if (path === '/install' || path === '/install/unifiedme-ai') {
    return new Response(INSTALL_SH, {
      status: 200,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  }

  const db = env.DB;

  // ── Auth endpoints ──────────────────────────────────────────────────────
  if (path === '/api/auth/activate' && method === 'POST') return authActivate(db, request);
  if (path === '/api/auth/heartbeat' && method === 'POST') return authHeartbeat(db, request);

  // ── Sync endpoints ──────────────────────────────────────────────────────
  if (path === '/api/sync/pull' && method === 'GET') return syncPull(db, url);
  if (path === '/api/sync/push' && method === 'POST') return syncPush(db, request);

  // ── Admin endpoints (require password) ──────────────────────────────────
  if (path.startsWith('/api/admin/')) {
    if (!validateAdmin(request, env)) return err('Invalid admin password', 403);

    // Licenses
    if (path === '/api/admin/licenses' && method === 'GET') return adminListLicenses(db, url);
    if (path === '/api/admin/licenses' && method === 'POST') return adminCreateLicense(db, request);
    const licMatch = path.match(/^\/api\/admin\/licenses\/([^/]+)$/);
    if (licMatch) {
      if (method === 'GET') return adminGetLicense(db, licMatch[1]);
      if (method === 'PUT') return adminUpdateLicense(db, licMatch[1], request);
      if (method === 'DELETE') return adminDeleteLicense(db, licMatch[1]);
    }

    // Stats
    if (path === '/api/admin/stats' && method === 'GET') return adminGlobalStats(db);
    const statsMatch = path.match(/^\/api\/admin\/stats\/([^/]+)$/);
    if (statsMatch && method === 'GET') return adminLicenseStats(db, statsMatch[1]);

    // Watchwords
    if (path === '/api/admin/watchwords' && method === 'GET') return adminListWatchwords(db);
    if (path === '/api/admin/watchwords' && method === 'POST') return adminCreateWatchword(db, request);
    const wwMatch = path.match(/^\/api\/admin\/watchwords\/(\d+)$/);
    if (wwMatch) {
      if (method === 'PUT') return adminUpdateWatchword(db, parseInt(wwMatch[1]), request);
      if (method === 'DELETE') return adminDeleteWatchword(db, parseInt(wwMatch[1]));
    }

    // Global Filters
    if (path === '/api/admin/global-filters' && method === 'GET') return adminListGlobalFilters(db);
    if (path === '/api/admin/global-filters' && method === 'POST') return adminCreateGlobalFilter(db, request);
    const gfMatch = path.match(/^\/api\/admin\/global-filters\/(\d+)$/);
    if (gfMatch) {
      if (method === 'PUT') return adminUpdateGlobalFilter(db, parseInt(gfMatch[1]), request);
      if (method === 'DELETE') return adminDeleteGlobalFilter(db, parseInt(gfMatch[1]));
    }

    // Alerts
    if (path === '/api/admin/alerts' && method === 'GET') return adminListAlerts(db, url);
    const ackMatch = path.match(/^\/api\/admin\/alerts\/(\d+)\/ack$/);
    if (ackMatch && method === 'POST') return adminAckAlert(db, parseInt(ackMatch[1]));

    // Logs
    if (path === '/api/admin/logs' && method === 'GET') return adminListLogs(db, url);

    // Accounts
    if (path === '/api/admin/accounts' && method === 'GET') return adminListAccounts(db, url);

    // Device management
    const devBanMatch = path.match(/^\/api\/admin\/devices\/(\d+)\/ban$/);
    if (devBanMatch && method === 'POST') return adminBanDevice(db, parseInt(devBanMatch[1]), request);
    const devUnbanMatch = path.match(/^\/api\/admin\/devices\/(\d+)\/unban$/);
    if (devUnbanMatch && method === 'POST') return adminUnbanDevice(db, parseInt(devUnbanMatch[1]));
    const devDeleteMatch = path.match(/^\/api\/admin\/devices\/(\d+)$/);
    if (devDeleteMatch && method === 'DELETE') return adminDeleteDevice(db, parseInt(devDeleteMatch[1]));
    if (path === '/api/admin/banned-devices' && method === 'GET') return adminListBannedDevices(db);
    const unbanMachineMatch = path.match(/^\/api\/admin\/banned-devices\/(\d+)$/);
    if (unbanMachineMatch && method === 'DELETE') return adminUnbanMachine(db, parseInt(unbanMachineMatch[1]));
  }

  return err('Not found', 404);
}

// ─── Auth: Activate ─────────────────────────────────────────────────────────

async function authActivate(db, request) {
  const body = await request.json();
  const { license_key, device_fingerprint, device_name } = body;
  if (!license_key || !device_fingerprint) return err('license_key and device_fingerprint required');

  // Capture client IP from Cloudflare headers
  const clientIp = request.headers.get('CF-Connecting-IP') || request.headers.get('X-Real-IP') || '';
  const deviceOs = body.os || '';
  const pcName = body.pc_name || device_name || '';
  const machineId = body.machine_id || '';

  const license = await validateLicense(db, license_key);
  if (!license) return err('Invalid or expired license', 401);

  // Check global device ban (by machine_id)
  if (machineId) {
    const banned = await db.prepare('SELECT * FROM banned_devices WHERE machine_id = ?').bind(machineId).first();
    if (banned) {
      return err('This device has been banned. Reason: ' + (banned.reason || 'Policy violation'), 403);
    }
  }

  // Check existing device
  let device = await resolveDevice(db, license.id, device_fingerprint);
  if (device) {
    await db.prepare(
      "UPDATE device_bindings SET last_seen = datetime('now'), device_name = ?, os = ?, pc_name = ?, ip_address = ?, machine_id = ? WHERE id = ?"
    ).bind(pcName || device.device_name, deviceOs || device.os, pcName || device.pc_name, clientIp || device.ip_address, machineId || device.machine_id, device.id).run();
    return json({
      ok: true,
      license: { id: license.id, tier: license.tier, max_devices: license.max_devices, max_accounts: license.max_accounts, owner_name: license.owner_name },
      device_id: device.id,
      is_new: false,
    });
  }

  // Check device count
  const countRow = await db.prepare('SELECT COUNT(*) as cnt FROM device_bindings WHERE license_id = ?').bind(license.id).first();
  if (countRow.cnt >= license.max_devices) {
    return err(`Device limit reached (${license.max_devices}). Remove a device first.`, 403);
  }

  // Bind new device
  const result = await db.prepare(
    'INSERT INTO device_bindings (license_id, device_fingerprint, device_name, os, pc_name, ip_address, machine_id) VALUES (?, ?, ?, ?, ?, ?, ?)'
  ).bind(license.id, device_fingerprint, pcName, deviceOs, pcName, clientIp, machineId).run();

  // Init global_stats row
  await db.prepare(
    'INSERT OR IGNORE INTO global_stats (license_id) VALUES (?)'
  ).bind(license.id).run();

  return json({
    ok: true,
    license: { id: license.id, tier: license.tier, max_devices: license.max_devices, max_accounts: license.max_accounts, owner_name: license.owner_name },
    device_id: result.meta.last_row_id,
    is_new: true,
  });
}

// ─── Auth: Heartbeat ────────────────────────────────────────────────────────

async function authHeartbeat(db, request) {
  const body = await request.json();
  const { license_key, device_fingerprint } = body;
  if (!license_key || !device_fingerprint) return err('license_key and device_fingerprint required');

  const license = await validateLicense(db, license_key);
  if (!license) return err('Invalid or expired license', 401);

  await db.prepare(
    "UPDATE device_bindings SET last_seen = datetime('now') WHERE license_id = ? AND device_fingerprint = ?"
  ).bind(license.id, device_fingerprint).run();

  return json({ ok: true });
}

// ─── Sync: Pull ─────────────────────────────────────────────────────────────

async function syncPull(db, url) {
  const licenseKey = url.searchParams.get('license_key');
  const fingerprint = url.searchParams.get('device_fingerprint');
  if (!licenseKey) return err('license_key required');

  const license = await validateLicense(db, licenseKey);
  if (!license) return err('Invalid or expired license', 401);

  const device = fingerprint ? await resolveDevice(db, license.id, fingerprint) : null;
  if (fingerprint) {
    await db.prepare("UPDATE device_bindings SET last_seen = datetime('now') WHERE license_id = ? AND device_fingerprint = ?")
      .bind(license.id, fingerprint).run();
  }

  // Accounts (full data for sync — includes tokens)
  const accounts = await db.prepare('SELECT * FROM accounts WHERE license_id = ?').bind(license.id).all();

  // Settings
  const settings = await db.prepare('SELECT key, value FROM settings WHERE license_id = ?').bind(license.id).all();

  // Filters
  const filters = await db.prepare('SELECT * FROM filters WHERE license_id = ? ORDER BY id ASC').bind(license.id).all();

  // Watchwords (global)
  const watchwords = await db.prepare('SELECT * FROM watchwords WHERE enabled = 1 ORDER BY id ASC').all();

  // Global filters (admin-managed, users can download)
  const globalFilters = await db.prepare('SELECT * FROM global_filters WHERE enabled = 1 ORDER BY id ASC').all();

  // Proxies (per device)
  let proxies = { results: [] };
  if (device) {
    proxies = await db.prepare('SELECT * FROM proxies WHERE license_id = ? AND device_id = ?').bind(license.id, device.id).all();
  }

  return json({
    ok: true,
    accounts: accounts.results,
    settings: Object.fromEntries((settings.results || []).map(r => [r.key, r.value])),
    filters: filters.results,
    watchwords: watchwords.results,
    global_filters: globalFilters.results,
    proxies: proxies.results,
  });
}

// ─── Sync: Push ─────────────────────────────────────────────────────────────

async function syncPush(db, request) {
  const body = await request.json();
  const { license_key, device_fingerprint, accounts, settings, usage_logs, usage_summary, alerts, proxies } = body;
  if (!license_key) return err('license_key required');

  const license = await validateLicense(db, license_key);
  if (!license) return err('Invalid or expired license', 401);

  const device = device_fingerprint ? await resolveDevice(db, license.id, device_fingerprint) : null;
  const deviceId = device ? device.id : 0;

  // Update last_seen
  if (device) {
    await db.prepare("UPDATE device_bindings SET last_seen = datetime('now') WHERE id = ?").bind(device.id).run();
  }

  let accountsUpserted = 0;
  let settingsUpserted = 0;
  let logsInserted = 0;
  let summaryInserted = 0;
  let alertsInserted = 0;
  let proxiesUpserted = 0;

  // Upsert accounts — direct overwrite (local device is always correct)
  let accountsDeleted = 0;
  if (accounts && Array.isArray(accounts)) {
    for (const acc of accounts) {
      if (!acc.email) continue;

      // Handle deleted accounts
      if (acc.status === 'deleted') {
        await db.prepare('DELETE FROM accounts WHERE license_id = ? AND email = ?')
          .bind(license.id, acc.email).run();
        accountsDeleted++;
        continue;
      }

      const existing = await db.prepare('SELECT id FROM accounts WHERE license_id = ? AND email = ?')
        .bind(license.id, acc.email).first();

      if (existing) {
        // Direct overwrite — pushing device has the correct data
        await db.prepare(`UPDATE accounts SET
          password = ?, status = ?,
          kiro_status = ?, kiro_access_token = ?, kiro_refresh_token = ?, kiro_profile_arn = ?,
          kiro_credits = ?, kiro_credits_total = ?, kiro_credits_used = ?,
          kiro_error = ?, kiro_error_count = ?, kiro_expires_at = ?,
          cb_status = ?, cb_api_key = ?, cb_credits = ?, cb_error = ?, cb_error_count = ?, cb_expires_at = ?,
          ws_status = ?, ws_api_key = ?, ws_credits = ?, ws_error = ?, ws_error_count = ?,
          gl_status = ?, gl_refresh_token = ?, gl_user_id = ?, gl_gummie_id = ?, gl_id_token = ?,
          gl_credits = ?, gl_error = ?, gl_error_count = ?,
          updated_at = datetime('now')
          WHERE id = ?`).bind(
          acc.password || '', acc.status || 'active',
          acc.kiro_status || 'pending', acc.kiro_access_token || '', acc.kiro_refresh_token || '', acc.kiro_profile_arn || '',
          acc.kiro_credits || 0, acc.kiro_credits_total || 0, acc.kiro_credits_used || 0,
          acc.kiro_error || '', acc.kiro_error_count || 0, acc.kiro_expires_at || '',
          acc.cb_status || 'pending', acc.cb_api_key || '', acc.cb_credits || 0, acc.cb_error || '', acc.cb_error_count || 0, acc.cb_expires_at || '',
          acc.ws_status || 'none', acc.ws_api_key || '', acc.ws_credits || 0, acc.ws_error || '', acc.ws_error_count || 0,
          acc.gl_status || 'none', acc.gl_refresh_token || '', acc.gl_user_id || '', acc.gl_gummie_id || '', acc.gl_id_token || '',
          acc.gl_credits || 0, acc.gl_error || '', acc.gl_error_count || 0,
          existing.id
        ).run();
      } else {
        await db.prepare(`INSERT INTO accounts (
          license_id, email, password, status,
          kiro_status, kiro_access_token, kiro_refresh_token, kiro_profile_arn,
          kiro_credits, kiro_credits_total, kiro_credits_used,
          kiro_error, kiro_error_count, kiro_expires_at,
          cb_status, cb_api_key, cb_credits, cb_error, cb_error_count, cb_expires_at,
          ws_status, ws_api_key, ws_credits, ws_error, ws_error_count,
          gl_status, gl_refresh_token, gl_user_id, gl_gummie_id, gl_id_token,
          gl_credits, gl_error, gl_error_count
        ) VALUES (?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?, ?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?)`).bind(
          license.id, acc.email, acc.password || '', acc.status || 'active',
          acc.kiro_status || 'pending', acc.kiro_access_token || '', acc.kiro_refresh_token || '', acc.kiro_profile_arn || '',
          acc.kiro_credits || 0, acc.kiro_credits_total || 0, acc.kiro_credits_used || 0,
          acc.kiro_error || '', acc.kiro_error_count || 0, acc.kiro_expires_at || '',
          acc.cb_status || 'pending', acc.cb_api_key || '', acc.cb_credits || 0, acc.cb_error || '', acc.cb_error_count || 0, acc.cb_expires_at || '',
          acc.ws_status || 'none', acc.ws_api_key || '', acc.ws_credits || 0, acc.ws_error || '', acc.ws_error_count || 0,
          acc.gl_status || 'none', acc.gl_refresh_token || '', acc.gl_user_id || '', acc.gl_gummie_id || '', acc.gl_id_token || '',
          acc.gl_credits || 0, acc.gl_error || '', acc.gl_error_count || 0
        ).run();
      }
      accountsUpserted++;
    }
  }

  // Upsert settings
  if (settings && typeof settings === 'object') {
    for (const [key, value] of Object.entries(settings)) {
      await db.prepare(
        "INSERT INTO settings (license_id, key, value) VALUES (?, ?, ?) ON CONFLICT(license_id, key) DO UPDATE SET value = excluded.value"
      ).bind(license.id, key, String(value)).run();
      settingsUpserted++;
    }
  }

  // Insert usage_logs
  if (usage_logs && Array.isArray(usage_logs)) {
    for (const log of usage_logs) {
      await db.prepare(
        'INSERT INTO usage_logs (license_id, device_id, model, tier, status_code, latency_ms, proxy_url, error_message, account_email, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)'
      ).bind(
        license.id, deviceId, log.model || '', log.tier || '', log.status_code || 200,
        log.latency_ms || 0, log.proxy_url || '', log.error_message || '',
        log.account_email || '', log.created_at || new Date().toISOString()
      ).run();
      logsInserted++;
    }
  }

  // Insert usage_summary
  if (usage_summary && Array.isArray(usage_summary)) {
    for (const s of usage_summary) {
      await db.prepare(
        'INSERT INTO usage_summary (license_id, device_id, period, model, tier, proxy_url, total_requests, success_count, error_count, total_latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?)'
      ).bind(
        license.id, deviceId, s.period || '', s.model || '', s.tier || '',
        s.proxy_url || '', s.total_requests || 0, s.success_count || 0,
        s.error_count || 0, s.total_latency_ms || 0
      ).run();
      summaryInserted++;
    }
  }

  // Insert watchword_alerts
  if (alerts && Array.isArray(alerts)) {
    for (const a of alerts) {
      await db.prepare(
        'INSERT INTO watchword_alerts (license_id, device_id, watchword_id, keyword_matched, severity, message_snippet, message_role, model, account_email, proxy_url) VALUES (?,?,?,?,?,?,?,?,?,?)'
      ).bind(
        license.id, deviceId, a.watchword_id || 0, a.keyword_matched || '',
        a.severity || 'warning', a.message_snippet || '', a.message_role || '',
        a.model || '', a.account_email || '', a.proxy_url || ''
      ).run();
      alertsInserted++;
    }
  }

  // Upsert proxies (per device)
  if (proxies && Array.isArray(proxies) && device) {
    // Delete existing proxies for this device, re-insert
    await db.prepare('DELETE FROM proxies WHERE license_id = ? AND device_id = ?').bind(license.id, device.id).run();
    for (const p of proxies) {
      await db.prepare(
        'INSERT INTO proxies (license_id, device_id, url, label, type, purpose, checked, active, last_latency_ms, last_tested, last_error) VALUES (?,?,?,?,?,?,?,?,?,?,?)'
      ).bind(
        license.id, device.id, p.url || '', p.label || '', p.type || 'http',
        p.purpose || 'api', p.checked || 0, p.active ?? 1,
        p.last_latency_ms ?? -1, p.last_tested || '', p.last_error || ''
      ).run();
      proxiesUpserted++;
    }
  }

  // Increment global_stats
  if (usage_logs && usage_logs.length > 0) {
    let totalReq = usage_logs.length;
    let totalSuccess = usage_logs.filter(l => (l.status_code || 200) < 400).length;
    let totalErrors = totalReq - totalSuccess;
    let totalTokens = usage_logs.reduce((sum, l) => sum + (l.tokens || 0), 0);

    await db.prepare(`INSERT INTO global_stats (license_id, total_requests, total_success, total_errors, total_tokens_used, updated_at)
      VALUES (?, ?, ?, ?, ?, datetime('now'))
      ON CONFLICT(license_id) DO UPDATE SET
        total_requests = total_requests + excluded.total_requests,
        total_success = total_success + excluded.total_success,
        total_errors = total_errors + excluded.total_errors,
        total_tokens_used = total_tokens_used + excluded.total_tokens_used,
        updated_at = datetime('now')
    `).bind(license.id, totalReq, totalSuccess, totalErrors, totalTokens).run();

    // Increment model_usage_stats
    const modelMap = {};
    for (const log of usage_logs) {
      const m = log.model || 'unknown';
      if (!modelMap[m]) modelMap[m] = { tier: log.tier || '', requests: 0, success: 0, errors: 0, tokens: 0, latency: 0 };
      modelMap[m].requests++;
      if ((log.status_code || 200) < 400) modelMap[m].success++;
      else modelMap[m].errors++;
      modelMap[m].tokens += log.tokens || 0;
      modelMap[m].latency += log.latency_ms || 0;
    }
    for (const [model, s] of Object.entries(modelMap)) {
      await db.prepare(`INSERT INTO model_usage_stats (license_id, model, tier, total_requests, total_success, total_errors, total_tokens, total_latency_ms, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(license_id, model) DO UPDATE SET
          total_requests = total_requests + excluded.total_requests,
          total_success = total_success + excluded.total_success,
          total_errors = total_errors + excluded.total_errors,
          total_tokens = total_tokens + excluded.total_tokens,
          total_latency_ms = total_latency_ms + excluded.total_latency_ms,
          updated_at = datetime('now')
      `).bind(license.id, model, s.tier, s.requests, s.success, s.errors, s.tokens, s.latency).run();
    }
  }

  // Update account-ever counters on global_stats
  if (accounts && accounts.length > 0) {
    const counts = await db.prepare(`SELECT
      COUNT(*) as total,
      SUM(CASE WHEN kiro_status = 'ok' OR kiro_status = 'exhausted' THEN 1 ELSE 0 END) as kiro,
      SUM(CASE WHEN cb_status = 'ok' OR cb_status = 'exhausted' THEN 1 ELSE 0 END) as cb,
      SUM(CASE WHEN ws_status = 'ok' OR ws_status = 'exhausted' THEN 1 ELSE 0 END) as ws,
      SUM(CASE WHEN gl_status = 'ok' OR gl_status = 'exhausted' THEN 1 ELSE 0 END) as gl
      FROM accounts WHERE license_id = ?`).bind(license.id).first();
    if (counts) {
      await db.prepare(`UPDATE global_stats SET
        total_accounts_added = ?, kiro_accounts_ever = CASE WHEN ? > kiro_accounts_ever THEN ? ELSE kiro_accounts_ever END,
        cb_accounts_ever = CASE WHEN ? > cb_accounts_ever THEN ? ELSE cb_accounts_ever END,
        ws_accounts_ever = CASE WHEN ? > ws_accounts_ever THEN ? ELSE ws_accounts_ever END,
        gl_accounts_ever = CASE WHEN ? > gl_accounts_ever THEN ? ELSE gl_accounts_ever END
        WHERE license_id = ?`).bind(
        counts.total, counts.kiro, counts.kiro, counts.cb, counts.cb, counts.ws, counts.ws, counts.gl, counts.gl, license.id
      ).run();
    }
  }

  return json({
    ok: true,
    accounts_upserted: accountsUpserted,
    accounts_deleted: accountsDeleted,
    settings_upserted: settingsUpserted,
    logs_inserted: logsInserted,
    summary_inserted: summaryInserted,
    alerts_inserted: alertsInserted,
    proxies_upserted: proxiesUpserted,
  });
}

// ─── Admin: Licenses ────────────────────────────────────────────────────────

async function adminListLicenses(db, url) {
  const { limit, offset } = parsePagination(url);
  const rows = await db.prepare(`
    SELECT l.*,
      (SELECT COUNT(*) FROM device_bindings d WHERE d.license_id = l.id) as device_count,
      (SELECT COUNT(*) FROM accounts a WHERE a.license_id = l.id) as account_count,
      (SELECT MAX(d2.last_seen) FROM device_bindings d2 WHERE d2.license_id = l.id) as last_device_seen,
      g.total_requests, g.total_success, g.total_errors
    FROM licenses l
    LEFT JOIN global_stats g ON g.license_id = l.id
    ORDER BY l.created_at DESC
    LIMIT ? OFFSET ?
  `).bind(limit, offset).all();

  const total = await db.prepare('SELECT COUNT(*) as cnt FROM licenses').first();
  return json({ licenses: rows.results, total: total.cnt, page: Math.floor(offset / limit) + 1, limit });
}

async function adminCreateLicense(db, request) {
  const body = await request.json();
  const id = generateLicenseKey();
  const { owner_name, owner_email, tier, max_devices, max_accounts, expires_at } = body;
  if (!owner_name || !owner_email) return err('owner_name and owner_email required');

  await db.prepare(
    'INSERT INTO licenses (id, owner_name, owner_email, tier, max_devices, max_accounts, expires_at) VALUES (?,?,?,?,?,?,?)'
  ).bind(id, owner_name, owner_email, tier || 'basic', max_devices || 2, max_accounts || 50, expires_at || null).run();

  await db.prepare('INSERT INTO global_stats (license_id) VALUES (?)').bind(id).run();

  return json({ ok: true, license_key: id, owner_name, owner_email, tier: tier || 'basic' }, 201);
}

async function adminGetLicense(db, licenseId) {
  const license = await db.prepare('SELECT * FROM licenses WHERE id = ?').bind(licenseId).first();
  if (!license) return err('License not found', 404);

  const devices = await db.prepare('SELECT id, device_fingerprint, device_name, os, pc_name, ip_address, machine_id, last_seen, created_at FROM device_bindings WHERE license_id = ? ORDER BY last_seen DESC').bind(licenseId).all();
  const stats = await db.prepare('SELECT * FROM global_stats WHERE license_id = ?').bind(licenseId).first();
  const modelStats = await db.prepare('SELECT * FROM model_usage_stats WHERE license_id = ? ORDER BY total_requests DESC').bind(licenseId).all();

  // Account counts per provider
  const accCounts = await db.prepare(`SELECT
    COUNT(*) as total,
    SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active,
    SUM(CASE WHEN kiro_status = 'ok' THEN 1 ELSE 0 END) as kiro_ok,
    SUM(CASE WHEN kiro_status = 'failed' THEN 1 ELSE 0 END) as kiro_failed,
    SUM(CASE WHEN kiro_status IN ('none','pending') THEN 1 ELSE 0 END) as kiro_none,
    SUM(CASE WHEN cb_status = 'ok' THEN 1 ELSE 0 END) as cb_ok,
    SUM(CASE WHEN cb_status = 'failed' THEN 1 ELSE 0 END) as cb_failed,
    SUM(CASE WHEN cb_status IN ('none','pending') THEN 1 ELSE 0 END) as cb_none,
    SUM(CASE WHEN ws_status = 'ok' THEN 1 ELSE 0 END) as ws_ok,
    SUM(CASE WHEN ws_status = 'failed' THEN 1 ELSE 0 END) as ws_failed,
    SUM(CASE WHEN ws_status IN ('none','pending') THEN 1 ELSE 0 END) as ws_none,
    SUM(CASE WHEN gl_status = 'ok' THEN 1 ELSE 0 END) as gl_ok,
    SUM(CASE WHEN gl_status = 'failed' THEN 1 ELSE 0 END) as gl_failed,
    SUM(CASE WHEN gl_status IN ('none','pending') THEN 1 ELSE 0 END) as gl_none
    FROM accounts WHERE license_id = ?`).bind(licenseId).first();

  return json({
    license,
    devices: devices.results,
    stats: stats || {},
    model_stats: modelStats.results,
    account_counts: accCounts || {},
  });
}

async function adminUpdateLicense(db, licenseId, request) {
  const body = await request.json();
  const fields = [];
  const vals = [];
  for (const key of ['owner_name', 'owner_email', 'tier', 'max_devices', 'max_accounts', 'expires_at', 'active']) {
    if (body[key] !== undefined) {
      fields.push(`${key} = ?`);
      vals.push(body[key]);
    }
  }
  if (fields.length === 0) return err('No fields to update');
  vals.push(licenseId);
  await db.prepare(`UPDATE licenses SET ${fields.join(', ')} WHERE id = ?`).bind(...vals).run();
  return json({ ok: true });
}

async function adminDeleteLicense(db, licenseId) {
  await db.prepare('UPDATE licenses SET active = 0 WHERE id = ?').bind(licenseId).run();
  return json({ ok: true });
}

// ─── Admin: Global Stats ────────────────────────────────────────────────────

async function adminGlobalStats(db) {
  const totals = await db.prepare(`SELECT
    SUM(total_requests) as total_requests,
    SUM(total_success) as total_success,
    SUM(total_errors) as total_errors,
    SUM(total_tokens_used) as total_tokens_used,
    SUM(total_accounts_added) as total_accounts_added
    FROM global_stats`).first();

  const licenseCount = await db.prepare("SELECT COUNT(*) as cnt FROM licenses WHERE active = 1").first();
  const deviceCount = await db.prepare("SELECT COUNT(*) as cnt FROM device_bindings").first();
  const activeDevices = await db.prepare("SELECT COUNT(*) as cnt FROM device_bindings WHERE last_seen > datetime('now', '-1 hour')").first();

  // Top models globally
  const topModels = await db.prepare(`SELECT model, tier,
    SUM(total_requests) as requests, SUM(total_success) as success, SUM(total_errors) as errors
    FROM model_usage_stats GROUP BY model ORDER BY requests DESC LIMIT 20`).all();

  // Top licenses by usage
  const topLicenses = await db.prepare(`SELECT g.*, l.owner_name, l.owner_email, l.tier,
    (SELECT COUNT(*) FROM accounts a WHERE a.license_id = g.license_id) as account_count
    FROM global_stats g JOIN licenses l ON l.id = g.license_id
    ORDER BY g.total_requests DESC LIMIT 20`).all();

  return json({
    totals: totals || {},
    active_licenses: licenseCount?.cnt || 0,
    total_devices: deviceCount?.cnt || 0,
    active_devices: activeDevices?.cnt || 0,
    top_models: topModels.results,
    top_licenses: topLicenses.results,
  });
}

async function adminLicenseStats(db, licenseId) {
  return adminGetLicense(db, licenseId);
}

// ─── Admin: Watchwords ──────────────────────────────────────────────────────

async function adminListWatchwords(db) {
  const rows = await db.prepare('SELECT * FROM watchwords ORDER BY id ASC').all();
  return json({ watchwords: rows.results });
}

async function adminCreateWatchword(db, request) {
  const body = await request.json();
  if (!body.keyword) return err('keyword required');
  const result = await db.prepare(
    'INSERT INTO watchwords (keyword, is_regex, severity, enabled, description) VALUES (?,?,?,?,?)'
  ).bind(body.keyword, body.is_regex ? 1 : 0, body.severity || 'warning', 1, body.description || '').run();
  return json({ ok: true, id: result.meta.last_row_id }, 201);
}

async function adminUpdateWatchword(db, id, request) {
  const body = await request.json();
  const fields = [];
  const vals = [];
  for (const key of ['keyword', 'is_regex', 'severity', 'enabled', 'description']) {
    if (body[key] !== undefined) {
      fields.push(`${key} = ?`);
      vals.push(key === 'is_regex' || key === 'enabled' ? (body[key] ? 1 : 0) : body[key]);
    }
  }
  if (fields.length === 0) return err('No fields to update');
  vals.push(id);
  await db.prepare(`UPDATE watchwords SET ${fields.join(', ')} WHERE id = ?`).bind(...vals).run();
  return json({ ok: true });
}

async function adminDeleteWatchword(db, id) {
  await db.prepare('DELETE FROM watchwords WHERE id = ?').bind(id).run();
  return json({ ok: true });
}

// ─── Admin: Global Filters ──────────────────────────────────────────────────

async function adminListGlobalFilters(db) {
  const rows = await db.prepare('SELECT * FROM global_filters ORDER BY id ASC').all();
  return json({ global_filters: rows.results });
}

async function adminCreateGlobalFilter(db, request) {
  const body = await request.json();
  if (!body.find_text) return err('find_text required');
  const result = await db.prepare(
    'INSERT INTO global_filters (find_text, replace_text, is_regex, enabled, description) VALUES (?,?,?,?,?)'
  ).bind(body.find_text, body.replace_text || '', body.is_regex ? 1 : 0, 1, body.description || '').run();
  return json({ ok: true, id: result.meta.last_row_id }, 201);
}

async function adminUpdateGlobalFilter(db, id, request) {
  const body = await request.json();
  const fields = [];
  const vals = [];
  for (const key of ['find_text', 'replace_text', 'is_regex', 'enabled', 'description']) {
    if (body[key] !== undefined) {
      fields.push(`${key} = ?`);
      vals.push(key === 'is_regex' || key === 'enabled' ? (body[key] ? 1 : 0) : body[key]);
    }
  }
  if (fields.length === 0) return err('No fields to update');
  vals.push(id);
  await db.prepare(`UPDATE global_filters SET ${fields.join(', ')} WHERE id = ?`).bind(...vals).run();
  return json({ ok: true });
}

async function adminDeleteGlobalFilter(db, id) {
  await db.prepare('DELETE FROM global_filters WHERE id = ?').bind(id).run();
  return json({ ok: true });
}

// ─── Admin: Alerts ──────────────────────────────────────────────────────────

async function adminListAlerts(db, url) {
  const { limit, offset } = parsePagination(url);
  const licenseId = url.searchParams.get('license_id');

  let query, countQuery;
  if (licenseId) {
    query = db.prepare(`SELECT a.*, l.owner_name FROM watchword_alerts a
      LEFT JOIN licenses l ON l.id = a.license_id
      WHERE a.license_id = ? ORDER BY a.created_at DESC LIMIT ? OFFSET ?`).bind(licenseId, limit, offset);
    countQuery = db.prepare('SELECT COUNT(*) as cnt FROM watchword_alerts WHERE license_id = ?').bind(licenseId);
  } else {
    query = db.prepare(`SELECT a.*, l.owner_name FROM watchword_alerts a
      LEFT JOIN licenses l ON l.id = a.license_id
      ORDER BY a.created_at DESC LIMIT ? OFFSET ?`).bind(limit, offset);
    countQuery = db.prepare('SELECT COUNT(*) as cnt FROM watchword_alerts');
  }

  const rows = await query.all();
  const total = await countQuery.first();
  return json({ alerts: rows.results, total: total.cnt, page: Math.floor(offset / limit) + 1, limit });
}

async function adminAckAlert(db, id) {
  await db.prepare('UPDATE watchword_alerts SET acknowledged = 1 WHERE id = ?').bind(id).run();
  return json({ ok: true });
}

// ─── Admin: Logs ────────────────────────────────────────────────────────────

async function adminListLogs(db, url) {
  const { limit, offset } = parsePagination(url);
  const licenseId = url.searchParams.get('license_id');

  let query, countQuery;
  if (licenseId) {
    query = db.prepare(`SELECT u.*, l.owner_name FROM usage_logs u
      LEFT JOIN licenses l ON l.id = u.license_id
      WHERE u.license_id = ? ORDER BY u.created_at DESC LIMIT ? OFFSET ?`).bind(licenseId, limit, offset);
    countQuery = db.prepare('SELECT COUNT(*) as cnt FROM usage_logs WHERE license_id = ?').bind(licenseId);
  } else {
    query = db.prepare(`SELECT u.*, l.owner_name FROM usage_logs u
      LEFT JOIN licenses l ON l.id = u.license_id
      ORDER BY u.created_at DESC LIMIT ? OFFSET ?`).bind(limit, offset);
    countQuery = db.prepare('SELECT COUNT(*) as cnt FROM usage_logs');
  }

  const rows = await query.all();
  const total = await countQuery.first();
  return json({ logs: rows.results, total: total.cnt, page: Math.floor(offset / limit) + 1, limit });
}

// ─── Admin: Accounts ────────────────────────────────────────────────────────

async function adminListAccounts(db, url) {
  const { limit, offset } = parsePagination(url);
  const licenseId = url.searchParams.get('license_id');

  // Return accounts WITHOUT sensitive tokens
  const selectCols = `a.id, a.license_id, a.email, a.status,
    a.kiro_status, a.kiro_credits, a.kiro_credits_total, a.kiro_credits_used, a.kiro_error, a.kiro_error_count, a.kiro_expires_at,
    a.cb_status, a.cb_credits, a.cb_error, a.cb_error_count, a.cb_expires_at,
    a.ws_status, a.ws_credits, a.ws_error, a.ws_error_count,
    a.gl_status, a.gl_credits, a.gl_error, a.gl_error_count,
    a.created_at, a.updated_at,
    l.owner_name`;

  let query, countQuery;
  if (licenseId) {
    query = db.prepare(`SELECT ${selectCols} FROM accounts a LEFT JOIN licenses l ON l.id = a.license_id WHERE a.license_id = ? ORDER BY a.created_at DESC LIMIT ? OFFSET ?`).bind(licenseId, limit, offset);
    countQuery = db.prepare('SELECT COUNT(*) as cnt FROM accounts WHERE license_id = ?').bind(licenseId);
  } else {
    query = db.prepare(`SELECT ${selectCols} FROM accounts a LEFT JOIN licenses l ON l.id = a.license_id ORDER BY a.created_at DESC LIMIT ? OFFSET ?`).bind(limit, offset);
    countQuery = db.prepare('SELECT COUNT(*) as cnt FROM accounts');
  }

  const rows = await query.all();
  const total = await countQuery.first();
  return json({ accounts: rows.results, total: total.cnt, page: Math.floor(offset / limit) + 1, limit });
}

// ─── Admin: Device Management ───────────────────────────────────────────────

async function adminBanDevice(db, deviceId, request) {
  const device = await db.prepare('SELECT * FROM device_bindings WHERE id = ?').bind(deviceId).first();
  if (!device) return err('Device not found', 404);

  const body = await request.json().catch(() => ({}));
  const reason = body.reason || 'Banned by admin';
  const machineId = device.machine_id || device.device_fingerprint;

  if (!machineId) return err('Device has no machine ID — cannot ban globally', 400);

  // Add to global ban list (by machine_id)
  await db.prepare(
    'INSERT OR REPLACE INTO banned_devices (machine_id, reason, banned_by) VALUES (?, ?, ?)'
  ).bind(machineId, reason, 'admin').run();

  return json({ ok: true, machine_id: machineId, reason });
}

async function adminUnbanDevice(db, deviceId) {
  const device = await db.prepare('SELECT * FROM device_bindings WHERE id = ?').bind(deviceId).first();
  if (!device) return err('Device not found', 404);

  const machineId = device.machine_id || device.device_fingerprint;
  await db.prepare('DELETE FROM banned_devices WHERE machine_id = ?').bind(machineId).run();
  return json({ ok: true });
}

async function adminDeleteDevice(db, deviceId) {
  // Delete device + its proxies
  await db.prepare('DELETE FROM proxies WHERE device_id = ?').bind(deviceId).run();
  await db.prepare('DELETE FROM device_bindings WHERE id = ?').bind(deviceId).run();
  return json({ ok: true });
}

async function adminListBannedDevices(db) {
  const rows = await db.prepare('SELECT * FROM banned_devices ORDER BY created_at DESC').all();
  return json({ banned_devices: rows.results });
}

async function adminUnbanMachine(db, banId) {
  await db.prepare('DELETE FROM banned_devices WHERE id = ?').bind(banId).run();
  return json({ ok: true });
}

// ─── Cron: Daily Purge ──────────────────────────────────────────────────────

async function handleCron(env) {
  const db = env.DB;

  // Purge usage_logs > 7 days
  const logs = await db.prepare("DELETE FROM usage_logs WHERE created_at < datetime('now', '-7 days')").run();

  // Purge usage_summary > 30 days
  const summary = await db.prepare("DELETE FROM usage_summary WHERE created_at < datetime('now', '-30 days')").run();

  // Purge acknowledged alerts > 30 days
  const alerts = await db.prepare("DELETE FROM watchword_alerts WHERE acknowledged = 1 AND created_at < datetime('now', '-30 days')").run();

  // Purge proxies from inactive devices (last_seen > 7 days)
  const proxies = await db.prepare(`DELETE FROM proxies WHERE device_id IN (
    SELECT id FROM device_bindings WHERE last_seen < datetime('now', '-7 days')
  )`).run();

  console.log(`Purge complete: logs=${logs.meta.changes}, summary=${summary.meta.changes}, alerts=${alerts.meta.changes}, proxies=${proxies.meta.changes}`);
}

// ─── Export ─────────────────────────────────────────────────────────────────

export default {
  async fetch(request, env, ctx) {
    try {
      return await handleRequest(request, env);
    } catch (e) {
      console.error('Worker error:', e);
      return err(`Internal error: ${e.message}`, 500);
    }
  },
  async scheduled(event, env, ctx) {
    await handleCron(env);
  },
};
