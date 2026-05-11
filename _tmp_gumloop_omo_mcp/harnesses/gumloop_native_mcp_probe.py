from __future__ import annotations

import argparse
import io
import sys
import time
import uuid
from pathlib import Path


if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "_tmp_gumloop_omo_mcp"
sys.path.insert(0, str(ROOT))

from _tmp_gumloop_omo_mcp.wrappers.event_recorder import EventRecorder
from _tmp_gumloop_omo_mcp.wrappers.gumloop_client_wrapper import load_config, stream_gumloop_events


PROMPTS = {
    "list_files": (SANDBOX / "fixtures" / "prompts" / "list_files.txt").read_text(encoding="utf-8").strip(),
    "read_file": (SANDBOX / "fixtures" / "prompts" / "read_file.txt").read_text(encoding="utf-8").strip(),
    "write_verify": (SANDBOX / "fixtures" / "prompts" / "write_verify.txt").read_text(encoding="utf-8").strip(),
    "bash_verify": (SANDBOX / "fixtures" / "prompts" / "bash_verify.txt").read_text(encoding="utf-8").strip(),
}


SYSTEM_PROMPT = (
    "You are validating MCP access for a local sandbox workspace. "
    "Always use MCP tools for filesystem and shell operations. "
    "Never use remote sandbox tools. Keep all writes inside the current MCP workspace."
)


async def run_probe(config_path: Path, case: str) -> None:
    config = load_config(config_path)
    output_path = SANDBOX / "outputs" / f"gumloop_probe_{case}_{int(time.time())}.jsonl"
    recorder = EventRecorder(output_path)
    interaction_id = uuid.uuid4().hex[:22]
    messages = [{"role": "user", "content": PROMPTS[case]}]

    print(f"case={case}")
    print(f"output={output_path}")
    print(f"interaction_id={interaction_id}")

    async for event in stream_gumloop_events(config, messages, SYSTEM_PROMPT, interaction_id=interaction_id):
        recorder.write(event)
        event_type = event.get("type", "")
        if event_type == "text-delta":
            delta = event.get("delta", "")
            if delta:
                print(delta, end="", flush=True)
        elif event_type == "tool-call":
            print(f"\n[tool] {event.get('toolName', '?')} {event.get('input', {})}", flush=True)
        elif event_type == "tool-result":
            print(f"[result] {event.get('toolName', '?')}", flush=True)
        elif event_type == "error":
            print(f"\n[error] {event.get('error', 'unknown error')}", flush=True)
        elif event_type == "finish" and event.get("final", True):
            print("\n[done]", flush=True)
            break


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Gumloop native MCP probe in the sandbox")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--case", choices=sorted(PROMPTS), default="list_files")
    args = parser.parse_args()

    import asyncio

    asyncio.run(run_probe(args.config, args.case))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
