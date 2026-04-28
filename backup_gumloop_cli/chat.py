#!/usr/bin/env python3
"""
Interactive MCP Chat — like OpenCode but via Gumloop.

Captcha solved ONCE at startup. Multi-turn conversation preserved.
Type your prompts, agent uses MCP tools on your workspace.

Usage:
    python _tmp_mcp_server/chat.py
    python _tmp_mcp_server/chat.py --model claude-opus-4-6
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

# ANSI colors
C_RESET = "\033[0m"
C_GRAY = "\033[90m"
C_YELLOW = "\033[33m"
C_GREEN = "\033[32m"
C_CYAN = "\033[36m"
C_RED = "\033[31m"
C_BOLD = "\033[1m"


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"  {C_GRAY}[{ts}]{C_RESET} {msg}", flush=True)


async def send_and_stream(
    gummie_id: str,
    messages: list[dict],
    auth: GumloopAuth,
    turnstile: TurnstileSolver,
    interaction_id: str = "",
) -> tuple[str, list[dict]]:
    """Send messages and stream response. Returns (full_text, tool_calls)."""
    full_text = []
    tool_calls = []
    step = 0
    needs_captcha_retry = False

    try:
        async for event in send_chat(
            gummie_id, messages, auth,
            turnstile=turnstile,
            interaction_id=interaction_id or None,
        ):
            etype = event.get("type", "")

            if etype == "text-delta":
                delta = event.get("delta", "")
                if delta:
                    print(delta, end="", flush=True)
                    full_text.append(delta)

            elif etype == "reasoning-delta":
                delta = event.get("delta", "")
                if delta:
                    print(f"{C_GRAY}{delta}{C_RESET}", end="", flush=True)

            elif etype == "tool-call":
                tool_name = event.get("toolName", "?")
                tool_input = event.get("input", {})
                step += 1
                input_str = json.dumps(tool_input, ensure_ascii=False)
                if len(input_str) > 200:
                    input_str = input_str[:200] + "..."
                print(f"\n{C_YELLOW}  > [{step}] {tool_name}({input_str}){C_RESET}", flush=True)
                tool_calls.append({"tool": tool_name, "input": tool_input})

            elif etype == "tool-result":
                tool_name = event.get("toolName", "?")
                output = event.get("output", "")
                if isinstance(output, dict):
                    output_str = json.dumps(output, ensure_ascii=False)
                else:
                    output_str = str(output)
                if len(output_str) > 300:
                    output_str = output_str[:300] + "..."
                print(f"{C_GREEN}    -> {output_str}{C_RESET}", flush=True)

            elif etype == "finish":
                if event.get("final", True):
                    break

    except Exception as e:
        err_str = str(e)
        if "1008" in err_str or "policy violation" in err_str:
            # Captcha expired — need to re-solve
            needs_captcha_retry = True
            print(f"\n{C_RED}  Captcha expired, re-solving...{C_RESET}", flush=True)
        else:
            print(f"\n{C_RED}  Error: {e}{C_RESET}", flush=True)

    if needs_captcha_retry:
        log("Re-solving captcha...")
        # Force fresh solve by clearing cached token
        turnstile._ready_token = None
        turnstile._ready_at = 0
        token = await turnstile.get_token()
        if token:
            log(f"Captcha re-solved OK (len={len(token)})")
            # Retry the request
            return await send_and_stream(gummie_id, messages, auth, turnstile, interaction_id)
        else:
            print(f"{C_RED}  Captcha re-solve failed{C_RESET}")

    return "".join(full_text), tool_calls


async def main(model: str, session_id: str = ""):
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

    print()
    print(f"  {C_BOLD}{'=' * 50}{C_RESET}")
    print(f"  {C_BOLD}  MCP Chat — Gumloop + Cloudflare Tunnel{C_RESET}")
    print(f"  {C_BOLD}{'=' * 50}{C_RESET}")
    print(f"  Model:   {model}")
    print(f"  Gummie:  {gummie_id}")
    print(f"  Account: {creds['email']}")
    print()

    # Auth
    auth = GumloopAuth(
        refresh_token=creds["refresh_token"],
        user_id=user_id,
        id_token=creds["id_token"],
    )
    log("Refreshing auth token...")
    try:
        await auth.refresh()
        log("Token OK")
    except Exception as e:
        log(f"Token refresh failed: {e}")
        return

    # Captcha — solve ONCE
    turnstile = TurnstileSolver(CAPTCHA_API_KEY)
    log("Solving captcha (one-time, ~15s)...")
    token = await turnstile.get_token()
    if token:
        log(f"Captcha OK (len={len(token)})")
    else:
        log("WARNING: No captcha — may get rejected")

    # Set model + system prompt
    system_prompt = """You are a coding assistant. You have MCP tools connected to the user's LOCAL workspace.

