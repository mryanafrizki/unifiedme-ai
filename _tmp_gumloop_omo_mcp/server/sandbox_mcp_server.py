from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / "_tmp_gumloop_omo_mcp" / "fixtures" / "workspace"
MCP_SERVER = ROOT / "mcp_server.py"


def main() -> int:
    parser = argparse.ArgumentParser(description="Sandbox launcher for MCP server")
    parser.add_argument("--port", type=int, default=9877)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--no-tunnel", action="store_true")
    args = parser.parse_args()

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(MCP_SERVER),
        "--workspace",
        str(WORKSPACE),
        "--port",
        str(args.port),
        "--no-interactive",
    ]
    if args.api_key:
        command.extend(["--api-key", args.api_key])
    if args.no_tunnel:
        command.append("--no-tunnel")

    return subprocess.call(command, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
