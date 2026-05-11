# Gumloop OMO MCP Sandbox

Isolated prototype area for testing whether Gumloop models can work with an
OhMyOpenCode/OpenCode-style MCP-driven workflow without touching the main
runtime code paths.

Rules:
- Do not import this folder from production code.
- Keep all writable test artifacts inside `fixtures/workspace/` or `outputs/`.
- Treat Gumloop live probes as opt-in.

Suggested flow:
1. Start sandbox MCP server.
2. Run local unit tests.
3. Run Gumloop native MCP probe if credentials are available.
4. Review JSONL transcripts in `outputs/`.

Quick commands:

```bash
python _tmp_gumloop_omo_mcp/server/sandbox_mcp_server.py --port 9877
python -m unittest discover _tmp_gumloop_omo_mcp/tests
python _tmp_gumloop_omo_mcp/harnesses/gumloop_native_mcp_probe.py --config _tmp_gumloop_omo_mcp/sandbox_config.json --case list_files
python _tmp_gumloop_omo_mcp/harnesses/omo_pseudo_loop.py --config _tmp_gumloop_omo_mcp/sandbox_config.json --task read_hello
python _tmp_gumloop_omo_mcp/harnesses/omo_pseudo_loop.py --config _tmp_gumloop_omo_mcp/sandbox_config.json --prompt "List the current workspace root and summarize it"
python _tmp_gumloop_omo_mcp/harnesses/omo_session.py --config _tmp_gumloop_omo_mcp/sandbox_config.json --prompt "Inspect this workspace" --goal "Understand the codebase" --read-only
```

Pseudo-OMO harness notes:
- The harness owns orchestration.
- `gl-*` models are used through the local unified proxy as the reasoning backend per role: planner, explorer, executor, verifier.
- MCP remains the only workspace tool layer.
- This is meant to feel closer to OpenCode/OMO than native Gumloop agent mode.
- Streaming is now emitted for role start/end, content deltas, reasoning deltas when present, tool calls, tool results, and final summary.

Terminal test flow:
1. Start or confirm the local unified proxy is running on `http://127.0.0.1:1430`.
2. Start the MCP server or public MCP tunnel for the workspace you want to inspect/edit.
3. Set `mcp_url` in `sandbox_config.json`.
4. Run one of these:

```bash
python _tmp_gumloop_omo_mcp/harnesses/omo_pseudo_loop.py --config _tmp_gumloop_omo_mcp/sandbox_config.json --task read_hello
python _tmp_gumloop_omo_mcp/harnesses/omo_pseudo_loop.py --config _tmp_gumloop_omo_mcp/sandbox_config.json --prompt "List the current workspace root and summarize the important directories and files for a coding agent."
```

What you should see live:
- `◇ planner`, `◇ explorer #1`, `◇ executor #1`, `◇ verifier`
- streamed JSON role output as it is produced
- `┃ Tool: ...`
- `┃ Result: ...`
- final verification line and transcript path

Budget guidance:
- For broad codebase prompts, start with `--explorer-budget 6` or higher.
- If you see `Explorer exceeded max iterations`, the session now exits cleanly and prints the transcript path instead of a traceback.

Why this pattern matters for Gumloop:
- Yes, effectively this local session-controller pattern is needed because Gumloop is cloud-based.
- Gumloop models can reason in the cloud, but they cannot directly read your local files or call localhost-only tools on their own.
- The local wrapper keeps control of workspace scope, tool safety, budgets, and transcripts while using `gl-*` only as the reasoning backend.
- That is the closest practical way to make it feel like OpenCode/OMO with a cloud model and a local codebase.

Production v2 path note:
- `gl-*` remains the original Gumloop-native path.
- `gl2-*` is intended for the newer wrapper-style path on the normal `http://127.0.0.1:1430/v1` endpoint.
- The old temporary shim has been retired now that the v2 path can live alongside the original path.
