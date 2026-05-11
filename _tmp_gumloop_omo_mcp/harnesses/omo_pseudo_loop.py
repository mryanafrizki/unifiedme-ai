from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "_tmp_gumloop_omo_mcp"
sys.path.insert(0, str(ROOT))

from _tmp_gumloop_omo_mcp.wrappers.mcp_client_wrapper import SandboxMCPClient
from _tmp_gumloop_omo_mcp.wrappers.proxy_role_reasoner import ProxyGLRoleReasoner
from _tmp_gumloop_omo_mcp.wrappers.role_reasoner import extract_json_object
from _tmp_gumloop_omo_mcp.wrappers.run_recorder import RunRecorder
from _tmp_gumloop_omo_mcp.wrappers.tool_guard import ToolGuard


def _friendly_error_message(message: str) -> str:
    if message == "Explorer exceeded max iterations":
        return message + ". Try a narrower prompt or increase --max-iterations / --explorer-budget."
    if message == "Executor exceeded max iterations":
        return message + ". Try enabling a narrower write task or increase the executor budget."
    return message


def load_prompt(name: str) -> str:
    return (SANDBOX / "fixtures" / "role_prompts" / f"{name}.txt").read_text(encoding="utf-8")


def load_task(task_name: str) -> str:
    return (SANDBOX / "fixtures" / "tasks" / f"{task_name}.txt").read_text(encoding="utf-8").strip()


def summarize_result(result: Any) -> Any:
    if isinstance(result, dict):
        if "text" in result and isinstance(result["text"], str):
            text = result["text"]
            return {"text": text[:1200] + ("..." if len(text) > 1200 else "")}
        if "entries" in result and isinstance(result["entries"], list):
            return {
                "path": result.get("path", ""),
                "count": result.get("count", len(result["entries"])),
                "entries_preview": result["entries"][:25],
            }
        if "matches" in result and isinstance(result["matches"], list):
            return {
                "count": result.get("count", len(result["matches"])),
                "matches_preview": result["matches"][:10],
            }
    return result


def _print_role_banner(role: str, iteration: int | None = None, stream_mode: str = "pretty") -> None:
    if stream_mode == "silent":
        return
    suffix = f" #{iteration}" if iteration is not None else ""
    print(f"\n◇ {role}{suffix}", flush=True)


def _print_role_done(role: str, status: str, stream_mode: str = "pretty") -> None:
    if stream_mode == "silent":
        return
    print(f"\n◇ {role} done ({status})", flush=True)


async def run_streamed_role(
    recorder: RunRecorder,
    reasoner: ProxyGLRoleReasoner,
    role: str,
    system_prompt: str,
    context: dict[str, Any],
    iteration: int | None = None,
    stream_mode: str = "pretty",
) -> tuple[dict[str, Any], str]:
    _print_role_banner(role, iteration, stream_mode)
    recorder.role_start(role, getattr(reasoner, "model", "scripted-role-reasoner"), iteration)
    started = time.time()
    pieces: list[str] = []
    parsed: dict[str, Any] | None = None

    async for event in reasoner.stream_role(role, system_prompt, context):
        if event["type"] == "reasoning":
            delta = event["delta"]
            recorder.reasoning_delta(role, delta, iteration)
            if stream_mode == "pretty":
                print(delta, end="", flush=True)
        elif event["type"] == "content":
            delta = event["delta"]
            recorder.message_delta(role, delta, iteration)
            pieces.append(delta)
            if stream_mode == "pretty":
                print(delta, end="", flush=True)
        elif event["type"] == "finish" and event.get("parsed"):
            parsed = event["parsed"]

    raw = "".join(pieces)
    if parsed is None:
        parsed = extract_json_object(raw)
    duration_ms = int((time.time() - started) * 1000)
    recorder.role_end(role, parsed.get("status", "unknown"), duration_ms, iteration)
    _print_role_done(role, parsed.get("status", "unknown"), stream_mode)
    return parsed, raw


