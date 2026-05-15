#!/usr/bin/env python3
"""
Batch Gumloop Login + University Automation (New Version)

Integrates university automation with batch login flow:
  1. Login via Google OAuth (Camoufox)
  2. Create gummie + attach MCP
  3. Complete university courses (15,000 credits)
  4. Save credentials to result.json

This is the NEW version using the fixed script.
Old batch login (gumloop_login.py) remains available as backup.

Usage:
    python batch_gumloop_university.py --email X@gmail.com --password Y --mcp-url http://localhost:1430
    
    # Batch mode (from file):
    python batch_gumloop_university.py --batch accounts.txt --mcp-url http://localhost:1430
    
    # accounts.txt format:
    # email1@example.com:password1
    # email2@example.com:password2
    # ...
"""

import sys
import os
import argparse
import asyncio
import json
from pathlib import Path

# Import the university automation module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'production'))
try:
    from gumloop_university import main as university_main, parse_answers
except ImportError:
    print("❌ ERROR: Could not import university automation module")
    print("Make sure production/gumloop_university.py exists")
    sys.exit(1)


def log(msg: str):
    """Simple logging with timestamp"""
    import datetime
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {msg}")


async def process_single_account(email: str, password: str, mcp_url: str, answers=None):
    """Process single account through university automation"""
    log(f"Starting automation for {email}")
    
    try:
        # Run university automation (already includes login + MCP + courses)
        await university_main(email, password, mcp_url, answers)
        
        # Read result
        result_file = Path("production/result.json")
        if result_file.exists():
            with open(result_file) as f:
                result = json.load(f)
            
            log(f"✅ SUCCESS for {email}")
            log(f"   User ID: {result.get('user_id', 'N/A')}")
            log(f"   Gummie ID: {result.get('gummie_id', 'N/A')}")
            log(f"   MCP: {result.get('mcp_name', 'N/A')}")
            
            return {
                "success": True,
                "email": email,
                "data": result
            }
        else:
            log(f"⚠️  No result.json found for {email}")
            return {
                "success": False,
                "email": email,
                "error": "No result file generated"
            }
    
    except Exception as e:
        log(f"❌ FAILED for {email}: {e}")
        return {
            "success": False,
            "email": email,
            "error": str(e)
        }


async def process_batch_file(filepath: str, mcp_url: str, answers=None):
    """Process accounts from file (one per line: email:password)"""
    batch_file = Path(filepath)
    if not batch_file.exists():
        print(f"❌ ERROR: Batch file not found: {filepath}")
        sys.exit(1)
    
    accounts = []
    with open(batch_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            
            if ":" not in line:
                print(f"⚠️  Skipping invalid line: {line}")
                continue
            
            parts = line.split(":", 1)
            if len(parts) == 2:
                accounts.append({
                    "email": parts[0].strip(),
                    "password": parts[1].strip()
                })
    
    if not accounts:
        print(f"❌ No valid accounts found in {filepath}")
        sys.exit(1)
    
    log(f"📋 Found {len(accounts)} accounts to process")
    print()
    
    # Process each account sequentially (can't run browsers in parallel easily)
    results = []
    for i, account in enumerate(accounts, 1):
        log("=" * 60)
        log(f"ACCOUNT {i}/{len(accounts)}: {account['email']}")
        log("=" * 60)
        
        result = await process_single_account(
            account['email'],
            account['password'],
            mcp_url,
            answers
        )
        results.append(result)
        
        # Save incremental results
        batch_results_file = Path("batch_results.json")
        with open(batch_results_file, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        
        # Move individual result to batch folder
        result_file = Path("production/result.json")
        if result_file.exists():
            batch_dir = Path("batch_results")
            batch_dir.mkdir(exist_ok=True)
            
            email_safe = account['email'].replace("@", "_at_").replace(".", "_")
            dest = batch_dir / f"{email_safe}_result.json"
            result_file.rename(dest)
            log(f"   Result saved to: {dest}")
        
        print()
    
    # Final summary
    log("=" * 60)
    log("BATCH COMPLETE")
    log("=" * 60)
    successful = sum(1 for r in results if r["success"])
    failed = len(results) - successful
    
    log(f"Total: {len(results)} accounts")
    log(f"✅ Success: {successful}")
    log(f"❌ Failed: {failed}")
    
    if failed > 0:
        log("\nFailed accounts:")
        for r in results:
            if not r["success"]:
                log(f"  • {r['email']}: {r.get('error', 'Unknown error')}")
    
    log(f"\nResults saved to: batch_results.json")
    log(f"Individual results in: batch_results/")


def main():
    parser = argparse.ArgumentParser(
        description="Batch Gumloop Login + University Automation (New Version)"
    )
    
    # Single account mode
    parser.add_argument("--email", help="Email for single account mode")
    parser.add_argument("--password", help="Password for single account mode")
    
    # Batch mode
    parser.add_argument("--batch", help="Path to batch file (email:password per line)")
    
    # Common options
    parser.add_argument("--mcp-url", required=True, help="MCP server URL")
    parser.add_argument("--answers", help="Comma-separated quiz answers (optional)")
    
    args = parser.parse_args()
    
    # Parse answers if provided
    answers = None
    if args.answers:
        try:
            answers = parse_answers(args.answers)
        except Exception as e:
            print(f"❌ Invalid answers format: {e}")
            sys.exit(1)
    
    # Determine mode
    if args.batch:
        # Batch mode
        print()
        print("=" * 60)
        print("  Batch Gumloop University Automation (New Version)")
        print("=" * 60)
        print()
        asyncio.run(process_batch_file(args.batch, args.mcp_url, answers))
    
    elif args.email and args.password:
        # Single account mode
        print()
        print("=" * 60)
        print("  Gumloop University Automation (New Version)")
        print("=" * 60)
        print()
        asyncio.run(process_single_account(args.email, args.password, args.mcp_url, answers))
    
    else:
        parser.print_help()
        print()
        print("❌ ERROR: Must provide either --email + --password OR --batch")
        sys.exit(1)


if __name__ == "__main__":
    main()
