-- Unified AI Proxy — Cloudflare D1 Schema
-- Central database for license management, account sync, usage tracking

-- 1. Licenses
CREATE TABLE IF NOT EXISTS licenses (
    id TEXT PRIMARY KEY,
    owner_name TEXT NOT NULL,
    owner_email TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'basic',
    max_devices INTEGER NOT NULL DEFAULT 2,
    max_accounts INTEGER NOT NULL DEFAULT 50,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

-- 2. Device bindings
CREATE TABLE IF NOT EXISTS device_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id TEXT NOT NULL REFERENCES licenses(id),
    device_fingerprint TEXT NOT NULL,
    device_name TEXT NOT NULL DEFAULT '',
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(license_id, device_fingerprint)
);

-- 3. Accounts (per license, all 4 providers)
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id TEXT NOT NULL,
    email TEXT NOT NULL,
    password TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',

    kiro_status TEXT NOT NULL DEFAULT 'pending',
    kiro_access_token TEXT NOT NULL DEFAULT '',
    kiro_refresh_token TEXT NOT NULL DEFAULT '',
    kiro_profile_arn TEXT NOT NULL DEFAULT '',
    kiro_credits REAL NOT NULL DEFAULT 0,
    kiro_credits_total REAL NOT NULL DEFAULT 0,
    kiro_credits_used REAL NOT NULL DEFAULT 0,
    kiro_error TEXT NOT NULL DEFAULT '',
    kiro_error_count INTEGER NOT NULL DEFAULT 0,
    kiro_expires_at TEXT NOT NULL DEFAULT '',

    cb_status TEXT NOT NULL DEFAULT 'pending',
    cb_api_key TEXT NOT NULL DEFAULT '',
    cb_credits REAL NOT NULL DEFAULT 0,
    cb_error TEXT NOT NULL DEFAULT '',
    cb_error_count INTEGER NOT NULL DEFAULT 0,
    cb_expires_at TEXT NOT NULL DEFAULT '',

    ws_status TEXT NOT NULL DEFAULT 'none',
    ws_api_key TEXT NOT NULL DEFAULT '',
    ws_credits REAL NOT NULL DEFAULT 0,
    ws_error TEXT NOT NULL DEFAULT '',
    ws_error_count INTEGER NOT NULL DEFAULT 0,

    gl_status TEXT NOT NULL DEFAULT 'none',
    gl_refresh_token TEXT NOT NULL DEFAULT '',
    gl_user_id TEXT NOT NULL DEFAULT '',
    gl_gummie_id TEXT NOT NULL DEFAULT '',
    gl_id_token TEXT NOT NULL DEFAULT '',
    gl_credits REAL NOT NULL DEFAULT 0,
    gl_error TEXT NOT NULL DEFAULT '',
    gl_error_count INTEGER NOT NULL DEFAULT 0,

    cbai_status TEXT NOT NULL DEFAULT 'none',
    cbai_api_key TEXT NOT NULL DEFAULT '',
    cbai_session_token TEXT NOT NULL DEFAULT '',
    cbai_credits REAL NOT NULL DEFAULT 0,
    cbai_error TEXT NOT NULL DEFAULT '',
    cbai_error_count INTEGER NOT NULL DEFAULT 0,

    skboss_status TEXT NOT NULL DEFAULT 'none',
    skboss_api_key TEXT NOT NULL DEFAULT '',
    skboss_credits REAL NOT NULL DEFAULT 0,
    skboss_error TEXT NOT NULL DEFAULT '',
    skboss_error_count INTEGER NOT NULL DEFAULT 0,

    windsurf_status TEXT NOT NULL DEFAULT 'none',
    windsurf_api_key TEXT NOT NULL DEFAULT '',
    windsurf_credits REAL NOT NULL DEFAULT 0,
    windsurf_error TEXT NOT NULL DEFAULT '',
    windsurf_error_count INTEGER NOT NULL DEFAULT 0,

    tr_status TEXT NOT NULL DEFAULT 'none',
    tr_api_key TEXT NOT NULL DEFAULT '',
    tr_credits REAL NOT NULL DEFAULT 0,
    tr_error TEXT NOT NULL DEFAULT '',
    tr_error_count INTEGER NOT NULL DEFAULT 0,

    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(license_id, email)
);

-- 4. API keys (per device)
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id TEXT NOT NULL,
    device_id INTEGER REFERENCES device_bindings(id),
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT 'default',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used TEXT NOT NULL DEFAULT '',
    usage_count INTEGER NOT NULL DEFAULT 0
);

-- 5. Settings (per license key-value)
CREATE TABLE IF NOT EXISTS settings (
    license_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (license_id, key)
);

