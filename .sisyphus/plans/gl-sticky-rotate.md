# Gumloop Sticky Rotate with Temporary Exhaustion

## Problem
- `rate_limited` and `exhausted` GL accounts are marked permanently dead
- No auto-recovery mechanism — admin must manually reset
- Accounts that hit rate limits should recover after cooldown

## Changes

### 1. database.py — Schema + Recovery
- Add `gl_exhausted_until TEXT DEFAULT ''` column (ISO timestamp)
- Modify `get_next_account_for_tier` for `max_gl` tier:
  - Before querying, auto-recover accounts where `gl_exhausted_until < now` → reset to `ok`
  - Skip accounts where `gl_exhausted_until > now` (still cooling down)
- Add helper: `mark_gl_exhausted_temporary(account_id, cooldown_seconds)` — sets `gl_status='exhausted'` + `gl_exhausted_until`

### 2. router_proxy.py — Gumloop retry block
- 429 → `mark_gl_exhausted_temporary(id, 120)` (2 min cooldown) instead of permanent `rate_limited`
- Credit exhausted → `mark_gl_exhausted_temporary(id, 3600)` (1 hour cooldown)
- 401/403 banned → stays permanent (no recovery)
- 500+ server error → use existing `mark_account_error` (3 strikes = failed)
- BackgroundTask credit detection → also uses temporary exhaustion

### 3. proxy_gumloop.py — Stream error handling
- Credit error in stream → use temporary exhaustion instead of permanent

### 4. account_manager.py — No changes needed
- `mark_account_error` and `mark_account_success` already work correctly
