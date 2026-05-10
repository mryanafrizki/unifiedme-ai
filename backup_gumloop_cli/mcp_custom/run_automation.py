"""
Helper script untuk run automation dengan config file
"""

import asyncio
import json
import sys
from pathlib import Path
from gumloop_university_automation import GumloopUniversityAutomation


async def run_from_config(config_path: str = "config.json"):
    """Run automation dari config file"""
    
    # Load config
    config_file = Path(config_path)
    if not config_file.exists():
        print(f"❌ Config file not found: {config_path}")
        print("📝 Copy config_example.json to config.json and fill in your details")
        sys.exit(1)
        
    with open(config_file, 'r') as f:
        config = json.load(f)
        
    # Validate
    required_fields = ["email", "password", "mcp_server_config", "quiz_answers"]
    for field in required_fields:
        if field not in config:
            print(f"❌ Missing required field in config: {field}")
            sys.exit(1)
            
    if len(config["quiz_answers"]) != 6:
        print(f"❌ quiz_answers must have exactly 6 items, found {len(config['quiz_answers'])}")
        sys.exit(1)
        
    # Create automation instance
    automation = GumloopUniversityAutomation(
        email=config["email"],
        password=config["password"],
        mcp_server_config=config["mcp_server_config"],
        quiz_answers=config["quiz_answers"],
        headless=config.get("headless", False),
    )
    
    # Run
    print("🚀 Starting Gumloop University automation...")
    print(f"📧 Email: {config['email']}")
    print(f"🔧 MCP Server: {config['mcp_server_config']['name']}")
    print(f"📝 Quiz answers loaded: {len(config['quiz_answers'])} items")
    print("-" * 60)
    
    await automation.run_full_flow(
        skip_account_creation=config.get("skip_account_creation", False)
    )


if __name__ == "__main__":
    config_file = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    asyncio.run(run_from_config(config_file))