async def run_loop(
    config_path: Path,
    task_name: str | None,
    max_iterations: int,
    custom_prompt: str = "",
    workspace_goal: str = "",
    allow_writes: bool = False,
    stream_mode: str = "pretty",
    planner_budget: int = 1,
    verifier_budget: int = 1,
) -> dict[str, Any]:
    reasoner = ProxyGLRoleReasoner(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    mcp_url = config.get("mcp_url", "")
    if not mcp_url:
        raise RuntimeError("sandbox_config.json must include mcp_url for the pseudo loop")

    mcp = SandboxMCPClient(mcp_url)
    guard = ToolGuard(allow_executor_writes=allow_writes)
    run_label = task_name or "custom"
    output = SANDBOX / "outputs" / "omo_runs" / f"run_{run_label}_{int(time.time())}_{uuid.uuid4().hex[:8]}.jsonl"
    recorder = RunRecorder(output)

    user_task = custom_prompt.strip() if custom_prompt.strip() else load_task(task_name or "read_hello")
    planner_prompt = load_prompt("planner")
    explorer_prompt = load_prompt("explorer")
    executor_prompt = load_prompt("executor")
    verifier_prompt = load_prompt("verifier")

    await mcp.connect()
    try:
        tools = await mcp.list_tools()
        tool_names = [tool.get("name", "?") for tool in tools]
        recorder.record(
            "session_start",
            {
                "task": task_name or "custom",
                "prompt": user_task,
                "workspace_goal": workspace_goal,
                "mcp_url": mcp_url,
                "tool_count": len(tool_names),
                "write_enabled": allow_writes,
                "role_budgets": {
                    "planner": planner_budget,
                    "explorer": max_iterations,
                    "executor": max_iterations,
                    "verifier": verifier_budget,
                },
                "stream_mode": stream_mode,
                "fixed_workspace": str(SANDBOX / "fixtures" / "workspace"),
            },
        )

        plan_context = {
            "task": user_task,
            "workspace_goal": workspace_goal,
            "available_tools": tool_names,
            "workspace_rule": "Use only relative paths in the MCP workspace",
            "write_enabled": allow_writes,
        }
        plan, raw_plan = await run_streamed_role(recorder, reasoner, "planner", planner_prompt, plan_context, stream_mode=stream_mode)
        recorder.record("planner", {"context": plan_context, "raw": raw_plan, "plan": plan})

        exploration_facts: list[str] = []
        explorer_history: list[dict[str, Any]] = []
        last_explorer_observation: dict[str, Any] | None = None
        for iteration in range(1, max_iterations + 1):
            context = {
                "task": user_task,
                "workspace_goal": workspace_goal,
                "plan": plan,
                "known_facts": exploration_facts,
                "tool_history": explorer_history,
                "last_observation": last_explorer_observation,
                "iteration": iteration,
            }
            action, raw = await run_streamed_role(recorder, reasoner, "explorer", explorer_prompt, context, iteration, stream_mode)
            recorder.record("explorer", {"iteration": iteration, "raw": raw, "action": action})

            if action.get("status") == "done":
                exploration_facts.extend(action.get("facts", []))
                break

            if action.get("status") != "needs_tool":
                raise RuntimeError(f"Unexpected explorer status: {action.get('status')}")

            tool_name = action.get("tool", "")
            arguments = action.get("arguments", {})
            ok, message = guard.validate("explorer", tool_name, arguments)
            if not ok:
                recorder.record("error", {"role": "explorer", "iteration": iteration, "error": message})
                raise RuntimeError(message)
            recorder.tool_call_start("explorer", tool_name, arguments, iteration)
            if stream_mode == "pretty":
                print(f"┃ Tool: {tool_name} {json.dumps(arguments, ensure_ascii=False)}", flush=True)
            result = await mcp.call_tool(tool_name, arguments)
            recorder.record("explorer_tool", {"iteration": iteration, "tool": tool_name, "arguments": arguments, "result": result})
            result_summary = summarize_result(result)
            recorder.tool_result("explorer", tool_name, result_summary, iteration)
            if stream_mode == "pretty":
                print(f"┃ Result: {tool_name}", flush=True)
                print(json.dumps(result_summary, ensure_ascii=False, indent=2), flush=True)
            exploration_facts.append(json.dumps({"tool": tool_name, "result": result}, ensure_ascii=False))
            last_explorer_observation = {"tool": tool_name, "arguments": arguments, "result": summarize_result(result)}
            explorer_history.append(last_explorer_observation)
        else:
            recorder.record("error", {"role": "explorer", "error": "Explorer exceeded max iterations"})
            raise RuntimeError("Explorer exceeded max iterations")

        execution_notes: list[str] = []
        executor_history: list[dict[str, Any]] = []
        last_executor_observation: dict[str, Any] | None = None
        changed_files: list[str] = []
        if not allow_writes:
            recorder.record("executor_skipped", {"reason": "read-only session", "iteration": None})
        else:
            for iteration in range(1, max_iterations + 1):
                context = {
                    "task": user_task,
                    "workspace_goal": workspace_goal,
                    "plan": plan,
                    "known_facts": exploration_facts,
                    "execution_notes": execution_notes,
                "tool_history": executor_history,
                "last_observation": last_executor_observation,
                "iteration": iteration,
            }
                action, raw = await run_streamed_role(recorder, reasoner, "executor", executor_prompt, context, iteration, stream_mode)
                recorder.record("executor", {"iteration": iteration, "raw": raw, "action": action})

                if action.get("status") == "done":
                    changed_files.extend(action.get("changed_files", []))
                    break
                if action.get("status") == "blocked":
                    recorder.record("error", {"role": "executor", "iteration": iteration, "error": action.get("summary", "Executor blocked")})
                    raise RuntimeError(action.get("summary", "Executor blocked"))
                if action.get("status") != "needs_tool":
                    recorder.record("error", {"role": "executor", "iteration": iteration, "error": f"Unexpected executor status: {action.get('status')}"})
                    raise RuntimeError(f"Unexpected executor status: {action.get('status')}")

                tool_name = action.get("tool", "")
                arguments = action.get("arguments", {})
                ok, message = guard.validate("executor", tool_name, arguments)
                if not ok:
                    recorder.record("error", {"role": "executor", "iteration": iteration, "error": message})
                    raise RuntimeError(message)
                recorder.tool_call_start("executor", tool_name, arguments, iteration)
                if stream_mode == "pretty":
                    print(f"┃ Tool: {tool_name} {json.dumps(arguments, ensure_ascii=False)}", flush=True)
                result = await mcp.call_tool(tool_name, arguments)
                recorder.record("executor_tool", {"iteration": iteration, "tool": tool_name, "arguments": arguments, "result": result})
                result_summary = summarize_result(result)
                recorder.tool_result("executor", tool_name, result_summary, iteration)
                if stream_mode == "pretty":
                    print(f"┃ Result: {tool_name}", flush=True)
                    print(json.dumps(result_summary, ensure_ascii=False, indent=2), flush=True)
                execution_notes.append(json.dumps({"tool": tool_name, "result": result}, ensure_ascii=False))
                last_executor_observation = {"tool": tool_name, "arguments": arguments, "result": summarize_result(result)}
                executor_history.append(last_executor_observation)
            else:
                recorder.record("error", {"role": "executor", "error": "Executor exceeded max iterations"})
                raise RuntimeError("Executor exceeded max iterations")

        verify_context = {
            "task": user_task,
            "plan": plan,
            "known_facts": exploration_facts,
            "execution_notes": execution_notes,
            "explorer_tool_history": explorer_history,
            "executor_tool_history": executor_history,
            "changed_files": changed_files,
        }
        verification, raw_verification = await run_streamed_role(recorder, reasoner, "verifier", verifier_prompt, verify_context, stream_mode=stream_mode)
        recorder.record("verifier", {"raw": raw_verification, "verification": verification})
        recorder.final_summary(verification, changed_files)
        recorder.record("session_end", {"changed_files": changed_files})
        if stream_mode == "pretty":
            print(f"✓ {verification.get('summary', 'Done')}", flush=True)
            print(f"Transcript: {output}", flush=True)

        return {
            "output_path": str(output),
            "plan": plan,
            "exploration_facts": exploration_facts,
            "execution_notes": execution_notes,
            "verification": verification,
            "changed_files": changed_files,
        }
    except Exception as exc:
        friendly = _friendly_error_message(str(exc))
        recorder.record("error", {"role": "system", "error": friendly})
        raise RuntimeError(f"{friendly}\nTranscript: {output}") from exc
    finally:
        await mcp.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pseudo OpenCode/OMO loop with Gumloop backend")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--task", choices=["read_hello", "edit_sample_app"], default="read_hello")
    parser.add_argument("--prompt", default="", help="Custom user prompt. Overrides --task when provided.")
    parser.add_argument("--goal", default="", help="High-level workspace goal for the session.")
    parser.add_argument("--max-iterations", type=int, default=4)
    parser.add_argument("--write", action="store_true", help="Enable executor write-capable tools.")
    parser.add_argument("--read-only", action="store_true", help="Force read-only session mode.")
    parser.add_argument("--planner-budget", type=int, default=1)
    parser.add_argument("--verifier-budget", type=int, default=1)
    parser.add_argument("--stream", choices=["pretty", "silent"], default="pretty")
    args = parser.parse_args()

    allow_writes = bool(args.write and not args.read_only)
    try:
        result = asyncio.run(
            run_loop(
                args.config,
                args.task,
                args.max_iterations,
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
