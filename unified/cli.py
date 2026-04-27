"""Unified AI Proxy — CLI commands.

Usage:
    unifiedme start       Start proxy in background
    unifiedme stop        Stop background proxy
    unifiedme run         Start proxy in foreground (interactive)
    unifiedme status      Show proxy status + version
    unifiedme update      Pull latest code + reinstall deps
    unifiedme fix         Check environment, auto-install missing deps
    unifiedme kill-port   Kill process using port 1430
    unifiedme logout      Clear license key (switch license)
    unifiedme help        Show this help
"""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import LISTEN_PORT, DATA_DIR, VERSION, CENTRAL_API_URL

PID_FILE = DATA_DIR / "proxy.pid"
UPTIME_FILE = DATA_DIR / ".uptime"
CMD = "unifiedme"


def _check_for_updates() -> str | None:
    """Check if a newer version is available. Returns latest version or None."""
    try:
        import httpx
        resp = httpx.get(f"{CENTRAL_API_URL}/api/version", timeout=5)
        data = resp.json()
        latest = data.get("version", "")
        if latest and latest != VERSION:
            return latest
    except Exception:
        pass
    return None


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _get_pid() -> int | None:
    if PID_FILE.exists():
        try:
            return int(PID_FILE.read_text().strip())
        except (ValueError, OSError):
            pass
    return None


def _is_running(pid: int) -> bool:
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


def _port_in_use(port: int) -> int | None:
    """Check if port is in use. Returns PID or None."""
    try:
        if _is_windows():
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split("\n"):
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    return int(parts[-1])
        else:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5
            )
            pid_str = result.stdout.strip().split("\n")[0].strip()
            if pid_str:
                return int(pid_str)
    except Exception:
        pass
    return None


def _kill_pid(pid: int) -> bool:
    try:
        if _is_windows():
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            os.kill(pid, signal.SIGKILL)
        return True
    except Exception:
        return False


def _kill_port(port: int) -> bool:
    pid = _port_in_use(port)
    if pid:
        print(f"  Killing PID {pid} on port {port}...")
        _kill_pid(pid)
        time.sleep(1)
        return True
    return False


def _save_uptime():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPTIME_FILE.write_text(str(int(time.time())))


def get_uptime_seconds() -> int:
    if UPTIME_FILE.exists():
        try:
            start = int(UPTIME_FILE.read_text().strip())
            return max(0, int(time.time()) - start)
        except (ValueError, OSError):
            pass
    return 0


def _find_pythonw() -> str | None:
    """Find pythonw.exe (windowless Python) for background mode on Windows."""
    python = sys.executable
    pythonw = python.replace("python.exe", "pythonw.exe")
    if os.path.exists(pythonw):
        return pythonw
    # Check in same directory
    d = os.path.dirname(python)
    pw = os.path.join(d, "pythonw.exe")
    if os.path.exists(pw):
        return pw
    return None


# ─── D1 Sync Helpers ─────────────────────────────────────────────────────────

def _get_timestamp_wib() -> str:
    """Get current timestamp in WIB (UTC+7) format."""
    from datetime import datetime, timezone, timedelta
    wib = timezone(timedelta(hours=7))
    return datetime.now(wib).strftime("%d %b %Y %H:%M:%S WIB")


def _show_d1_status_from_log():
    """Read proxy.log and show D1 sync status if available."""
    log_path = DATA_DIR / "proxy.log"
    if not log_path.exists():
        return
    try:
        # Wait a bit for proxy to finish startup sync
        time.sleep(2)
        lines = log_path.read_text(errors="replace").strip().split("\n")
        # Look for D1 sync lines in last 30 lines
        for line in lines[-30:]:
            if "D1 Synced" in line or "Accounts:" in line and "local DB" not in line:
                # Found sync info — show summary
                _show_d1_box("D1 Synced", "pull")
                return
    except Exception:
        pass