MANDATORY RULES (never violate):
1. For ALL file operations: ONLY use MCP tools (read_file, write_file, edit_file, bash, list_directory, glob, grep, download_image).
2. NEVER use sandbox_python, sandbox_file, sandbox_download, or ANY sandbox tool. They are on a remote server, NOT the user's machine.
3. ALL output files (code, html, text) → write_file.
4. ALL shell commands → bash.
5. IMAGE WORKFLOW (critical):
   a. Generate image with image_generator tool → you get a response with storage_link (gl:// URL)
   b. Immediately call download_image with the EXACT gl:// URL and a filename
   c. Example: download_image(url="gl://uid-xxx/custom_agent_interactions/.../image.png", filename="output.png")
   d. NEVER use sandbox_download. NEVER convert gl:// URLs to gumloop.com/files/ URLs.
   e. The download_image MCP tool handles gl:// authentication internally.
6. Respond in the same language as the user."""

    log(f"Setting model to {model} + system prompt...")
    try:
        await update_gummie_config(
            gummie_id=gummie_id, auth=auth,
            model_name=model, system_prompt=system_prompt,
        )
        log("Model + system prompt set OK")
    except Exception as e:
        log(f"Config set failed: {e}")

    print()
    print(f"  {C_CYAN}Ready. Type your prompts. Ctrl+C to exit.{C_RESET}")
    print(f"  {C_GRAY}Commands: /clear (reset history), /model <name>, /quit{C_RESET}")
    print()

    # Conversation history + persistent session ID
    conversation: list[dict] = []
    if session_id:
        interaction_id = session_id
        log(f"Resuming session: {interaction_id}")
    else:
        interaction_id = uuid.uuid4().hex[:22]
        log(f"New session: {interaction_id}")
    turn = 0

    while True:
        try:
            # Multi-line input support: empty line submits
            print(f"{C_BOLD}You:{C_RESET} ", end="", flush=True)
            lines = []
            first_line = input()
            if not first_line.strip():
                continue
            lines.append(first_line)

            user_input = "\n".join(lines).strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n\n  {C_GRAY}Bye!{C_RESET}")
            break

        if not user_input:
            continue

        # Commands
        if user_input.startswith("/"):
            cmd = user_input.lower().split()
            if cmd[0] == "/quit" or cmd[0] == "/exit":
                print(f"  {C_GRAY}Bye!{C_RESET}")
                break
            elif cmd[0] == "/clear":
                conversation.clear()
                interaction_id = uuid.uuid4().hex[:22]
                turn = 0
                print(f"  {C_GRAY}History cleared. New session: {interaction_id}{C_RESET}")
                continue
            elif cmd[0] == "/model" and len(cmd) > 1:
                new_model = cmd[1]
                try:
                    await update_gummie_config(gummie_id=gummie_id, auth=auth, model_name=new_model)
                    model = new_model
                    print(f"  {C_GREEN}Model changed to {model}{C_RESET}")
                except Exception as e:
                    print(f"  {C_RED}Failed: {e}{C_RESET}")
                continue
            elif cmd[0] == "/help":
                print(f"  {C_GRAY}/clear  - Reset conversation history{C_RESET}")
                print(f"  {C_GRAY}/model  - Change model (e.g. /model claude-sonnet-4-5){C_RESET}")
                print(f"  {C_GRAY}/quit   - Exit{C_RESET}")
                continue

        # Add user message
        conversation.append({"role": "user", "content": user_input})
        turn += 1

        print(f"\n{C_BOLD}Assistant:{C_RESET} ", end="", flush=True)

        # Refresh token if needed (tokens expire after ~1h)
        try:
            await auth.get_token()
        except Exception:
            pass

        # Send and stream (same interaction_id = same chat session)
        response_text, tool_calls = await send_and_stream(
            gummie_id, conversation, auth, turnstile,
            interaction_id=interaction_id,
        )

        # Add assistant response to history
        if response_text:
            conversation.append({"role": "assistant", "content": response_text})

        print()
        if tool_calls:
            print(f"  {C_GRAY}[{len(tool_calls)} tool call(s) | turn {turn}]{C_RESET}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interactive MCP Chat via Gumloop")
    parser.add_argument("--model", default="claude-opus-4-6", help="Model name (default: claude-opus-4-6)")
    parser.add_argument("--session", default="", help="Resume a previous session ID (e.g. 52325010792f4fd5b2671f)")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.model, session_id=args.session))
    except KeyboardInterrupt:
        print(f"\n  Bye!")
