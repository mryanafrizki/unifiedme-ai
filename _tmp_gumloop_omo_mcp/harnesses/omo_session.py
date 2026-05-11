from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from _tmp_gumloop_omo_mcp.harnesses.omo_pseudo_loop import run_loop


def main() -> int:
    parser = argparse.ArgumentParser(description="Sandbox opencode-like session wrapper")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--prompt", default="")
    parser.add_argument("--goal", default="")
    parser.add_argument("--task", choices=["read_hello", "edit_sample_app"], default="read_hello")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--planner-budget", type=int, default=1)
    parser.add_argument("--explorer-budget", type=int, default=4)
    parser.add_argument("--verifier-budget", type=int, default=1)
    parser.add_argument("--stream", choices=["pretty", "silent"], default="pretty")
    args = parser.parse_args()

    allow_writes = bool(args.write and not args.read_only)
    try:
        result = asyncio.run(
            run_loop(
                args.config,
                args.task,
                args.explorer_budget,
                custom_prompt=args.prompt,
                workspace_goal=args.goal,
                allow_writes=allow_writes,
                stream_mode=args.stream,
                planner_budget=args.planner_budget,
                verifier_budget=args.verifier_budget,
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"\n[session-error] {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