def _push_d1_before_stop():
    """Call the running proxy's API to trigger D1 push before stopping."""
    try:
        import httpx
        # Trigger sync push via admin API
        # Read admin password from data dir
        import sqlite3
        db_path = DATA_DIR / "unified.db"
        if not db_path.exists():
            return

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        pw_row = db.execute("SELECT value FROM settings WHERE key='admin_password'").fetchone()
        admin_pw = pw_row[0] if pw_row else "kUcingku0"

        # Count accounts
        acct_count = db.execute("SELECT COUNT(*) FROM accounts WHERE status='active'").fetchone()[0]
        gl_count = db.execute("SELECT COUNT(*) FROM accounts WHERE gl_status='ok'").fetchone()[0]
        db.close()

        # Try to push via proxy API (fire and forget)
        try:
            resp = httpx.post(
                f"http://localhost:{LISTEN_PORT}/api/sync/push",
                headers={"X-Admin-Password": admin_pw},
                timeout=10,
            )
            push_ok = resp.status_code == 200
        except Exception:
            push_ok = False

        _now = _get_timestamp_wib()
        _device = platform.node() or "unknown"
        _os_info = f"{platform.system()} {platform.release()}"

        GREEN = "\033[0;32m"
        CYAN = "\033[0;36m"
        YELLOW = "\033[1;33m"
        DIM = "\033[2m"
        NC = "\033[0m"

        print()
        print(f"  {CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{NC}")
        if push_ok:
            print(f"  {GREEN}  D1 Updated ✓{NC}")
        else:
            print(f"  {YELLOW}  D1 Push (best effort){NC}")
        print(f"    Accounts: {acct_count} active, {gl_count} GL")
        print(f"    Last update: {_now}")
        print(f"    Device: {_device} ({_os_info})")
        print(f"  {CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{NC}")
        print()

    except Exception:
        pass


