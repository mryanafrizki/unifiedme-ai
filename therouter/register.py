#!/usr/bin/env python3
"""
TheRouter automatic signup + login + API key creation.

Pure HTTP — no browser needed. Uses NextAuth.js credential flow.

Flow:
    1. Generate random name + email + password
    2. POST /api/auth/register  → create account
    3. GET  /api/auth/csrf      → get CSRF token + cookies
    4. POST /api/auth/callback/credentials → login (sets session cookie)
    5. POST /api/proxy/api-keys → create API key
    6. Output: JSON with email, password, api_key

Usage:
    # Single account (auto-generated credentials)
    python therouter/register.py

    # Batch: create N accounts
    python therouter/register.py --count 5

    # Custom email (skip random generation)
    python therouter/register.py --email user@gmail.com --password MyPass123!

    # batch_runner compatible (JSON lines to stdout)
    python therouter/register.py --batch-mode

    # With proxy
    python therouter/register.py --proxy socks5://user:pass@host:port
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import secrets
import string
import sys
import time

import httpx

THEROUTER_BASE = "https://dashboard.therouter.ai"

# ─── Name Generation ────────────────────────────────────────────────────────
# Seed syllables — will be shuffled/reversed to create natural-sounding names
_SEED_NAMES = [
    # 4-letter
    "zaki", "nove", "dane", "sata", "riko", "mela", "tova", "lune",
    "kael", "nira", "faye", "juno", "cleo", "raze", "onyx", "sage",
    "wren", "flux", "aria", "bram", "cade", "dion", "elan", "finn",
    "gale", "haze", "ivan", "jade", "kira", "leon", "milo", "noel",
    "omar", "pike", "reed", "seth", "tate", "vale", "axel", "beau",
    "cole", "drew", "eden", "ford", "glen", "hugo", "iris", "joel",
    "kane", "lara", "maya", "nash", "opal", "penn", "remy", "shay",
    "troy", "vera", "wade", "xena", "yara", "zane", "aldo", "bria",
    "cruz", "dara", "elsa", "fern", "gwen", "hank", "ines", "jace",
    "koda", "levi", "mira", "nico", "olga", "paco", "reva", "sven",
    # 3-letter
    "dex", "vex", "kai", "leo", "max", "neo", "rio", "sam", "tom",
    "ava", "eva", "ida", "ivy", "mia", "nia", "ora", "pia", "zoe",
    # 5-letter
    "asher", "blake", "caleb", "derek", "elena", "felix", "grace",
    "hazel", "irene", "james", "kylie", "lucas", "megan", "nolan",
    "olive", "petra", "quinn", "raven", "silas", "tessa", "uriel",
    "vince", "wyatt", "xavia", "yosef", "zelda",
]


def _scramble_name(name: str) -> str:
    """Scramble a seed name into a new plausible name.

    Strategies (picked randomly):
      - reverse
      - rotate characters
      - swap vowels/consonants
      - take random slice + pad
    """
    strategy = random.randint(0, 4)

    if strategy == 0:
        # Reverse
        return name[::-1]

    if strategy == 1:
        # Rotate by 1-2 positions
        n = random.randint(1, min(2, len(name) - 1))
        return name[n:] + name[:n]

    if strategy == 2:
        # Swap pairs
        chars = list(name)
        if len(chars) >= 2:
            i = random.randint(0, len(chars) - 2)
            chars[i], chars[i + 1] = chars[i + 1], chars[i]
        return "".join(chars)

    if strategy == 3:
        # Replace one vowel with another
        vowels = "aeiou"
        chars = list(name)
        vowel_positions = [i for i, c in enumerate(chars) if c in vowels]
        if vowel_positions:
            pos = random.choice(vowel_positions)
            replacement = random.choice([v for v in vowels if v != chars[pos]] or list(vowels))
            chars[pos] = replacement
        return "".join(chars)

    # strategy 4: combine halves of two different seeds
    other = random.choice(_SEED_NAMES)
    half1 = name[: len(name) // 2 + 1]
    half2 = other[len(other) // 2:]
    return half1 + half2


def generate_name() -> tuple[str, str]:
    """Generate a two-word name: 'FirstName LastName'.

    Returns (first_name, last_name) — both capitalized.
    """
    seed1, seed2 = random.sample(_SEED_NAMES, 2)
    first = _scramble_name(seed1).capitalize()
    last = _scramble_name(seed2).capitalize()
    return first, last


def generate_email(first: str, last: str, domain: str = "@gmail.com") -> str:
    """Generate email: firstname.lastname.NNNN@domain.com"""
    digits = f"{random.randint(1000, 9999)}"
    if not domain.startswith("@"):
        domain = f"@{domain}"
    return f"{first.lower()}.{last.lower()}.{digits}{domain}"


def generate_password(length: int = 14) -> str:
    """Generate a random password with letters, digits, and symbols."""
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    # Ensure at least one of each category
    pw = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%&*"),
    ]
    pw += [secrets.choice(alphabet) for _ in range(length - 4)]
    random.shuffle(pw)
    return "".join(pw)


# ─── HTTP Client ────────────────────────────────────────────────────────────

def _build_client(proxy: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15, read=30, write=15, pool=10),
        follow_redirects=False,  # We manage redirects manually for cookie handling
        proxy=proxy,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.6778.86 Safari/537.36"
            ),
        },
    )


# ─── Registration Flow ─────────────────────────────────────────────────────

async def register_account(
    client: httpx.AsyncClient,
    email: str,
    password: str,
    name: str,
) -> dict:
    """Step 1: Create account via /api/auth/register."""
    resp = await client.post(
        f"{THEROUTER_BASE}/api/auth/register",
        json={"email": email, "password": password, "name": name},
        headers={"Content-Type": "application/json"},
    )

    if resp.status_code != 200:
        body = resp.text
        raise RuntimeError(f"Register failed: HTTP {resp.status_code} — {body[:500]}")

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Register failed: {data}")

    return data


async def login_account(
    client: httpx.AsyncClient,
    email: str,
    password: str,
) -> dict[str, str]:
    """Step 2: Login via NextAuth credential flow.

    1. GET /api/auth/csrf → csrfToken + __Host-authjs.csrf-token cookie
    2. POST /api/auth/callback/credentials → session cookie

    Returns cookies dict for subsequent requests.
    """
    # Get CSRF token
    csrf_resp = await client.get(f"{THEROUTER_BASE}/api/auth/csrf")
    if csrf_resp.status_code != 200:
        raise RuntimeError(f"CSRF fetch failed: HTTP {csrf_resp.status_code}")

    csrf_data = csrf_resp.json()
    csrf_token = csrf_data.get("csrfToken", "")
    if not csrf_token:
        raise RuntimeError(f"No csrfToken in response: {csrf_data}")

    # Login with credentials (form-urlencoded, like NextAuth expects)
    login_resp = await client.post(
        f"{THEROUTER_BASE}/api/auth/callback/credentials?",
        data={
            "email": email,
            "password": password,
            "csrfToken": csrf_token,
            "callbackUrl": f"{THEROUTER_BASE}/login",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "x-auth-return-redirect": "1",
        },
    )

    if login_resp.status_code not in (200, 302):
        raise RuntimeError(f"Login failed: HTTP {login_resp.status_code} — {login_resp.text[:500]}")

    # Verify session is active
    session_resp = await client.get(
        f"{THEROUTER_BASE}/api/auth/session",
        headers={"Content-Type": "application/json"},
    )

    if session_resp.status_code != 200:
        raise RuntimeError(f"Session check failed: HTTP {session_resp.status_code}")

    session_data = session_resp.json()
    user = session_data.get("user")
    if not user:
        raise RuntimeError(f"No user in session: {session_data}")

    return {
        "user_id": user.get("id", ""),
        "tenant_id": user.get("tenantId", ""),
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        "quotas": user.get("quotas", {}),
    }


async def create_api_key(
    client: httpx.AsyncClient,
    key_name: str,
) -> str:
    """Step 3: Create API key via /api/proxy/api-keys."""
    resp = await client.post(
        f"{THEROUTER_BASE}/api/proxy/api-keys",
        json={"name": key_name},
        headers={"Content-Type": "application/json"},
    )

    if resp.status_code != 200:
        raise RuntimeError(f"API key creation failed: HTTP {resp.status_code} — {resp.text[:500]}")

    data = resp.json()
    api_key = data.get("api_key", "")
    if not api_key:
        raise RuntimeError(f"No api_key in response: {data}")

    return api_key


# ─── Full Flow ──────────────────────────────────────────────────────────────

async def create_full_account(
    email: str | None = None,
    password: str | None = None,
    name: str | None = None,
    proxy: str | None = None,
    batch_mode: bool = False,
    domain: str = "@gmail.com",
) -> dict:
    """Run the full signup → login → create API key flow.

    Returns dict with email, password, name, api_key, user_id, tenant_id.
    """
    # Generate credentials if not provided
    if not name:
        first, last = generate_name()
        name = f"{first} {last}"
    else:
        parts = name.split()
        first = parts[0] if parts else "User"
        last = parts[1] if len(parts) > 1 else "Name"

    if not email:
        email = generate_email(first, last, domain)

    if not password:
        password = generate_password()

    def emit(data: dict):
        if batch_mode:
            try:
                print(json.dumps(data, ensure_ascii=True), flush=True)
            except Exception:
                pass
        else:
            msg = data.get("message", data.get("step", ""))
            if msg:
                print(f"  [{data.get('step', '?')}] {msg}")

    result = {
        "email": email,
        "password": password,
        "name": name,
        "api_key": "",
        "user_id": "",
        "tenant_id": "",
        "quotas": {},
        "error": "",
    }

    async with _build_client(proxy) as client:
        # Step 1: Register
        emit({"type": "progress", "step": "register", "message": f"Registering {email}..."})
        try:
            await register_account(client, email, password, name)
            emit({"type": "progress", "step": "register", "message": "Account created"})
        except Exception as e:
            error_msg = str(e)
            # If "already exists" — skip to login
            if "already" in error_msg.lower() or "exists" in error_msg.lower():
                emit({"type": "progress", "step": "register", "message": "Account exists, trying login..."})
            else:
                result["error"] = error_msg
                emit({"type": "error", "step": "register", "message": error_msg})
                return result

        # Step 2: Login
        emit({"type": "progress", "step": "login", "message": "Logging in..."})
        try:
            session_info = await login_account(client, email, password)
            result["user_id"] = session_info["user_id"]
            result["tenant_id"] = session_info["tenant_id"]
            result["quotas"] = session_info.get("quotas", {})
            emit({"type": "progress", "step": "login", "message": f"Logged in as {session_info['email']}"})
        except Exception as e:
            result["error"] = str(e)
            emit({"type": "error", "step": "login", "message": str(e)})
            return result

        # Step 3: Create API key
        emit({"type": "progress", "step": "api_key", "message": "Creating API key..."})
        try:
            api_key = await create_api_key(client, name)
            result["api_key"] = api_key
            emit({"type": "progress", "step": "api_key", "message": f"API key: {api_key[:20]}..."})
        except Exception as e:
            result["error"] = str(e)
            emit({"type": "error", "step": "api_key", "message": str(e)})
            return result

    return result


# ─── CLI ────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="TheRouter auto signup + login + API key")
    parser.add_argument("--email", type=str, help="Email (auto-generated if omitted)")
    parser.add_argument("--password", type=str, help="Password (auto-generated if omitted)")
    parser.add_argument("--name", type=str, help="Display name (auto-generated if omitted)")
    parser.add_argument("--count", type=int, default=1, help="Number of accounts to create (default: 1)")
    parser.add_argument("--domain", type=str, default="@gmail.com", help="Email domain (default: @gmail.com)")
    parser.add_argument("--proxy", type=str, help="Proxy URL (socks5://user:pass@host:port)")
    parser.add_argument("--batch-mode", action="store_true", help="JSON lines output (for batch_runner)")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between accounts in seconds (default: 2)")
    args = parser.parse_args()

    if not args.batch_mode:
        print()
        print("=" * 60)
        print("  TheRouter Auto Registration")
        print("=" * 60)
        print(f"  Accounts to create: {args.count}")
        if args.proxy:
            print(f"  Proxy: {args.proxy}")
        print()

    results = []

    for i in range(args.count):
        if not args.batch_mode and args.count > 1:
            print(f"\n  ── Account {i + 1}/{args.count} ──")

        # Only use provided email/password for single account
        email = args.email if args.count == 1 else None
        password = args.password if args.count == 1 else None
        name = args.name if args.count == 1 else None

        result = await create_full_account(
            email=email,
            password=password,
            name=name,
            proxy=args.proxy,
            batch_mode=args.batch_mode,
            domain=args.domain,
        )
        results.append(result)

        if args.batch_mode:
            print(json.dumps({"type": "result", **result}, ensure_ascii=True), flush=True)

        # Delay between accounts
        if i < args.count - 1 and args.delay > 0:
            if not args.batch_mode:
                print(f"  Waiting {args.delay}s...")
            await asyncio.sleep(args.delay)

    # Summary
    if not args.batch_mode:
        success = [r for r in results if r["api_key"]]
        failed = [r for r in results if r["error"]]

        print()
        print("=" * 60)
        print(f"  Results: {len(success)} success, {len(failed)} failed")
        print("=" * 60)

        for r in results:
            status = "OK" if r["api_key"] else "FAIL"
            print(f"\n  [{status}] {r['email']}")
            print(f"    Password: {r['password']}")
            if r["api_key"]:
                print(f"    API Key:  {r['api_key']}")
                print(f"    User ID:  {r['user_id']}")
                print(f"    Tenant:   {r['tenant_id']}")
                if r.get("quotas"):
                    q = r["quotas"]
                    print(f"    Quotas:   daily={q.get('daily_quota', '?')} rpm={q.get('rate_limit_per_minute', '?')} concurrency={q.get('concurrency_limit', '?')}")
            if r["error"]:
                print(f"    Error:    {r['error']}")

        print()

        # Save to file
        output_file = "therouter/registered_accounts.json"
        try:
            import os
            existing = []
            if os.path.exists(output_file):
                with open(output_file) as f:
                    existing = json.load(f)
            existing.extend(results)
            with open(output_file, "w") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
            print(f"  Saved to {output_file} ({len(existing)} total accounts)")
        except Exception as e:
            print(f"  [WARN] Could not save: {e}")

        print()


if __name__ == "__main__":
    asyncio.run(main())
