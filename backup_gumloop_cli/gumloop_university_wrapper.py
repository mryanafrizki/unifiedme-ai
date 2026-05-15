#!/usr/bin/env python3
"""
Wrapper for batch_gumloop_university.py - Backward Compatible with gumloop_login.py

This wrapper maintains the same CLI interface as the old gumloop_login.py
so it can be used as a drop-in replacement in existing systems.

Interface:
    python gumloop_university_wrapper.py --email X@gmail.com --password Y [--mcp-url http://localhost:1430]
    
The --mcp-url is optional. If not provided:
- Tries to detect running MCP server from the proxy database
- Falls back to http://localhost:1430
- Falls back to skipping university automation if no MCP found

Output format matches old script: JSON lines with {"type": "result", "gumloop": {...}}
"""

import sys
import os
import argparse
import asyncio
import json
from pathlib import Path

# Import the new batch script
sys.path.insert(0, os.path.dirname(__file__))
try:
    from batch_gumloop_university import process_single_account
except ImportError as e:
    print(json.dumps({
        "type": "error",
        "step": "import",
        "error": f"Could not import batch_gumloop_university: {e}"
    }))
    sys.exit(1)


def detect_mcp_url() -> str | None:
    """Try to detect MCP URL from unified proxy database or environment."""
    
    # 1. Check environment variable
    env_mcp = os.getenv("MCP_SERVER_URL", "").strip()
    if env_mcp:
        return env_mcp
    
    # 2. Check if unified proxy database exists and has MCP instances
    try:
        db_path = Path(__file__).parent.parent / "unified" / "data" / "unified.db"
        if db_path.exists():
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute("""
                SELECT url FROM mcp_instances 
                WHERE status = 'running' 
                ORDER BY id DESC LIMIT 1
            """)
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
    except Exception:
        pass  # Silent fallback
    
    # 3. Default to localhost:1430
    return "http://localhost:1430"


async def main():
    parser = argparse.ArgumentParser(
        description="Gumloop University Automation (Wrapper - Backward Compatible)"
    )
    parser.add_argument("--email", required=True, help="Gmail account email")
    parser.add_argument("--password", required=True, help="Gmail account password")
    parser.add_argument("--mcp-url", help="MCP server URL (optional, auto-detected if not provided)")
    
    args = parser.parse_args()
    
    # Detect or use provided MCP URL
    mcp_url = args.mcp_url or detect_mcp_url()
    
    if not mcp_url:
        print(json.dumps({
            "type": "warning",
            "step": "mcp_detection",
            "message": "No MCP server URL found. Will skip university automation."
        }))
    else:
        print(json.dumps({
            "type": "info",
            "step": "mcp_detection",
            "message": f"Using MCP server: {mcp_url}"
        }))
    
    # Call the new batch script
    try:
        result = await process_single_account(
            email=args.email,
            password=args.password,
            mcp_url=mcp_url or "",  # Empty string if no MCP
            answers=None  # Will use defaults
        )
        
        # Output in the format expected by batch_runner
        # Old format: {"type": "result", "gumloop": {...}}
        if result:
            print(json.dumps({
                "type": "result",
                "gumloop": result
            }))
            sys.exit(0 if result.get("success") else 1)
        else:
            print(json.dumps({
                "type": "error",
                "step": "automation",
                "error": "No result returned from automation"
            }))
            sys.exit(1)
            
    except Exception as e:
        print(json.dumps({
            "type": "error",
            "step": "automation",
            "error": str(e)
        }))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
