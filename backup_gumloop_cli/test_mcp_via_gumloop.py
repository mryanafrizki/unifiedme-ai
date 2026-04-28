#!/usr/bin/env python3
"""
Test MCP tools via Gumloop WebSocket API.

Sends a message to the gummie (which has MCP attached),
waits for the agent to call MCP tools, and prints all events.

Usage:
    python _tmp_mcp_server/test_mcp_via_gumloop.py
    python _tmp_mcp_server/test_mcp_via_gumloop.py --task "list files in workspace"
"""

import argparse
import asyncio
import io
import json
import os
import sys
import time
import uuid

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unified.gumloop.auth import GumloopAuth
from unified.gumloop.client import send_chat, update_gummie_config
from unified.gumloop.turnstile import TurnstileSolver

CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "5ff220e19373d967a494cf020fe454b7")


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", flush=True)


# Test tasks for MCP tools
TEST_TASKS = {
    "list": "Use the list_directory tool to list all files in the current workspace root directory. Show me what you find.",
    "write": 'Use the write_file tool to create a file called "hello.txt" with the content "Hello from Gumloop MCP test! Created at ' + time.strftime("%Y-%m-%d %H:%M:%S") + '". Then use read_file to verify it was created.',
    "glob": 'Use the glob tool to find all files matching "*.txt" in the workspace. Show the results.',
    "edit": 'First use read_file to read "hello.txt", then use edit_file to replace "Hello" with "Greetings" in that file. Then read it again to verify.',
    "grep": 'Use the grep tool to search for the pattern "Gumloop" in all files. Show matching lines.',
    "bash": 'Use the bash tool to run "echo Hello from bash && date" and show the output.',
    "full": """Do these tasks in order using MCP tools:
1. Use list_directory to see what's in the workspace
2. Use write_file to create "test_app.py" with a simple Python hello world script
3. Use read_file to verify test_app.py was created
4. Use bash to run "python test_app.py" and show the output
5. Use glob to find all .py files
6. Summarize what you did""",
}


async def run_test(task_name: str, custom_task: str | None = None):
    # Load credentials
    creds_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "_tmp_auto_mcp", "result.json",
    )
    if not os.path.exists(creds_file):
        print(f"  ERROR: Credentials not found at {creds_file}")
        print(f"  Run auto_register_mcp.py first.")
        return

    with open(creds_file) as f:
        creds = json.load(f)

    gummie_id = creds["gummie_id"]
    user_id = creds["user_id"]
    model = "claude-opus-4-6"

    # Create auth
    auth = GumloopAuth(
        refresh_token=creds["refresh_token"],
        user_id=user_id,
        id_token=creds["id_token"],
    )

    # Refresh token first
    log("Refreshing auth token...")
    try:
        await auth.refresh()
        log(f"Token refreshed OK (uid={user_id[:8]}...)")
    except Exception as e:
        log(f"Token refresh failed: {e}")
        return

    # Setup captcha solver
    turnstile = TurnstileSolver(CAPTCHA_API_KEY)
    log("Solving Turnstile captcha (this may take 30-60s)...")
    captcha_token = await turnstile.get_token()
    if captcha_token:
        log(f"Captcha solved OK (len={len(captcha_token)})")
    else:
        log("WARNING: No captcha token — request may be rejected")

    # Update gummie model + system prompt
    system_prompt = (
        "You are a coding assistant with access to a local workspace via MCP tools. "
        "ALWAYS use MCP tools (read_file, write_file, edit_file, bash, list_directory, glob, grep, download_image) for ALL file operations. "
        "NEVER use sandbox_python, sandbox_file, or any sandbox tools. They run on a remote server, NOT the user's machine. "
        "ALL files MUST be created in the MCP workspace using write_file."
    )
    log(f"Setting model to {model} + system prompt...")
    try:
        await update_gummie_config(
            gummie_id=gummie_id,
            auth=auth,
            model_name=model,
            system_prompt=system_prompt,
        )
        log("Model + prompt set OK")
    except Exception as e:
        log(f"Config set failed: {e}")

    # Get task message
    if custom_task:
        message = custom_task
    elif task_name in TEST_TASKS:
        message = TEST_TASKS[task_name]
    else:
        message = task_name  # Use as-is

    messages = [{"role": "user", "content": message}]

    print()
    print("=" * 60)
    print(f"  Task: {task_name}")
    print(f"  Model: {model}")
    print(f"  Gummie: {gummie_id}")
    print("=" * 60)
    print(f"  Message: {message[:100]}{'...' if len(message) > 100 else ''}")
    print("=" * 60)
    print()

    # Send and stream
    log("Sending message via WebSocket...")
    full_text = []
    tool_calls = []
    step = 0

    try:
        async for event in send_chat(gummie_id, messages, auth, turnstile=turnstile):
            etype = event.get("type", "")

            if etype == "text-delta":
                delta = event.get("delta", "")
                if delta:
                    print(delta, end="", flush=True)
                    full_text.append(delta)

            elif etype == "reasoning-delta":
                delta = event.get("delta", "")
                if delta:
                    # Print reasoning in gray
                    print(f"\033[90m{delta}\033[0m", end="", flush=True)

            elif etype == "text-start":
                pass

            elif etype == "text-end":
                pass

            elif etype == "tool-call":
                tool_name = event.get("toolName", "?")
                tool_input = event.get("input", {})
                step += 1
                print(f"\n\033[33m  > [{step}] Tool: {tool_name}\033[0m", flush=True)
                input_str = json.dumps(tool_input, ensure_ascii=False, indent=2)
                if len(input_str) > 300:
                    input_str = input_str[:300] + "..."
                print(f"\033[33m    Input: {input_str}\033[0m", flush=True)
                tool_calls.append({"tool": tool_name, "input": tool_input})

            elif etype == "tool-result":
                tool_name = event.get("toolName", "?")
                output = event.get("output", "")
                if isinstance(output, dict):
                    output_str = json.dumps(output, ensure_ascii=False, indent=2)
                else:
                    output_str = str(output)
                if len(output_str) > 500:
                    output_str = output_str[:500] + "..."
                print(f"\033[32m    Result: {output_str}\033[0m", flush=True)

            elif etype == "tool-input-start":
                pass

            elif etype == "tool-input-delta":
                pass

            elif etype == "step-start":
                pass

            elif etype == "finish":
                is_final = event.get("final", True)
                usage = event.get("usage", {})
                if is_final:
                    print()
                    print()
                    print("=" * 60)
                    print(f"  DONE")
                    print(f"  Tool calls: {len(tool_calls)}")
                    for tc in tool_calls:
                        print(f"    - {tc['tool']}")
                    print(f"  Usage: {json.dumps(usage)}")
                    print(f"  Text length: {len(''.join(full_text))} chars")
                    print("=" * 60)
                    break
                else:
                    log(f"Step finish (not final), continuing...")

    except Exception as e:
        print(f"\n\n  ERROR: {e}")
        import traceback
        traceback.print_exc()


async def main():
    parser = argparse.ArgumentParser(description="Test MCP via Gumloop")
    parser.add_argument(
        "--task", default="list",
        help=f"Test task: {', '.join(TEST_TASKS.keys())} or custom message",
    )
    args = parser.parse_args()

    await run_test(args.task)


if __name__ == "__main__":
    asyncio.run(main())