def _show_d1_box(title: str, mode: str = "pull"):
    """Show D1 sync status box in terminal."""
    try:
        import sqlite3
        db_path = DATA_DIR / "unified.db"
        if not db_path.exists():
            return

        db = sqlite3.connect(str(db_path))
        acct_count = db.execute("SELECT COUNT(*) FROM accounts WHERE status='active'").fetchone()[0]
        gl_count = db.execute("SELECT COUNT(*) FROM accounts WHERE gl_status='ok'").fetchone()[0]
        db.close()

        _now = _get_timestamp_wib()
        _device = platform.node() or "unknown"
        _os_info = f"{platform.system()} {platform.release()}"

        GREEN = "\033[0;32m"
        CYAN = "\033[0;36m"
        NC = "\033[0m"

        print()
        print(f"  {CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{NC}")
        print(f"  {GREEN}  {title} ✓{NC}")
        print(f"    Accounts: {acct_count} active, {gl_count} GL")
        print(f"    Last sync: {_now}")
        print(f"    Device: {_device} ({_os_info})")
        print(f"  {CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{NC}")
        print()

    except Exception:
        pass


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_start():
    """Start proxy in background."""
    pid = _get_pid()
    if pid and _is_running(pid):
        print(f"  Proxy already running (PID {pid})")
        print(f"  Dashboard: http://localhost:{LISTEN_PORT}/dashboard")
        return

    # Check if port is already in use by something else
    port_pid = _port_in_use(LISTEN_PORT)
    if port_pid:
        print(f"  Port {LISTEN_PORT} is already in use (PID {port_pid}).")
        try:
            answer = input("  Kill it and continue? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            return
        if answer == "y":
            _kill_pid(port_pid)
            time.sleep(1)
            print(f"  Killed PID {port_pid}.")
        else:
            print("  Aborted. Free the port first:")
            print(f"    {CMD} kill-port")
            return

    # License check
    from .main import cli_license_flow
    license_key = cli_license_flow()
    os.environ["LICENSE_KEY"] = license_key

    print("  Starting proxy in background...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    log_file = open(DATA_DIR / "proxy.log", "a")

    if _is_windows():
        # Use pythonw.exe for truly windowless background process
        pythonw = _find_pythonw()
        exe = pythonw or sys.executable
        proc = subprocess.Popen(
            [exe, "-m", "unified.main"],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            env={**os.environ, "LICENSE_KEY": license_key},
            close_fds=True,
        )
    else:
        proc = subprocess.Popen(
            [sys.executable, "-m", "unified.main"],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "LICENSE_KEY": license_key},
            close_fds=True,
        )

    PID_FILE.write_text(str(proc.pid))
    _save_uptime()

    # Detach from child — don't wait
    time.sleep(3)
    if _is_running(proc.pid):
        print(f"  Proxy started (PID {proc.pid})")
        print(f"  Dashboard: http://localhost:{LISTEN_PORT}/dashboard")
        print(f"  Log file:  {DATA_DIR / 'proxy.log'}")
        # Show D1 sync status from log
        _show_d1_status_from_log()
    else:
        print("  Failed to start!")
        # Show last error lines from log
        log_path = DATA_DIR / "proxy.log"
        if log_path.exists():
            lines = log_path.read_text(errors="replace").strip().split("\n")
            error_lines = [l for l in lines[-20:] if "ERROR" in l or "Traceback" in l or "Exception" in l or "Error" in l]
            if error_lines:
                print("  Recent errors:")
                for line in error_lines[-5:]:
                    print(f"    \033[0;31m{line.strip()[:150]}\033[0m")
            else:
                print(f"  Check log: {log_path}")


def cmd_stop():
    """Stop background proxy."""
    pid = _get_pid()
    if not pid:
        print("  No proxy running (no PID file)")
        return

    if not _is_running(pid):
        print(f"  PID {pid} not running (stale PID file, cleaning up)")
        PID_FILE.unlink(missing_ok=True)
        UPTIME_FILE.unlink(missing_ok=True)
        return

    # Push to D1 before stopping
    _push_d1_before_stop()

    print(f"  Stopping proxy (PID {pid})...")
    try:
        if _is_windows():
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
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
    print(f"  Version:   {VERSION}")

    # Check for updates
    latest = _check_for_updates()
    if latest:
        print(f"  Update:    {latest} available! Run: {CMD} update")

    pid = _get_pid()
    if pid and _is_running(pid):
        uptime = get_uptime_seconds()
        h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
        print(f"  Status:    RUNNING")
        print(f"  PID:       {pid}")
        print(f"  Port:      {LISTEN_PORT}")
        print(f"  Uptime:    {h}h {m}m {s}s")
        print(f"  Dashboard: http://localhost:{LISTEN_PORT}/dashboard")
    else:
        print("  Status:    STOPPED")
        if pid:
            print(f"  (stale PID {pid})")
            PID_FILE.unlink(missing_ok=True)


def cmd_run():
    """Start proxy in foreground (interactive)."""
    # Check port first
    port_pid = _port_in_use(LISTEN_PORT)
    if port_pid:
        print(f"  Port {LISTEN_PORT} is already in use (PID {port_pid}).")
        try:
            answer = input("  Kill it and continue? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            return
        if answer == "y":
            _kill_pid(port_pid)
            time.sleep(1)
            print(f"  Killed PID {port_pid}.")
        else:
            print("  Aborted.")
            return

    from .main import main
    _save_uptime()
    main()


def cmd_update():
    """Pull latest code from GitHub and reinstall dependencies."""
    print(f"  Current version: {VERSION}")
    install_dir = Path(__file__).resolve().parent.parent
    venv_dir = install_dir / ".venv"

    # Find venv python/pip
    venv_python = None
    venv_pip = None
    for p in [venv_dir / "Scripts" / "python.exe", venv_dir / "Scripts" / "python", venv_dir / "bin" / "python"]:
        if p.exists():
            venv_python = str(p)
            break
    for p in [venv_dir / "Scripts" / "pip.exe", venv_dir / "Scripts" / "pip", venv_dir / "bin" / "pip"]:
        if p.exists():
            venv_pip = str(p)
            break

    # Check if proxy is running — warn user
    pid = _get_pid()
    was_running = False
    if pid and _is_running(pid):
        was_running = True
        print(f"  Proxy is running (PID {pid}). Stopping for update...")
        cmd_stop()
        time.sleep(1)

    # Git pull
    print("  Pulling latest code...")
    result = subprocess.run(
        ["git", "pull", "--ff-only"],
        capture_output=True, text=True, timeout=30,
        cwd=str(install_dir),
    )
    if result.returncode != 0:
        # Try regular pull
        result = subprocess.run(
            ["git", "pull"],
            capture_output=True, text=True, timeout=30,
            cwd=str(install_dir),
        )

    if result.returncode == 0:
        output = result.stdout.strip()
        if "Already up to date" in output or "Already up-to-date" in output:
            print("  Already up to date.")
        else:
            print(f"  Updated: {output.split(chr(10))[-1]}")
    else:
        print(f"  Git pull failed: {result.stderr.strip()}")
        return

    # Reinstall dependencies
    if venv_pip:
        print("  Installing dependencies...")
        result = subprocess.run(
            [venv_pip, "install", "-r", "requirements.txt"],
            capture_output=True, text=True, timeout=120,
            cwd=str(install_dir),
        )
        if result.returncode == 0:
            print("  Dependencies updated.")
        else:
            last_lines = result.stdout.strip().split("\n")[-3:]
            print("  Dep install output:")
            for line in last_lines:
                print(f"    {line}")
    else:
        print("  Warning: pip not found in venv, skip dependency install.")

    # Read new version
    version_file = install_dir / "VERSION"
    new_version = version_file.read_text().strip() if version_file.exists() else "?"
    if new_version != VERSION:
        print(f"  Updated: v{VERSION} -> v{new_version}")
    else:
        print(f"  Version: v{new_version} (no change)")
    print("  Update complete!")

    # Restart if was running
    if was_running:
        print("  Restarting proxy...")
        cmd_start()


def cmd_fix():
    """Check environment, auto-install missing deps, check for updates."""
    install_dir = Path(__file__).resolve().parent.parent
    venv_dir = install_dir / ".venv"

    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    NC = "\033[0m"
    OK = f"{GREEN}[OK]{NC}"
    FAIL = f"{RED}[MISSING]{NC}"
    WARN = f"{YELLOW}[WARN]{NC}"

    print(f"  Version: {VERSION}")
    print()
    issues = 0

    # 1. Check Python version
    import sys as _sys
    py_ver = f"{_sys.version_info.major}.{_sys.version_info.minor}.{_sys.version_info.micro}"
    if _sys.version_info >= (3, 10):
        print(f"  {OK} Python {py_ver}")
    else:
        print(f"  {FAIL} Python {py_ver} (need >= 3.10)")
        issues += 1

    # 2. Check venv
    if venv_dir.exists():
        print(f"  {OK} Virtual environment")
    else:
        print(f"  {FAIL} Virtual environment (.venv not found)")
        print(f"       Fix: python -m venv .venv")
        issues += 1

    # 3. Find venv python/pip
    venv_python = None
    venv_pip = None
    for p in [venv_dir / "Scripts" / "python.exe", venv_dir / "Scripts" / "python", venv_dir / "bin" / "python"]:
        if p.exists():
            venv_python = str(p)
            break
    for p in [venv_dir / "Scripts" / "pip.exe", venv_dir / "Scripts" / "pip", venv_dir / "bin" / "pip"]:
        if p.exists():
            venv_pip = str(p)
            break

    # 4. Check critical Python packages
    required = ["fastapi", "uvicorn", "httpx", "pydantic", "aiosqlite", "aiohttp", "websockets"]
    missing_pkgs = []
    for pkg in required:
        try:
            result = subprocess.run(
                [venv_python or "python", "-c", f"import {pkg}"],
                capture_output=True, timeout=10,
            )
            if result.returncode == 0:
                print(f"  {OK} {pkg}")
            else:
                print(f"  {FAIL} {pkg}")
                missing_pkgs.append(pkg)
                issues += 1
        except Exception:
            print(f"  {FAIL} {pkg} (check failed)")
            missing_pkgs.append(pkg)
            issues += 1

    # 5. Check license
    from .main import LICENSE_FILE
    if LICENSE_FILE.exists():
        key = LICENSE_FILE.read_text().strip()
        print(f"  {OK} License: {key[:15]}...")
    else:
        env_key = os.environ.get("LICENSE_KEY", "")
        if env_key:
            print(f"  {OK} License: {env_key[:15]}... (env)")
        else:
            print(f"  {WARN} No license key saved (run: {CMD} run)")

    # 6. Check data directory
    data_dir = install_dir / "unified" / "data"
    if data_dir.exists():
        print(f"  {OK} Data directory")
    else:
        print(f"  {WARN} Data directory missing (will be created on first run)")
        data_dir.mkdir(parents=True, exist_ok=True)

    # 7. Check for updates
    print()
    latest = _check_for_updates()
    if latest:
        print(f"  {WARN} Update available: v{VERSION} -> v{latest}")
        try:
            answer = input(f"  Install update now? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer == "y":
            cmd_update()
            return
    else:
        print(f"  {OK} Up to date (v{VERSION})")

    # 8. Auto-install missing packages
    if missing_pkgs and venv_pip:
        print()
        print(f"  {WARN} {len(missing_pkgs)} missing packages. Installing...")
        result = subprocess.run(
            [venv_pip, "install", "-r", "requirements.txt"],
            capture_output=True, text=True, timeout=120,
            cwd=str(install_dir),
        )
        if result.returncode == 0:
            print(f"  {OK} Dependencies installed")
            issues -= len(missing_pkgs)
        else:
            print(f"  {FAIL} Install failed:")
            for line in result.stderr.strip().split("\n")[-3:]:
                print(f"       {line}")

    # 9. Show recent error logs
    log_file = data_dir / "proxy.log"
    if log_file.exists():
        log_content = log_file.read_text(errors="replace")
        error_lines = [l for l in log_content.split("\n") if "ERROR" in l or "Traceback" in l or "Exception" in l]
        if error_lines:
            print()
            print(f"  {WARN} Recent errors in proxy.log:")
            for line in error_lines[-5:]:
                print(f"    {RED}{line.strip()[:120]}{NC}")

    print()
    if issues == 0:
        print(f"  {GREEN}All checks passed!{NC}")
    else:
        print(f"  {RED}{issues} issue(s) found.{NC}")


def cmd_logout():
    """Clear saved license key. Next run will prompt for new one."""
    from .main import LICENSE_FILE
    if LICENSE_FILE.exists():
        LICENSE_FILE.unlink()
        print(f"  License cleared. Run '{CMD} run' to enter a new license key.")
    else:
        print("  No saved license found.")


def cli_main():
    """CLI entry point."""
    args = sys.argv[1:]
    cmd = args[0] if args else "run"

    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "run": cmd_run,
        "status": cmd_status,
        "update": cmd_update,
        "fix": cmd_fix,
        "kill-port": cmd_kill_port,
        "logout": cmd_logout,
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