-- 6. Proxies (per device, auto-purge on inactive)
CREATE TABLE IF NOT EXISTS proxies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id TEXT NOT NULL,
    device_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'http',
    purpose TEXT NOT NULL DEFAULT 'api',
    checked INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    last_latency_ms INTEGER NOT NULL DEFAULT -1,
    last_tested TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 7. Filters (per license, find/replace rules)
CREATE TABLE IF NOT EXISTS filters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id TEXT NOT NULL,
    find_text TEXT NOT NULL,
    replace_text TEXT NOT NULL DEFAULT '',
    is_regex INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    description TEXT NOT NULL DEFAULT '',
    hit_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 8. Watchwords (global, managed by super admin)
CREATE TABLE IF NOT EXISTS watchwords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    is_regex INTEGER NOT NULL DEFAULT 0,
    severity TEXT NOT NULL DEFAULT 'warning',
    enabled INTEGER NOT NULL DEFAULT 1,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 9. Watchword alerts (per device, pushed by proxy)
CREATE TABLE IF NOT EXISTS watchword_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id TEXT NOT NULL,
    device_id INTEGER NOT NULL,
    watchword_id INTEGER NOT NULL,
    keyword_matched TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warning',
    message_snippet TEXT NOT NULL DEFAULT '',
    message_role TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    account_email TEXT NOT NULL DEFAULT '',
    proxy_url TEXT NOT NULL DEFAULT '',
    acknowledged INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 10. Global stats (per license, lifetime counters — NEVER purged)
CREATE TABLE IF NOT EXISTS global_stats (
    license_id TEXT PRIMARY KEY,
    total_requests INTEGER NOT NULL DEFAULT 0,
    total_success INTEGER NOT NULL DEFAULT 0,
    total_errors INTEGER NOT NULL DEFAULT 0,
    total_tokens_used INTEGER NOT NULL DEFAULT 0,
    total_accounts_added INTEGER NOT NULL DEFAULT 0,
    kiro_accounts_ever INTEGER NOT NULL DEFAULT 0,
    cb_accounts_ever INTEGER NOT NULL DEFAULT 0,
    ws_accounts_ever INTEGER NOT NULL DEFAULT 0,
    gl_accounts_ever INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 11. Model usage stats (per license per model, lifetime — NEVER purged)
CREATE TABLE IF NOT EXISTS model_usage_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id TEXT NOT NULL,
    model TEXT NOT NULL,
    tier TEXT NOT NULL,
    total_requests INTEGER NOT NULL DEFAULT 0,
    total_success INTEGER NOT NULL DEFAULT 0,
    total_errors INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_latency_ms INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(license_id, model)
);

-- 12. Usage summary (aggregated per 5-min bucket — purge after 30 days)
CREATE TABLE IF NOT EXISTS usage_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id TEXT NOT NULL,
    device_id INTEGER NOT NULL,
    period TEXT NOT NULL,
    model TEXT NOT NULL,
    tier TEXT NOT NULL,
    proxy_url TEXT NOT NULL DEFAULT '',
    total_requests INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    total_latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 13. Usage logs (per-request metadata — purge after 7 days)
CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id TEXT NOT NULL,
    device_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    tier TEXT NOT NULL,
    status_code INTEGER NOT NULL DEFAULT 200,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    proxy_url TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    account_email TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 14. Used emails (global duplicate prevention — never deleted)
CREATE TABLE IF NOT EXISTS used_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    provider TEXT NOT NULL,
    license_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(email, provider)
);

CREATE INDEX IF NOT EXISTS idx_used_emails_email ON used_emails(email);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_device_license ON device_bindings(license_id);
CREATE INDEX IF NOT EXISTS idx_device_lastseen ON device_bindings(last_seen);
CREATE INDEX IF NOT EXISTS idx_accounts_license ON accounts(license_id);
CREATE INDEX IF NOT EXISTS idx_accounts_license_status ON accounts(license_id, status);
CREATE INDEX IF NOT EXISTS idx_apikeys_license ON api_keys(license_id);
CREATE INDEX IF NOT EXISTS idx_apikeys_device ON api_keys(device_id);
CREATE INDEX IF NOT EXISTS idx_proxies_device ON proxies(license_id, device_id);
CREATE INDEX IF NOT EXISTS idx_filters_license ON filters(license_id);
CREATE INDEX IF NOT EXISTS idx_alerts_license ON watchword_alerts(license_id, created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON watchword_alerts(severity, acknowledged);
CREATE INDEX IF NOT EXISTS idx_model_stats_license ON model_usage_stats(license_id);
CREATE INDEX IF NOT EXISTS idx_usage_summary_period ON usage_summary(license_id, period);
CREATE INDEX IF NOT EXISTS idx_usage_logs_license ON usage_logs(license_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_logs_created ON usage_logs(created_at);
