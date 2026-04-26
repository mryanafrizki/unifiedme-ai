"""Unified AI Proxy — CLI commands.

Usage:
    unified start       Start proxy in background
    unified stop        Stop background proxy
    unified run         Start proxy in foreground (interactive)
    unified kill-port   Kill process using port 1430
    unified status      Show proxy status
"""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import LISTEN_PORT, DATA_DIR

PID_FILE = DATA_DIR / "proxy.pid"
UPTIME_FILE = DATA_DIR / ".uptime"


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _get_pid() -> int | None:
    """Read PID from file."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            return pid
        except (ValueError, OSError):
            pass
    return None


def _is_running(pid: int) -> bool:
    """Check if a process with given PID is running."""
    try:
        if _is_windows():
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=5
            )
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _kill_port(port: int) -> bool:
    """Kill process using a specific port."""
    try:
        if _is_windows():
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split("\n"):
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    pid = int(parts[-1])
                    print(f"  Killing PID {pid} on port {port}...")
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], timeout=5)
                    return True
        else:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5
            )
            pids = result.stdout.strip().split("\n")
            for pid_str in pids:
                if pid_str.strip():
                    pid = int(pid_str.strip())
                    print(f"  Killing PID {pid} on port {port}...")
                    os.kill(pid, signal.SIGKILL)
                    return True
    except Exception as e:
        print(f"  Error: {e}")
    return False


def _save_uptime():
    """Save startup timestamp."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPTIME_FILE.write_text(str(int(time.time())))


def get_uptime_seconds() -> int:
    """Get proxy uptime in seconds. Returns 0 if not running."""
    if UPTIME_FILE.exists():
        try:
            start = int(UPTIME_FILE.read_text().strip())
            return max(0, int(time.time()) - start)
        except (ValueError, OSError):
            pass
    return 0


def cmd_start():
    """Start proxy in background."""
    pid = _get_pid()
    if pid and _is_running(pid):
        print(f"  Proxy already running (PID {pid})")
        return

    # Import and run license check first
    from .main import cli_license_flow
    license_key = cli_license_flow()
    os.environ["LICENSE_KEY"] = license_key

    print("  Starting proxy in background...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    python = sys.executable
    if _is_windows():
        # Windows: use pythonw or START /B
        proc = subprocess.Popen(
            [python, "-m", "unified.main"],
            stdout=open(DATA_DIR / "proxy.log", "a"),
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            env={**os.environ, "LICENSE_KEY": license_key},
        )
    else:
        proc = subprocess.Popen(
            [python, "-m", "unified.main"],
            stdout=open(DATA_DIR / "proxy.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "LICENSE_KEY": license_key},
        )

    PID_FILE.write_text(str(proc.pid))
    _save_uptime()

    # Wait a moment and check if it started
    time.sleep(2)
    if _is_running(proc.pid):
        print(f"  Proxy started (PID {proc.pid})")
        print(f"  Dashboard:  http://localhost:{LISTEN_PORT}/dashboard")
        print(f"  Log file:   {DATA_DIR / 'proxy.log'}")
    else:
        print("  Failed to start. Check log:")
        print(f"  {DATA_DIR / 'proxy.log'}")


def cmd_stop():
    """Stop background proxy."""
    pid = _get_pid()
    if not pid:
        print("  No proxy running (no PID file)")
        return

    if not _is_running(pid):
        print(f"  PID {pid} not running (stale PID file)")
        PID_FILE.unlink(missing_ok=True)
        UPTIME_FILE.unlink(missing_ok=True)
        return

    print(f"  Stopping proxy (PID {pid})...")
    try:
        if _is_windows():
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
            # Wait for graceful shutdown
            for _ in range(10):
                time.sleep(1)
                if not _is_running(pid):
                    break
            else:
                os.kill(pid, signal.SIGKILL)
    except Exception as e:
        print(f"  Error: {e}")

    PID_FILE.unlink(missing_ok=True)
    UPTIME_FILE.unlink(missing_ok=True)
    print("  Proxy stopped.")


def cmd_kill_port():
    """Kill process using port 1430."""
    print(f"  Checking port {LISTEN_PORT}...")
    if _kill_port(LISTEN_PORT):
        print(f"  Port {LISTEN_PORT} freed.")
        PID_FILE.unlink(missing_ok=True)
        UPTIME_FILE.unlink(missing_ok=True)
    else:
        print(f"  Port {LISTEN_PORT} is not in use.")


def cmd_status():
    """Show proxy status."""
    pid = _get_pid()
    if pid and _is_running(pid):
        uptime = get_uptime_seconds()
        h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
        print(f"  Status:   RUNNING")
        print(f"  PID:      {pid}")
        print(f"  Port:     {LISTEN_PORT}")
        print(f"  Uptime:   {h}h {m}m {s}s")
        print(f"  Dashboard: http://localhost:{LISTEN_PORT}/dashboard")
    else:
        print("  Status:   STOPPED")
        if pid:
            print(f"  (stale PID {pid})")
            PID_FILE.unlink(missing_ok=True)


def cmd_run():
    """Start proxy in foreground (interactive)."""
    from .main import main
    _save_uptime()
    main()


def cli_main():
    """CLI entry point."""
    args = sys.argv[1:]
    cmd = args[0] if args else "run"

    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "run": cmd_run,
        "kill-port": cmd_kill_port,
        "status": cmd_status,
    }

    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return

    if cmd not in commands:
        print(f"  Unknown command: {cmd}")
        print(f"  Available: {', '.join(commands.keys())}")
        sys.exit(1)

    commands[cmd]()


if __name__ == "__main__":
    cli_main()
