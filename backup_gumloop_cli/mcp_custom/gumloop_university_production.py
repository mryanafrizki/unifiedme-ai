#!/usr/bin/env python3
"""
Gumloop University Automation - Production Version
Auto-complete university courses + MCP server setup

Production mode:
- No screenshot saving
- No request log files
- Only essential result.json output
- Optimized for batch operations

Usage:
    python gumloop_university_production.py \\
        --email user@example.com \\
        --password secret123 \\
        --mcp-url http://localhost:1430 \\
        [--answers "2,3,3,2,2,2,2,2,2,2,2,2,2"]

Output:
    result.json - Contains credentials, tokens, gummie_id, MCP info
"""

import os
import sys

# Disable all debug file outputs for production
PRODUCTION_MODE = True
SAVE_SCREENSHOTS = False
SAVE_REQUEST_LOG = False

# Import and patch the main module
sys.path.insert(0, os.path.dirname(__file__))

# Monkey-patch logging to suppress file writes
_original_captured = []

def _noop_save(*args, **kwargs):
    """No-op for file saving in production."""
    pass

# Import main module AFTER setting flags
import intercept_gumloop_university as main_module

# Patch screenshot saving
async def _no_screenshot(page, filename):
    """Disabled screenshot saving for production."""
    pass

# Patch the module
if hasattr(main_module, 'save_screenshot'):
    main_module.save_screenshot = _no_screenshot

# Run main
if __name__ == "__main__":
    import argparse
    import asyncio
    
    parser = argparse.ArgumentParser(description="Gumloop University Automation (Production)")
    parser.add_argument("--email", required=True, help="Google account email")
    parser.add_argument("--password", required=True, help="Google account password")
    parser.add_argument("--mcp-url", required=True, help="MCP server URL")
    parser.add_argument("--answers", help="Comma-separated quiz answers (optional)")
    
    args = parser.parse_args()
    
    # Parse answers
    answers = None
    if args.answers:
        answers = main_module.parse_answers(args.answers)
    
    # Run main
    asyncio.run(main_module.main(args.email, args.password, args.mcp_url, answers))
