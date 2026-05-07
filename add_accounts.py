#!/usr/bin/env python3
"""
CLI tool for batch adding accounts to UnifiedMe proxy.

Calls the running proxy server API — proxy must be running first.

Usage:
    python add_accounts.py

Logs:
    addaccounts.log         — full process log
    addaccounts_failed.log  — failed accounts (email:password + reason)
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

# ─── Config ──────────────────────────────────────────────────────────────────

BASE_URL = os.getenv("PROXY_URL", "http://127.0.0.1:1430")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "kUcingku0")
LOG_FILE = Path("addaccounts.log")
FAIL_FILE = Path("addaccounts_failed.log")

# ─── Helpers ─────────────────────────────────────────────────────────────────

def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, also_print: bool = True):
    line = f"[{ts()}] {msg}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if also_print:
        print(line)


def log_fail(email: str, password: str, reason: str):
    with open(FAIL_FILE, "a", encoding="utf-8") as f:
        f.write(f"{email}:{password} | {reason}\n")


def api(method: str, path: str, json_body=None, timeout: float = 30) -> dict:
    """Call proxy admin API."""
    headers = {"X-Admin-Password": ADMIN_PASSWORD, "Content-Type": "application/json"}
    url = f"{BASE_URL}/api{path}"
    with httpx.Client(timeout=timeout) as client:
        if method == "GET":
            resp = client.get(url, headers=headers)
        else:
            resp = client.post(url, headers=headers, json=json_body or {})
        if resp.status_code != 200:
            raise RuntimeError(f"API {method} {path} → HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()


def check_proxy_alive():
    """Check if proxy server is running."""
    try:
        api("GET", "/accounts")
        return True
    except Exception:
        return False


def prompt_choice(question: str, options: list[str], default: str = "") -> str:
    """Prompt user to pick from options."""
    print(f"\n  {question}")
    for i, opt in enumerate(options, 1):
        marker = " *" if opt == default else ""
        print(f"    {i}. {opt}{marker}")
    while True:
        raw = input(f"  > Choose [1-{len(options)}]: ").strip()
        if not raw and default:
            return default
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        print("    Invalid choice, try again.")


def prompt_yn(question: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"  {question} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def prompt_input(question: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    raw = input(f"  {question}{hint}: ").strip()
    return raw or default


# ─── Proxy Loading ───────────────────────────────────────────────────────────

def load_proxies(filepath: str) -> list[str]:
    """Load proxies from .txt file. One per line."""
    path = Path(filepath)
    if not path.exists():
        print(f"  [ERROR] File not found: {filepath}")
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    proxies = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    return proxies


def check_proxies(proxies: list[str]) -> list[dict]:
    """Quick connectivity check on proxies."""
    results = []
    for p in proxies:
        try:
            with httpx.Client(proxy=p, timeout=10) as client:
                resp = client.get("https://httpbin.org/ip")
                if resp.status_code == 200:
                    ip = resp.json().get("origin", "?")
                    results.append({"url": p, "status": "ok", "ip": ip})
                else:
                    results.append({"url": p, "status": "fail", "ip": ""})
        except Exception as e:
            results.append({"url": p, "status": "fail", "ip": str(e)[:50]})
    return results


# ─── Account Loading ─────────────────────────────────────────────────────────

def load_accounts(filepath: str) -> list[tuple[str, str]]:
    """Load email:password pairs from .txt file."""
    path = Path(filepath)
    if not path.exists():
        print(f"  [ERROR] File not found: {filepath}")
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    accounts = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        parts = line.split(":", 1)
        accounts.append((parts[0].strip(), parts[1].strip()))
    return accounts


# ─── SSE Progress Listener ──────────────────────────────────────────────────

def stream_progress(account_map: dict[str, str]):
    """Listen to SSE events and print real-time progress.

    account_map: {email: password} for logging failures.
    """
    url = f"{BASE_URL}/api/events?token={ADMIN_PASSWORD}"
    done_count = 0
    total = len(account_map)

    try:
        with httpx.Client(timeout=None) as client:
            with client.stream("GET", url) as resp:
                for line in resp.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    etype = data.get("type", "")

                    if etype == "batch_start":
                        log(f"Batch started: {data.get('total', '?')} jobs, concurrency={data.get('concurrency', 1)}")

                    elif etype == "job_start":
                        email = data.get("email", "?")
                        proxy = data.get("proxy_used", "direct") or "direct"
                        if "@" in proxy:
                            proxy = proxy.split("@")[-1]
                        idx = data.get("index", 0) + 1
                        log(f"[{idx}/{total}] {email} — starting (proxy: {proxy})")

                    elif etype == "job_log":
                        email = data.get("email", "?")
                        provider = data.get("provider", "")
                        step = data.get("step", "")
                        msg = data.get("message", "")
                        log(f"  [{provider}:{step}] {email} — {msg}")

                    elif etype == "provider_done":
                        email = data.get("email", "?")
                        provider = data.get("provider", "")
                        success = data.get("success", False)
                        status = "OK" if success else "FAIL"
                        log(f"  [{provider}] {email} — {status}")

                    elif etype == "import_ok":
                        email = data.get("email", "?")
                        provider = data.get("provider", "")
                        log(f"  [{provider}] {email} — imported")

                    elif etype == "import_error":
                        email = data.get("email", "?")
                        provider = data.get("provider", "")
                        error = data.get("error", "unknown")
                        log(f"  [{provider}] {email} — import FAILED: {error}")
                        pw = account_map.get(email, "?")
                        log_fail(email, pw, f"{provider} import: {error}")

                    elif etype == "job_done":
                        email = data.get("email", "?")
                        status = data.get("status", "?")
                        done_count += 1
                        if status == "failed":
                            errors = data.get("errors", {})
                            for prov, err in errors.items():
                                pw = account_map.get(email, "?")
                                log_fail(email, pw, f"{prov}: {err}")
                        log(f"[{done_count}/{total}] {email} — {status.upper()}")

                    elif etype == "batch_done":
                        ok = data.get("success", 0)
                        fail = data.get("failed", 0)
                        cancelled = data.get("cancelled", 0)
                        log(f"\nBatch complete: {ok} OK, {fail} failed, {cancelled} cancelled")
                        return

                    elif etype == "batch_skipped":
                        count = data.get("count", 0)
                        log(f"Skipped {count} accounts (already OK for selected providers)")

                    elif etype == "job_cancelled":
                        email = data.get("email", "?")
                        done_count += 1
                        log(f"[{done_count}/{total}] {email} — CANCELLED")

    except KeyboardInterrupt:
        log("\nInterrupted by user. Batch may still be running on server.")
        log("Use dashboard or call POST /api/batch/cancel to stop.")
    except Exception as e:
        log(f"SSE connection error: {e}")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print()
    print("=" * 60)
    print("  UnifiedMe — Add Accounts CLI")
    print("=" * 60)

    # Check proxy server
    print("\n  Checking proxy server...", end=" ")
    if not check_proxy_alive():
        print("FAILED")
        print(f"  [ERROR] Cannot connect to {BASE_URL}")
        print("  Make sure the proxy is running: cd unifiedme-ai && unifiedme run")
        sys.exit(1)
    print("OK")

    # ── Step 1: Proxy ──
    print("\n" + "-" * 40)
    print("  STEP 1: Proxy Configuration")
    print("-" * 40)
    proxy_file = prompt_input("Proxy file (.txt) or 'n' for direct", "n")

    proxies = []
    proxy_method = "direct"
    if proxy_file.lower() != "n":
        proxies = load_proxies(proxy_file)
        if not proxies:
            print("  No proxies loaded. Continuing with direct connection.")
        else:
            print(f"\n  Loaded {len(proxies)} proxies. Checking connectivity...")
            results = check_proxies(proxies)
            alive = [r for r in results if r["status"] == "ok"]
            dead = [r for r in results if r["status"] != "ok"]
            print(f"  Active: {len(alive)}  |  Dead: {len(dead)}")
            for r in alive:
                print(f"    [OK]   {r['url']} → {r['ip']}")
            for r in dead:
                print(f"    [FAIL] {r['url']} — {r['ip']}")

            if not alive:
                print("  [WARN] No working proxies! Continuing with direct connection.")
                proxies = []
            else:
                proxy_method = prompt_choice(
                    "Proxy method:",
                    ["sticky", "smart_rotate"],
                    default="smart_rotate",
                )

    # Note: proxy config is managed by the running server's settings.
    # The CLI just informs the user — actual proxy selection happens server-side.
    if proxies:
        print(f"\n  Proxy mode: {proxy_method} ({len(proxies)} proxies)")
        print("  Note: Proxy rotation is managed by the server's batch proxy settings.")
        print("  Make sure your dashboard Batch Login Proxies are configured correctly.")
    else:
        print("\n  Proxy mode: direct (no proxy)")

    # ── Step 2: Accounts ──
    print("\n" + "-" * 40)
    print("  STEP 2: Account List")
    print("-" * 40)
    while True:
        account_file = prompt_input("Account file (.txt, format: email:password)")
        if not account_file:
            print("  Account file is required.")
            continue
        accounts = load_accounts(account_file)
        if accounts:
            break
        print("  No valid accounts found. Try again.")

    print(f"\n  Loaded {len(accounts)} accounts")
    for i, (email, _) in enumerate(accounts[:5], 1):
        print(f"    {i}. {email}")
    if len(accounts) > 5:
        print(f"    ... and {len(accounts) - 5} more")

    # ── Step 3: Providers ──
    print("\n" + "-" * 40)
    print("  STEP 3: Provider Selection")
    print("-" * 40)
    providers = []
    if prompt_yn("Kiro?", default=False):
        providers.append("kiro")
    if prompt_yn("CodeBuddy?", default=True):
        providers.append("codebuddy")
    if prompt_yn("WaveSpeed?", default=False):
        providers.append("wavespeed")
    gumloop = prompt_yn("Gumloop?", default=False)
    if gumloop:
        providers.append("gumloop")

    if not providers:
        print("  [ERROR] Select at least one provider.")
        sys.exit(1)

    # ── Step 4: MCP (if Gumloop) ──
    mcp_urls = []
    if gumloop:
        print("\n" + "-" * 40)
        print("  STEP 4: MCP Server (Gumloop)")
        print("-" * 40)
        mcp_input = prompt_input("MCP server URL(s), comma-separated, or 'n' to skip", "n")
        if mcp_input.lower() != "n":
            mcp_urls = [u.strip() for u in mcp_input.split(",") if u.strip()]
            if mcp_urls:
                print(f"  MCP servers ({len(mcp_urls)}):")
                for u in mcp_urls:
                    print(f"    - {u}")

    # ── Step 5: Concurrency ──
    print("\n" + "-" * 40)
    print("  STEP 5: Parallel Browsers")
    print("-" * 40)
    concurrency = int(prompt_input("Number of parallel Camoufox instances", "1"))
    concurrency = max(1, min(10, concurrency))

    # ── Summary ──
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    print(f"  Accounts:    {len(accounts)}")
    print(f"  Providers:   {', '.join(providers)}")
    print(f"  Proxy:       {proxy_method} ({len(proxies)} proxies)" if proxies else "  Proxy:       direct")
    print(f"  MCP servers: {len(mcp_urls)}" if mcp_urls else "  MCP servers: none")
    print(f"  Concurrency: {concurrency}")
    print(f"  Log file:    {LOG_FILE}")
    print(f"  Fail file:   {FAIL_FILE}")
    print("=" * 60)

    if not prompt_yn("Start batch?", default=True):
        print("  Cancelled.")
        sys.exit(0)

    # ── Init log files ──
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"[{ts()}] New batch: {len(accounts)} accounts, providers={providers}\n")
        f.write(f"[{ts()}] Proxy: {proxy_method} ({len(proxies)}), MCP: {len(mcp_urls)}, Concurrency: {concurrency}\n")

    # ── Start batch via API ──
    print()
    account_lines = [f"{email}:{pw}" for email, pw in accounts]
    account_map = {email: pw for email, pw in accounts}

    try:
        result = api("POST", "/batch/start", {
            "accounts": account_lines,
            "providers": providers,
            "headless": True,
            "concurrency": concurrency,
            "mcp_urls": mcp_urls,
        })
        queued = result.get("count", 0)
        log(f"Queued {queued} jobs (server accepted)")
    except RuntimeError as e:
        log(f"[ERROR] Failed to start batch: {e}")
        sys.exit(1)

    # ── Stream progress ──
    stream_progress(account_map)

    # ── Final summary ──
    fail_count = 0
    if FAIL_FILE.exists():
        fail_count = len(FAIL_FILE.read_text(encoding="utf-8").strip().splitlines())

    print()
    print("=" * 60)
    if fail_count:
        print(f"  Failed accounts saved to: {FAIL_FILE} ({fail_count} entries)")
    else:
        print("  No failures!")
    print(f"  Full log: {LOG_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    main()
