"""Unified AI Proxy — CLI commands.

Usage:
    unifiedme start                Start proxy in background
    unifiedme stop                 Stop background proxy
    unifiedme run                  Start proxy in foreground (interactive)
    unifiedme status               Show proxy status + version
    unifiedme update               Pull latest code + reinstall deps
    unifiedme fix                  Check environment, auto-install missing deps
    unifiedme kill-port            Kill process using port 1430
    unifiedme logout               Clear license key (switch license)
    unifiedme addaccounts add      Batch add accounts (interactive)
    unifiedme addaccounts status   Show batch progress (real-time)
    unifiedme addaccounts stop     Force stop running batch
    unifiedme mcp start [folder]   Start MCP server (background daemon)
    unifiedme mcp stop             Stop MCP server
    unifiedme mcp status           Show MCP server status
    unifiedme mcp list             List MCP servers for all GL accounts
    unifiedme mcp toggle           Enable/disable MCP on an account
    unifiedme mcp bind <url>       Bind MCP URL to all GL accounts (or --account N)
    unifiedme tunnel status        Show tunnel status
    unifiedme tunnel start         Start cloudflared tunnel (proxy or mcp)
    unifiedme tunnel stop          Stop cloudflared tunnel
    unifiedme tunnel install       Install cloudflared + nginx
    unifiedme vps list             List registered VPS servers
    unifiedme vps add              Add a VPS server (interactive)
    unifiedme vps install <id>     Auto-install on a VPS
    unifiedme help                 Show this help
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


def _get_account_stats() -> dict:
    """Read account stats from local DB."""
    import sqlite3
    db_path = DATA_DIR / "unified.db"
    if not db_path.exists():
        return {}
    try:
        db = sqlite3.connect(str(db_path))
        total = db.execute("SELECT COUNT(*) FROM accounts WHERE status='active'").fetchone()[0]
        kr = db.execute("SELECT COUNT(*) FROM accounts WHERE kiro_status='ok'").fetchone()[0]
        cb = db.execute("SELECT COUNT(*) FROM accounts WHERE cb_status='ok'").fetchone()[0]
        ws = db.execute("SELECT COUNT(*) FROM accounts WHERE ws_status='ok'").fetchone()[0]
        gl = db.execute("SELECT COUNT(*) FROM accounts WHERE gl_status='ok'").fetchone()[0]
        db.close()
        return {"total": total, "kr": kr, "cb": cb, "ws": ws, "gl": gl}
    except Exception:
        return {}


def _print_d1_box(title: str, push_ok: bool = True):
    """Print D1 sync status box with account breakdown."""
    stats = _get_account_stats()
    if not stats:
        return

    _now = _get_timestamp_wib()
    _device = platform.node() or "unknown"
    _os_info = f"{platform.system()} {platform.release()}"

    GREEN = "\033[0;32m"
    CYAN = "\033[0;36m"
    YELLOW = "\033[1;33m"
    DIM = "\033[2m"
    WHITE = "\033[1;37m"
    NC = "\033[0m"

    status = f"{GREEN}{title}{NC}" if push_ok else f"{YELLOW}{title}{NC}"

    print()
    print(f"  {CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{NC}")
    print(f"    {status}")
    print(f"    {WHITE}KR:{NC} {stats['kr']}  {WHITE}CB:{NC} {stats['cb']}  {WHITE}WS:{NC} {stats['ws']}  {WHITE}GL:{NC} {stats['gl']}  {DIM}Total: {stats['total']}{NC}")
    print(f"    {DIM}{_now} · {_device} ({_os_info}){NC}")
    print(f"    {DIM}D1 = pusat · Heartbeat: 2min · Push: instant per-account{NC}")
    print(f"  {CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{NC}")
    print()


def _push_d1_before_stop():
    """Push to D1 before stopping and show status."""
    try:
        import httpx
        # Try to push via proxy API
        import sqlite3
        db_path = DATA_DIR / "unified.db"
        if not db_path.exists():
            return

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        pw_row = db.execute("SELECT value FROM settings WHERE key='admin_password'").fetchone()
        admin_pw = pw_row[0] if pw_row else "kUcingku0"
        db.close()

        push_ok = False
        try:
            resp = httpx.post(
                f"http://localhost:{LISTEN_PORT}/api/sync/push",
                headers={"X-Admin-Password": admin_pw},
                timeout=10,
            )
            push_ok = resp.status_code == 200
        except Exception:
            pass

        _print_d1_box("D1 Updated", push_ok)

    except Exception:
        pass


def _show_d1_status_from_log():
    """Show D1 sync status after proxy starts."""
    GREEN = "\033[0;32m"
    CYAN = "\033[0;36m"
    DIM = "\033[2m"
    NC = "\033[0m"

    # Wait for proxy to finish startup sync
    print(f"  {DIM}Syncing with D1...{NC}", end="", flush=True)
    for i in range(15):
        time.sleep(1)
        log_path = DATA_DIR / "proxy.log"
        if log_path.exists():
            lines = log_path.read_text(errors="replace").strip().split("\n")
            # Look for sync completion in last 20 lines
            for line in lines[-20:]:
                if "D1 Synced" in line or "Sync push:" in line:
                    print(f"\r  {GREEN}D1 sync complete{NC}     ")
                    _print_d1_box("D1 Synced")
                    return
                if "D1 startup sync failed" in line or "Sync push failed" in line:
                    print(f"\r  {CYAN}D1 sync failed (using local data){NC}")
                    _print_d1_box("D1 Local Only")
                    return
    # Timeout — show whatever we have
    print(f"\r  {DIM}D1 sync in progress...{NC}")
    _print_d1_box("D1 Synced")


# ─── Camoufox System Dependencies ────────────────────────────────────────────

# Required system libraries for Camoufox/Firefox headless on Linux
_CAMOUFOX_SYSTEM_DEPS = [
    "libgtk-3-0", "libdbus-glib-1-2", "libasound2", "libx11-xcb1",
    "libxcomposite1", "libxdamage1", "libxrandr2", "libgbm1",
    "libpango-1.0-0", "libcairo2", "libatk1.0-0", "libatk-bridge2.0-0",
    "libxkbcommon0", "libxfixes3", "libcups2", "libnspr4", "libnss3",
    "fonts-liberation", "xvfb",
]


def check_camoufox_deps() -> tuple[list[str], list[str]]:
    """Check which Camoufox system deps are installed (Linux only).

    Returns (installed, missing) lists.
    """
    if platform.system() != "Linux":
        return [], []

    installed = []
    missing = []
    for pkg in _CAMOUFOX_SYSTEM_DEPS:
        result = subprocess.run(
            ["dpkg", "-s", pkg],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and "Status: install ok installed" in result.stdout:
            installed.append(pkg)
        else:
            missing.append(pkg)
    return installed, missing


def install_camoufox_deps(missing: list[str]) -> bool:
    """Install missing system deps via apt. Returns True on success."""
    if not missing:
        return True
    print(f"  Installing {len(missing)} system packages...")
    print(f"  {' '.join(missing)}")
    result = subprocess.run(
        ["sudo", "apt", "install", "-y"] + missing,
        timeout=120,
    )
    return result.returncode == 0


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

    # Show current stats before stopping
    _print_d1_box("Stopping")

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

    # 4b. Check Camoufox system dependencies (Linux only)
    if platform.system() == "Linux":
        cam_installed, cam_missing = check_camoufox_deps()
        if cam_missing:
            print(f"  {FAIL} Camoufox deps: {len(cam_missing)} missing")
            for pkg in cam_missing:
                print(f"       - {pkg}")
            issues += 1
            try:
                answer = input("  Install missing system packages now? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer != "n":
                if install_camoufox_deps(cam_missing):
                    print(f"  {OK} System packages installed")
                    issues -= 1
                else:
                    print(f"  {FAIL} Some packages failed to install")
        else:
            print(f"  {OK} Camoufox system deps ({len(cam_installed)} packages)")

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


# ─── Add Accounts CLI ────────────────────────────────────────────────────────

_AA_LOG = DATA_DIR / "addaccounts.log"
_AA_FAIL = DATA_DIR / "addaccounts_failed.log"

# ANSI colors
_GREEN = "\033[0;32m"
_RED = "\033[0;31m"
_CYAN = "\033[0;36m"
_YELLOW = "\033[1;33m"
_DIM = "\033[2m"
_WHITE = "\033[1;37m"
_NC = "\033[0m"


def _aa_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _aa_log(msg: str, also_print: bool = True):
    line = f"[{_aa_ts()}] {msg}"
    with open(_AA_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if also_print:
        print(line)


def _aa_log_fail(email: str, password: str, reason: str):
    with open(_AA_FAIL, "a", encoding="utf-8") as f:
        f.write(f"{email}:{password} | {reason}\n")


def _aa_api(method: str, path: str, json_body=None, timeout: float = 30) -> dict:
    import httpx
    from .config import ADMIN_PASSWORD
    headers = {"X-Admin-Password": ADMIN_PASSWORD, "Content-Type": "application/json"}
    url = f"http://localhost:{LISTEN_PORT}/api{path}"
    with httpx.Client(timeout=timeout) as client:
        if method == "GET":
            resp = client.get(url, headers=headers)
        else:
            resp = client.post(url, headers=headers, json=json_body or {})
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()


def _aa_prompt(question: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    raw = input(f"  {question}{hint}: ").strip()
    return raw or default


def _aa_yn(question: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"  {question} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _aa_check_server() -> bool:
    """Check proxy is running and return True."""
    pid = _get_pid()
    if not pid or not _is_running(pid):
        return False
    try:
        _aa_api("GET", "/accounts", timeout=5)
        return True
    except Exception:
        return False


def _aa_get_license_info() -> dict | None:
    """Get license info from saved file + validate."""
    from .main import LICENSE_FILE, _validate_license_sync
    if not LICENSE_FILE.exists():
        return None
    key = LICENSE_FILE.read_text().strip()
    if not key:
        return None
    result = _validate_license_sync(key)
    if result.get("ok"):
        lic = result.get("license", {})
        return {
            "key": key,
            "owner": lic.get("owner_name", "?"),
            "tier": lic.get("tier", "?"),
            "max_devices": lic.get("max_devices", "?"),
            "max_accounts": lic.get("max_accounts", "?"),
            "device_id": result.get("device_id", "?"),
        }
    return None


def _aa_load_file(filepath: str, label: str) -> list[str]:
    """Load lines from a .txt file."""
    p = Path(filepath)
    if not p.exists():
        print(f"  {_RED}[ERROR]{_NC} File not found: {filepath}")
        return []
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def _aa_check_proxies(proxies: list[str]) -> list[dict]:
    """Quick connectivity check."""
    import httpx
    results = []
    for p in proxies:
        try:
            with httpx.Client(proxy=p, timeout=10) as client:
                resp = client.get("https://httpbin.org/ip")
                if resp.status_code == 200:
                    ip = resp.json().get("origin", "?")
                    results.append({"url": p, "ok": True, "ip": ip})
                else:
                    results.append({"url": p, "ok": False, "ip": f"HTTP {resp.status_code}"})
        except Exception as e:
            results.append({"url": p, "ok": False, "ip": str(e)[:40]})
    return results


def _aa_stream_progress(account_map: dict[str, str]):
    """Listen to SSE events and print real-time progress."""
    import httpx
    import json
    from .config import ADMIN_PASSWORD

    url = f"http://localhost:{LISTEN_PORT}/api/events?token={ADMIN_PASSWORD}"
    done_count = 0
    total = len(account_map)

    try:
        with httpx.Client(timeout=None) as client:
            with client.stream("GET", url) as resp:
                for line in resp.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                    except (json.JSONDecodeError, ValueError):
                        continue

                    etype = data.get("type", "")

                    if etype == "batch_start":
                        _aa_log(f"Batch started: {data.get('total', '?')} jobs, concurrency={data.get('concurrency', 1)}")

                    elif etype == "batch_skipped":
                        _aa_log(f"Skipped {data.get('count', 0)} accounts (already OK)")

                    elif etype == "job_start":
                        email = data.get("email", "?")
                        proxy = data.get("proxy_used", "") or "direct"
                        if "@" in proxy:
                            proxy = proxy.split("@")[-1]
                        idx = data.get("index", 0) + 1
                        _aa_log(f"  {_CYAN}[{idx}/{total}]{_NC} {email} {_DIM}(proxy: {proxy}){_NC}")

                    elif etype == "job_log":
                        provider = data.get("provider", "")
                        step = data.get("step", "")
                        msg = data.get("message", "")
                        _aa_log(f"    {_DIM}[{provider}:{step}]{_NC} {msg}")

                    elif etype == "provider_done":
                        email = data.get("email", "?")
                        provider = data.get("provider", "")
                        ok = data.get("success", False)
                        color = _GREEN if ok else _RED
                        status = "OK" if ok else "FAIL"
                        _aa_log(f"    {color}[{provider}] {status}{_NC}")

                    elif etype == "import_ok":
                        provider = data.get("provider", "")
                        _aa_log(f"    {_GREEN}[{provider}] imported{_NC}")

                    elif etype == "import_error":
                        email = data.get("email", "?")
                        provider = data.get("provider", "")
                        error = data.get("error", "unknown")
                        _aa_log(f"    {_RED}[{provider}] import FAILED: {error}{_NC}")
                        pw = account_map.get(email, "?")
                        _aa_log_fail(email, pw, f"{provider}: {error}")

                    elif etype == "job_done":
                        email = data.get("email", "?")
                        status = data.get("status", "?")
                        done_count += 1
                        color = _GREEN if status == "success" else _RED
                        _aa_log(f"  {color}[{done_count}/{total}] {email} — {status.upper()}{_NC}")
                        if status == "failed":
                            errors = data.get("errors", {})
                            for prov, err in errors.items():
                                pw = account_map.get(email, "?")
                                _aa_log_fail(email, pw, f"{prov}: {err}")

                    elif etype == "job_cancelled":
                        email = data.get("email", "?")
                        done_count += 1
                        _aa_log(f"  {_YELLOW}[{done_count}/{total}] {email} — CANCELLED{_NC}")

                    elif etype == "batch_auto_stop":
                        reason = data.get("reason", "Auto-stopped")
                        _aa_log(f"\n  {_RED}[AUTO-STOP] {reason}{_NC}")

                    elif etype == "batch_done":
                        ok = data.get("success", 0)
                        fail = data.get("failed", 0)
                        cancelled = data.get("cancelled", 0)
                        _aa_log(f"\n  {_WHITE}Batch complete: {_GREEN}{ok} OK{_NC}, {_RED}{fail} failed{_NC}, {_YELLOW}{cancelled} cancelled{_NC}")
                        return

    except KeyboardInterrupt:
        print(f"\n  {_DIM}Detached from progress stream. Batch continues on server.{_NC}")
        print(f"  Re-attach: {CMD} addaccounts status")
        print(f"  Stop:      {CMD} addaccounts stop")
    except Exception as e:
        _aa_log(f"SSE error: {e}")


def cmd_addaccounts_add():
    """Interactive batch add accounts."""
    print()
    print(f"  {_CYAN}{'='*50}{_NC}")
    print(f"  {_WHITE}UnifiedMe — Add Accounts{_NC}")
    print(f"  {_CYAN}{'='*50}{_NC}")

    # ── Check server ──
    print(f"\n  Checking proxy server...", end=" ", flush=True)
    if not _aa_check_server():
        print(f"{_RED}NOT RUNNING{_NC}")
        print(f"\n  Start the proxy first:")
        print(f"    {CMD} start")
        print(f"    {CMD} run")
        sys.exit(1)
    print(f"{_GREEN}OK{_NC}")

    # ── Check Camoufox deps (Linux) ──
    if platform.system() == "Linux":
        _, cam_missing = check_camoufox_deps()
        if cam_missing:
            print(f"\n  {_YELLOW}[WARN]{_NC} Missing {len(cam_missing)} Camoufox system packages:")
            for pkg in cam_missing[:5]:
                print(f"    - {pkg}")
            if len(cam_missing) > 5:
                print(f"    ... and {len(cam_missing) - 5} more")
            if _aa_yn("Install now? (requires sudo)", True):
                if install_camoufox_deps(cam_missing):
                    print(f"  {_GREEN}Installed{_NC}")
                else:
                    print(f"  {_RED}Install failed. Batch may error.{_NC}")
                    if not _aa_yn("Continue anyway?", False):
                        sys.exit(1)
            else:
                print(f"  {_DIM}Skipped. Run manually:{_NC}")
                print(f"    sudo apt install -y {' '.join(cam_missing)}")
                if not _aa_yn("Continue anyway?", False):
                    sys.exit(1)

    # ── Show license ──
    lic = _aa_get_license_info()
    if lic:
        print(f"\n  {_DIM}License:{_NC}  {lic['key'][:20]}...")
        print(f"  {_DIM}Owner:{_NC}    {lic['owner']}")
        print(f"  {_DIM}Tier:{_NC}     {lic['tier']}")
        print(f"  {_DIM}Max Acct:{_NC} {lic['max_accounts']}")
    else:
        print(f"\n  {_RED}No valid license found.{_NC}")
        print(f"  Run '{CMD} start' or '{CMD} run' to set up license first.")
        sys.exit(1)

    # ── Show current account stats ──
    stats = _get_account_stats()
    if stats:
        print(f"\n  {_DIM}Current accounts:{_NC} KR:{stats['kr']}  CB:{stats['cb']}  WS:{stats['ws']}  GL:{stats['gl']}  Total:{stats['total']}")

    # ── Step 1: Proxy ──
    print(f"\n  {_CYAN}--- Step 1: Proxy ---{_NC}")
    proxy_file = _aa_prompt("Proxy file (.txt) or 'n' for direct", "n")

    proxies = []
    proxy_method = "direct"
    if proxy_file.lower() != "n":
        raw = _aa_load_file(proxy_file, "proxies")
        if raw:
            print(f"\n  Loaded {len(raw)} proxies. Checking...", flush=True)
            results = _aa_check_proxies(raw)
            alive = [r for r in results if r["ok"]]
            dead = [r for r in results if not r["ok"]]
            print(f"  {_GREEN}Active: {len(alive)}{_NC}  |  {_RED}Dead: {len(dead)}{_NC}")
            for r in alive:
                p_display = r["url"].split("@")[-1] if "@" in r["url"] else r["url"]
                print(f"    {_GREEN}[OK]{_NC}   {p_display} -> {r['ip']}")
            for r in dead:
                p_display = r["url"].split("@")[-1] if "@" in r["url"] else r["url"]
                print(f"    {_RED}[FAIL]{_NC} {p_display} — {r['ip']}")

            if alive:
                proxies = [r["url"] for r in alive]
                print(f"\n  Proxy method:")
                print(f"    1. sticky")
                print(f"    2. smart_rotate (recommended)")
                choice = _aa_prompt("Choose [1-2]", "2")
                proxy_method = "smart_rotate" if choice == "2" else "sticky"
            else:
                print(f"  {_YELLOW}No working proxies. Using direct.{_NC}")
        else:
            print(f"  No proxies loaded. Using direct.")

    if proxies:
        print(f"\n  {_DIM}Proxy: {proxy_method} ({len(proxies)} active){_NC}")
    else:
        print(f"\n  {_DIM}Proxy: direct{_NC}")

    print(f"  {_DIM}Note: Proxy rotation uses server's batch proxy settings.{_NC}")

    # ── Step 2: Accounts ──
    print(f"\n  {_CYAN}--- Step 2: Accounts ---{_NC}")
    while True:
        account_file = _aa_prompt("Account file (.txt, email:password)")
        if not account_file:
            print("  Required.")
            continue
        raw_lines = _aa_load_file(account_file, "accounts")
        accounts = []
        for line in raw_lines:
            if ":" in line:
                parts = line.split(":", 1)
                accounts.append((parts[0].strip(), parts[1].strip()))
        if accounts:
            break
        print(f"  {_RED}No valid email:password pairs found.{_NC}")

    print(f"\n  {_WHITE}Loaded {len(accounts)} accounts{_NC}")
    for i, (email, _) in enumerate(accounts[:5], 1):
        print(f"    {i}. {email}")
    if len(accounts) > 5:
        print(f"    {_DIM}... and {len(accounts) - 5} more{_NC}")

    # ── Step 3: Providers ──
    print(f"\n  {_CYAN}--- Step 3: Providers ---{_NC}")
    providers = []
    if _aa_yn("Kiro?", False):
        providers.append("kiro")
    if _aa_yn("CodeBuddy?", True):
        providers.append("codebuddy")
    if _aa_yn("WaveSpeed?", False):
        providers.append("wavespeed")
    gumloop = _aa_yn("Gumloop?", False)
    if gumloop:
        providers.append("gumloop")

    if not providers:
        print(f"  {_RED}Select at least one provider.{_NC}")
        sys.exit(1)

    # ── Step 4: MCP ──
    mcp_urls = []
    if gumloop:
        print(f"\n  {_CYAN}--- Step 4: MCP Server ---{_NC}")
        mcp_input = _aa_prompt("MCP URL(s), comma-separated, or 'n' to skip", "n")
        if mcp_input.lower() != "n":
            mcp_urls = [u.strip() for u in mcp_input.split(",") if u.strip()]
            if mcp_urls:
                for u in mcp_urls:
                    print(f"    - {u}")

    # ── Step 5: Concurrency ──
    print(f"\n  {_CYAN}--- Step 5: Parallel ---{_NC}")
    concurrency = int(_aa_prompt("Parallel Camoufox instances", "1"))
    concurrency = max(1, min(10, concurrency))

    # ── Summary ──
    print(f"\n  {_CYAN}{'='*50}{_NC}")
    print(f"  {_WHITE}SUMMARY{_NC}")
    print(f"  {_CYAN}{'='*50}{_NC}")
    print(f"  Accounts:    {_WHITE}{len(accounts)}{_NC}")
    print(f"  Providers:   {_WHITE}{', '.join(providers)}{_NC}")
    if proxies:
        print(f"  Proxy:       {_WHITE}{proxy_method} ({len(proxies)}){_NC}")
    else:
        print(f"  Proxy:       {_DIM}direct{_NC}")
    if mcp_urls:
        print(f"  MCP servers: {_WHITE}{len(mcp_urls)}{_NC}")
    else:
        print(f"  MCP servers: {_DIM}none{_NC}")
    print(f"  Concurrency: {_WHITE}{concurrency}{_NC}")
    print(f"  Log:         {_DIM}{_AA_LOG}{_NC}")
    print(f"  Failed:      {_DIM}{_AA_FAIL}{_NC}")
    print(f"  {_CYAN}{'='*50}{_NC}")

    if not _aa_yn("Start batch?", True):
        print("  Cancelled.")
        return

    # ── Init logs ──
    with open(_AA_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"[{_aa_ts()}] Batch: {len(accounts)} accounts, providers={providers}\n")
        f.write(f"[{_aa_ts()}] Proxy: {proxy_method} ({len(proxies)}), MCP: {len(mcp_urls)}, Concurrency: {concurrency}\n")

    # ── Start ──
    print()
    account_lines = [f"{e}:{p}" for e, p in accounts]
    account_map = {e: p for e, p in accounts}

    try:
        result = _aa_api("POST", "/batch/start", {
            "accounts": account_lines,
            "providers": providers,
            "headless": True,
            "concurrency": concurrency,
            "mcp_urls": mcp_urls,
        })
        queued = result.get("count", 0)
        _aa_log(f"Queued {queued} jobs")
    except RuntimeError as e:
        _aa_log(f"{_RED}[ERROR] {e}{_NC}")
        sys.exit(1)

    # ── Stream ──
    _aa_stream_progress(account_map)

    # ── Done — show summary ──
    try:
        status = _aa_api("GET", "/batch/status", timeout=5)
        _aa_print_summary(status)
    except Exception:
        # Fallback if API unavailable
        fail_count = 0
        if _AA_FAIL.exists():
            fail_count = len([l for l in _AA_FAIL.read_text(encoding="utf-8").strip().splitlines() if l.strip()])
        print()
        print(f"  {_CYAN}{'='*50}{_NC}")
        if fail_count:
            print(f"  {_RED}Failed: {_AA_FAIL} ({fail_count} entries){_NC}")
        else:
            print(f"  {_GREEN}No failures!{_NC}")
        print(f"  {_DIM}Full log: {_AA_LOG}{_NC}")
        print(f"  {_CYAN}{'='*50}{_NC}")


def _aa_format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def _aa_format_ts(epoch: float) -> str:
    """Format epoch timestamp to readable string."""
    if not epoch:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _aa_print_summary(status: dict):
    """Print batch summary with timing and fail info."""
    jobs = status.get("jobs", [])
    total = len(jobs)
    ok = sum(1 for j in jobs if j.get("status") == "success")
    fail = sum(1 for j in jobs if j.get("status") == "failed")
    cancelled = sum(1 for j in jobs if j.get("status") == "cancelled")
    started_at = status.get("started_at", 0)
    finished_at = status.get("finished_at", 0)

    print(f"\n  {_CYAN}{'='*50}{_NC}")
    print(f"  {_WHITE}Batch Summary{_NC}")
    print(f"  {_CYAN}{'='*50}{_NC}")
    print(f"  Result:    {_GREEN}{ok} OK{_NC}, {_RED}{fail} failed{_NC}, {_YELLOW}{cancelled} cancelled{_NC}  (total {total})")
    print(f"  Started:   {_DIM}{_aa_format_ts(started_at)}{_NC}")
    if finished_at:
        print(f"  Finished:  {_DIM}{_aa_format_ts(finished_at)}{_NC}")
        if started_at:
            duration = finished_at - started_at
            print(f"  Duration:  {_DIM}{_aa_format_duration(duration)}{_NC}")
    else:
        if started_at:
            elapsed = time.time() - started_at
            print(f"  Elapsed:   {_DIM}{_aa_format_duration(elapsed)} (still running){_NC}")

    # Show fail file info
    fail_count = 0
    if _AA_FAIL.exists():
        lines = [l for l in _AA_FAIL.read_text(encoding="utf-8").strip().splitlines() if l.strip()]
        fail_count = len(lines)
    if fail_count:
        print(f"  Failed:    {_RED}{_AA_FAIL} ({fail_count} entries){_NC}")
    print(f"  Log:       {_DIM}{_AA_LOG}{_NC}")
    print(f"  {_CYAN}{'='*50}{_NC}")


def cmd_addaccounts_status():
    """Attach to running batch progress stream."""
    print(f"\n  Connecting to batch progress...", end=" ", flush=True)
    if not _aa_check_server():
        print(f"{_RED}Server not running{_NC}")
        sys.exit(1)

    try:
        status = _aa_api("GET", "/batch/status", timeout=5)
    except Exception:
        print(f"{_RED}Failed{_NC}")
        sys.exit(1)

    running = status.get("running", False)
    jobs = status.get("jobs", [])
    total = len(jobs)
    done = sum(1 for j in jobs if j.get("status") in ("success", "failed", "cancelled"))

    if not running and done == total and total > 0:
        print(f"{_YELLOW}Batch already finished{_NC}")
        _aa_print_summary(status)
        return
    elif not running and total == 0:
        print(f"{_DIM}No batch running{_NC}")
        return

    print(f"{_GREEN}Connected{_NC} ({done}/{total} done)")
    print(f"  {_DIM}Press Ctrl+C to detach (batch keeps running){_NC}\n")

    # Build account map from jobs for fail logging
    account_map = {j.get("email", "?"): "?" for j in jobs}
    _aa_stream_progress(account_map)

    # After stream ends, show summary
    try:
        status = _aa_api("GET", "/batch/status", timeout=5)
        _aa_print_summary(status)
    except Exception:
        pass


def cmd_addaccounts_stop():
    """Force stop running batch."""
    print(f"\n  Stopping batch...", end=" ", flush=True)
    if not _aa_check_server():
        print(f"{_RED}Server not running{_NC}")
        sys.exit(1)

    try:
        _aa_api("POST", "/batch/cancel")
        print(f"{_GREEN}Cancelled{_NC}")
        print(f"  Running jobs will finish, queued jobs will be skipped.")
    except Exception as e:
        print(f"{_RED}Failed: {e}{_NC}")


def cmd_addaccounts():
    """Route addaccounts subcommands."""
    args = sys.argv[2:]
    subcmd = args[0] if args else ""

    subcmds = {
        "add": cmd_addaccounts_add,
        "status": cmd_addaccounts_status,
        "stop": cmd_addaccounts_stop,
    }

    if subcmd in subcmds:
        subcmds[subcmd]()
    else:
        print(f"\n  Usage:")
        print(f"    {CMD} addaccounts add      Batch add accounts (interactive)")
        print(f"    {CMD} addaccounts status   Show batch progress (real-time)")
        print(f"    {CMD} addaccounts stop     Force stop running batch")


# ─── MCP Management CLI ─────────────────────────────────────────────────────

def cmd_mcp_list():
    """List MCP servers for all GL accounts."""
    print(f"\n  {_CYAN}MCP Servers — All Gumloop Accounts{_NC}")
    print(f"  {'='*50}")

    if not _aa_check_server():
        print(f"  {_RED}Server not running. Start with: {CMD} start{_NC}")
        sys.exit(1)

    # Get all accounts
    try:
        data = _aa_api("GET", "/accounts", timeout=10)
    except Exception as e:
        print(f"  {_RED}Failed to get accounts: {e}{_NC}")
        sys.exit(1)

    # Find GL accounts
    all_accts = data.get("accounts", {})
    active = all_accts.get("active", [])
    gl_accounts = [a for a in active if a.get("gl_status") == "ok" and a.get("gl_gummie_id")]

    if not gl_accounts:
        print(f"  {_DIM}No active Gumloop accounts found.{_NC}")
        return

    print(f"  {_DIM}Found {len(gl_accounts)} GL accounts. Fetching MCP info...{_NC}\n")

    for acct in gl_accounts:
        acct_id = acct.get("id", "")
        email = acct.get("email", "?")
        print(f"  {_WHITE}{email}{_NC}")

        try:
            res = _aa_api("GET", f"/accounts/{acct_id}/mcp-list", timeout=15)
            servers = res.get("mcp_servers", [])

            if not servers:
                print(f"    {_DIM}(no MCP servers){_NC}")
            else:
                for s in servers:
                    dot = f"{_GREEN}*{_NC}" if s.get("active") else f"{_RED}x{_NC}"
                    status = f"{_GREEN}ON{_NC}" if s.get("active") else f"{_DIM}OFF{_NC}"
                    print(f"    {dot} [{status}] {s.get('name', '?')} — {_DIM}{s.get('url', '?')}{_NC}")
        except Exception as e:
            print(f"    {_RED}Error: {e}{_NC}")

        print()

    print(f"  {_DIM}Manage via dashboard or: {CMD} mcp toggle{_NC}")


def cmd_mcp_toggle():
    """Interactive toggle MCP on/off for an account."""
    if not _aa_check_server():
        print(f"\n  {_RED}Server not running. Start with: {CMD} start{_NC}")
        sys.exit(1)

    # Get GL accounts
    try:
        data = _aa_api("GET", "/accounts", timeout=10)
    except Exception as e:
        print(f"\n  {_RED}Failed: {e}{_NC}")
        sys.exit(1)

    active = data.get("accounts", {}).get("active", [])
    gl_accounts = [a for a in active if a.get("gl_status") == "ok" and a.get("gl_gummie_id")]

    if not gl_accounts:
        print(f"\n  {_DIM}No active GL accounts.{_NC}")
        return

    # Pick account
    print(f"\n  {_CYAN}Select account:{_NC}")
    for i, a in enumerate(gl_accounts[:20], 1):
        print(f"    {i}. {a.get('email', '?')}")
    choice = _aa_prompt("Account number", "1")
    try:
        idx = int(choice) - 1
        acct = gl_accounts[idx]
    except (ValueError, IndexError):
        print(f"  {_RED}Invalid choice.{_NC}")
        return

    acct_id = acct["id"]
    email = acct.get("email", "?")

    # Get MCP list
    try:
        res = _aa_api("GET", f"/accounts/{acct_id}/mcp-list", timeout=15)
    except Exception as e:
        print(f"  {_RED}Failed: {e}{_NC}")
        return

    servers = res.get("mcp_servers", [])
    if not servers:
        print(f"\n  {_DIM}No MCP servers for {email}.{_NC}")
        print(f"  Add via dashboard or: {CMD} addaccounts add")
        return

    print(f"\n  {_WHITE}{email}{_NC} — MCP servers:")
    for i, s in enumerate(servers, 1):
        status = f"{_GREEN}ON{_NC}" if s.get("active") else f"{_RED}OFF{_NC}"
        print(f"    {i}. [{status}] {s.get('name', '?')} — {_DIM}{s.get('url', '?')}{_NC}")

    choice = _aa_prompt("Toggle which? (number)")
    try:
        idx = int(choice) - 1
        server = servers[idx]
    except (ValueError, IndexError):
        print(f"  {_RED}Invalid choice.{_NC}")
        return

    action = "disable" if server.get("active") else "enable"
    print(f"  {action.capitalize()}ing {server.get('name', '?')}...", end=" ", flush=True)

    try:
        body = {}
        if action == "enable":
            body["enable"] = [server["url"]]
        else:
            body["disable"] = [server["url"]]
        res = _aa_api("POST", f"/accounts/{acct_id}/mcp-toggle", json_body=body, timeout=30)
        if res.get("ok"):
            print(f"{_GREEN}Done{_NC} ({res.get('active_mcp', 0)} active)")
        else:
            print(f"{_RED}Failed: {res.get('error', '?')}{_NC}")
    except Exception as e:
        print(f"{_RED}Failed: {e}{_NC}")


def cmd_mcp_bind():
    """Bind MCP server URL(s) to Gumloop accounts.

    Usage:
        unifiedme mcp bind <url>                    Bind to ALL active GL accounts
        unifiedme mcp bind <url> --account <id>     Bind to specific account
        unifiedme mcp bind <url1>,<url2>            Multiple URLs (comma-separated)
    """
    args = sys.argv[3:]
    if not args:
        print(f"  Usage: {CMD} mcp bind <url> [--account <id>]")
        print(f"  Example: {CMD} mcp bind https://xxx.trycloudflare.com/mcp")
        return

    # Parse URL(s) — first arg, comma-separated
    raw_urls = args[0]
    mcp_urls = [u.strip() for u in raw_urls.split(",") if u.strip()]
    if not mcp_urls:
        print(f"  {_RED}No valid MCP URLs provided.{_NC}")
        return

    # Parse --account flag
    account_id = None
    if "--account" in args:
        idx = args.index("--account")
        if idx + 1 < len(args):
            try:
                account_id = int(args[idx + 1])
            except ValueError:
                print(f"  {_RED}Invalid account ID.{_NC}")
                return

    # Check server running
    if not _aa_check_server():
        print(f"  {_RED}Proxy not running. Start with: {CMD} start{_NC}")
        sys.exit(1)

    print(f"\n  {_CYAN}MCP Bind{_NC}")
    print(f"  {'='*50}")
    for u in mcp_urls:
        print(f"  URL: {_WHITE}{u}{_NC}")

    if account_id:
        # Single account
        print(f"  Target: account #{account_id}")
        print(f"\n  Binding...", end=" ", flush=True)
        try:
            res = _aa_api("POST", f"/accounts/{account_id}/bind-mcp", json_body={"mcp_urls": mcp_urls}, timeout=30)
            if res.get("ok"):
                print(f"{_GREEN}OK{_NC}")
                print(f"  Active MCP: {res.get('active_mcp', '?')}")
            else:
                print(f"{_RED}Failed: {res.get('error', '?')}{_NC}")
        except Exception as e:
            print(f"{_RED}Failed: {e}{_NC}")
    else:
        # All GL accounts
        print(f"  Target: ALL active Gumloop accounts")
        print()
        try:
            res = _aa_api("POST", "/accounts/bind-mcp-bulk", json_body={"mcp_urls": mcp_urls}, timeout=120)
            total = res.get("total", 0)
            success = res.get("success", 0)
            results = res.get("results", [])

            for r in results:
                email = r.get("email", "?")
                if r.get("ok"):
                    print(f"  {_GREEN}[OK]{_NC}   {email} — {r.get('active_mcp', '?')} MCP active")
                else:
                    print(f"  {_RED}[FAIL]{_NC} {email} — {r.get('error', '?')}")

            print(f"\n  {_WHITE}Result: {_GREEN}{success}{_NC}/{total} accounts bound{_NC}")
        except Exception as e:
            print(f"  {_RED}Failed: {e}{_NC}")


MCP_PID_FILE = DATA_DIR / "mcp.pid"
MCP_LOG_FILE = DATA_DIR / "mcp.log"


def cmd_mcp_start():
    """Start MCP server in background.

    Usage:
        unifiedme mcp start                          Interactive (asks folder name)
        unifiedme mcp start my-project               Workspace = ~/mcp-workspaces/my-project
        unifiedme mcp start ~/some/path              Workspace = ~/some/path (absolute)
        unifiedme mcp start my-project --port 9876   Custom port
    """
    args = sys.argv[3:]

    # Parse --port
    port = 9876
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            port = int(args[idx + 1])
            args = args[:idx] + args[idx + 2:]

    # Determine workspace
    if args and args[0] not in ("--port",):
        ws_input = args[0]
    else:
        # Interactive: ask for folder name
        print(f"\n  {_CYAN}MCP Server — Start{_NC}")
        print(f"  Workspace is the folder MCP tools can read/write.")
        print(f"  Enter a name (creates ~/mcp-workspaces/<name>)")
        print(f"  Or a full path (e.g. ~/my-project)")
        print()
        try:
            ws_input = input("  Workspace: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return
        if not ws_input:
            print(f"  {_RED}Workspace is required.{_NC}")
            return

    # Resolve workspace path
    if ws_input.startswith("/") or ws_input.startswith("~"):
        workspace = os.path.expanduser(ws_input)
    else:
        workspace = os.path.expanduser(f"~/mcp-workspaces/{ws_input}")

    os.makedirs(workspace, exist_ok=True)
    workspace = os.path.realpath(workspace)

    # Check if already running
    if MCP_PID_FILE.exists():
        try:
            old_pid = int(MCP_PID_FILE.read_text().strip())
            if _is_running(old_pid):
                print(f"  MCP server already running (PID {old_pid})")
                print(f"  Stop first: {CMD} mcp stop")
                return
        except (ValueError, OSError):
            pass

    # Check port
    port_pid = _port_in_use(port)
    if port_pid:
        print(f"  Port {port} already in use (PID {port_pid}).")
        try:
            answer = input("  Kill it? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return
        if answer == "y":
            _kill_pid(port_pid)
            time.sleep(1)
        else:
            return

    print(f"  Starting MCP server...")
    print(f"  Workspace: {workspace}")
    print(f"  Port:      {port}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    install_dir = Path(__file__).resolve().parent.parent
    python_bin = install_dir / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")

    if not python_bin.exists():
        python_bin = Path(sys.executable)

    mcp_script = install_dir / "mcp_server.py"
    if not mcp_script.exists():
        print(f"  {_RED}mcp_server.py not found at {mcp_script}{_NC}")
        return

    log_fh = open(MCP_LOG_FILE, "a")

    cmd_args = [
        str(python_bin), str(mcp_script),
        "--workspace", workspace,
        "--port", str(port),
        "--no-tunnel",
        "--no-interactive",
    ]

    # Load API key if available
    api_key_file = DATA_DIR / ".mcp_api_key"
    if api_key_file.exists():
        key = api_key_file.read_text().strip()
        if key:
            cmd_args.extend(["--api-key", key])

    if _is_windows():
        pythonw = _find_pythonw()
        if pythonw:
            cmd_args[0] = pythonw
        proc = subprocess.Popen(
            cmd_args,
            stdout=log_fh, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
    else:
        proc = subprocess.Popen(
            cmd_args,
            stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    MCP_PID_FILE.write_text(str(proc.pid))

    time.sleep(2)
    if _is_running(proc.pid):
        print(f"  {_GREEN}MCP server started (PID {proc.pid}){_NC}")
        print(f"  Endpoint: http://0.0.0.0:{port}/mcp")
        print(f"  Log:      {MCP_LOG_FILE}")
        print()
        print(f"  {_DIM}To expose via tunnel:{_NC}")
        print(f"    {CMD} tunnel start mcp --port {port}")
    else:
        print(f"  {_RED}Failed to start MCP server.{_NC}")
        # Show last lines of log
        if MCP_LOG_FILE.exists():
            lines = MCP_LOG_FILE.read_text(errors="replace").strip().split("\n")
            for line in lines[-5:]:
                print(f"    {_RED}{line.strip()[:120]}{_NC}")


def cmd_mcp_stop():
    """Stop MCP server."""
    if not MCP_PID_FILE.exists():
        print("  No MCP server running (no PID file)")
        return

    try:
        pid = int(MCP_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        MCP_PID_FILE.unlink(missing_ok=True)
        print("  Invalid PID file, cleaned up.")
        return

    if not _is_running(pid):
        print(f"  PID {pid} not running (stale PID file)")
        MCP_PID_FILE.unlink(missing_ok=True)
        return

    print(f"  Stopping MCP server (PID {pid})...", end=" ", flush=True)
    _kill_pid(pid)
    time.sleep(1)
    MCP_PID_FILE.unlink(missing_ok=True)
    print(f"{_GREEN}Stopped{_NC}")


def cmd_mcp_status():
    """Show MCP server status."""
    if MCP_PID_FILE.exists():
        try:
            pid = int(MCP_PID_FILE.read_text().strip())
            if _is_running(pid):
                print(f"  MCP Server: {_GREEN}RUNNING{_NC} (PID {pid})")
                print(f"  Log: {MCP_LOG_FILE}")
                return
        except (ValueError, OSError):
            pass
    print(f"  MCP Server: {_DIM}STOPPED{_NC}")


def cmd_mcp():
    """Route mcp subcommands."""
    args = sys.argv[2:]
    subcmd = args[0] if args else "list"

    subcmds = {
        "list": cmd_mcp_list,
        "toggle": cmd_mcp_toggle,
        "bind": cmd_mcp_bind,
        "start": cmd_mcp_start,
        "stop": cmd_mcp_stop,
        "status": cmd_mcp_status,
    }

    if subcmd in subcmds:
        subcmds[subcmd]()
    else:
        print(f"\n  Usage:")
        print(f"    {CMD} mcp start [folder]            Start MCP server (background)")
        print(f"    {CMD} mcp stop                      Stop MCP server")
        print(f"    {CMD} mcp status                    Show MCP server status")
        print(f"    {CMD} mcp list                      List MCP servers for all GL accounts")
        print(f"    {CMD} mcp toggle                    Enable/disable MCP on an account")
        print(f"    {CMD} mcp bind <url> [--account N]  Bind MCP URL to GL accounts")


# ─── Tunnel CLI ──────────────────────────────────────────────────────────────

def cmd_tunnel_start():
    """Start cloudflared tunnel."""
    from .tunnel_manager import start_tunnel, check_cloudflared

    if not check_cloudflared():
        print(f"  {_RED}cloudflared not installed.{_NC}")
        print(f"  Install: curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null")
        print(f"  Or run: {CMD} tunnel install")
        return

    args = sys.argv[3:]
    target = "proxy"
    port = None

    # Parse: unifiedme tunnel start [proxy|mcp] [--port 1430]
    if args and args[0] in ("proxy", "mcp"):
        target = args[0]
        args = args[1:]
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            port = int(args[idx + 1])

    default_port = LISTEN_PORT if target == "proxy" else 9876
    actual_port = port or default_port

    print(f"  Starting cloudflared tunnel for {target} (port {actual_port})...")
    result = start_tunnel(target, actual_port)

    if result.get("ok"):
        print(f"  {_GREEN}Tunnel started!{_NC}")
        print(f"  URL: {_WHITE}{result['url']}{_NC}")
        if target == "mcp":
            print(f"  MCP endpoint: {result['url']}/mcp")
        print(f"  PID: {result.get('pid', '-')}")
    else:
        print(f"  {_RED}Failed: {result.get('error', 'Unknown error')}{_NC}")


def cmd_tunnel_stop():
    """Stop cloudflared tunnel."""
    from .tunnel_manager import stop_tunnel

    args = sys.argv[3:]
    target = "proxy"
    if args and args[0] in ("proxy", "mcp"):
        target = args[0]

    print(f"  Stopping {target} tunnel...", end=" ", flush=True)
    result = stop_tunnel(target)
    if result.get("ok"):
        print(f"{_GREEN}Done{_NC}")
    else:
        print(f"{_RED}Failed: {result.get('error', '')}{_NC}")


def cmd_tunnel_status():
    """Show tunnel status."""
    from .tunnel_manager import get_tunnel_status, get_system_info

    sys_info = get_system_info()
    print(f"\n  {_CYAN}Tunnel Status{_NC}")
    print(f"  {'='*40}")

    # System
    cf_status = f"{_GREEN}installed{_NC}" if sys_info["cloudflared_installed"] else f"{_RED}not found{_NC}"
    ng_status = f"{_GREEN}installed{_NC}" if sys_info["nginx_installed"] else f"{_RED}not found{_NC}"
    print(f"  cloudflared: {cf_status}")
    print(f"  nginx:       {ng_status}")
    print(f"  OS:          {sys_info['os']}")
    print()

    # Tunnels
    for target in ("proxy", "mcp"):
        info = get_tunnel_status(target)
        status = info.get("status", "stopped")
        if status == "running":
            color = _GREEN
            url = info.get("url", "")
            uptime = info.get("uptime_seconds", 0)
            m, s = uptime // 60, uptime % 60
            print(f"  {_WHITE}{target.upper()}{_NC}: {color}{status}{_NC}")
            print(f"    URL:    {url}")
            print(f"    Port:   {info.get('port', '-')}")
            print(f"    PID:    {info.get('pid', '-')}")
            print(f"    Uptime: {m}m {s}s")
        else:
            print(f"  {_WHITE}{target.upper()}{_NC}: {_DIM}{status}{_NC}")
            if info.get("error"):
                print(f"    Error: {info['error']}")
        print()


def cmd_tunnel_install():
    """Install cloudflared and/or nginx."""
    import asyncio
    from .tunnel_manager import install_cloudflared, install_nginx, check_cloudflared, check_nginx

    if not check_cloudflared():
        print(f"  Installing cloudflared...", end=" ", flush=True)
        result = asyncio.run(install_cloudflared())
        if result.get("ok"):
            print(f"{_GREEN}OK{_NC}")
        else:
            print(f"{_RED}Failed: {result.get('error', '')}{_NC}")
    else:
        print(f"  cloudflared: {_GREEN}already installed{_NC}")

    if not check_nginx():
        print(f"  Installing nginx...", end=" ", flush=True)
        result = asyncio.run(install_nginx())
        if result.get("ok"):
            print(f"{_GREEN}OK{_NC}")
        else:
            print(f"{_RED}Failed: {result.get('error', '')}{_NC}")
    else:
        print(f"  nginx: {_GREEN}already installed{_NC}")


def cmd_tunnel():
    """Route tunnel subcommands."""
    args = sys.argv[2:]
    subcmd = args[0] if args else "status"

    subcmds = {
        "start": cmd_tunnel_start,
        "stop": cmd_tunnel_stop,
        "status": cmd_tunnel_status,
        "install": cmd_tunnel_install,
    }

    if subcmd in subcmds:
        subcmds[subcmd]()
    else:
        print(f"\n  Usage:")
        print(f"    {CMD} tunnel status              Show tunnel status")
        print(f"    {CMD} tunnel start [proxy|mcp]   Start cloudflared tunnel")
        print(f"    {CMD} tunnel stop [proxy|mcp]    Stop tunnel")
        print(f"    {CMD} tunnel install             Install cloudflared + nginx")


def cmd_vps_list():
    """List registered VPS servers."""
    import asyncio
    import aiosqlite
    from .config import DB_PATH

    async def _list():
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM vps_servers ORDER BY created_at DESC")
        rows = [dict(r) for r in await cur.fetchall()]
        await db.close()
        return rows

    servers = asyncio.run(_list())
    if not servers:
        print(f"  {_DIM}No VPS servers registered.{_NC}")
        print(f"  Add via dashboard or: {CMD} vps add")
        return

    print(f"\n  {_CYAN}VPS Servers{_NC}")
    print(f"  {'='*50}")
    for s in servers:
        status_color = _GREEN if s['status'] in ('online', 'installed') else _RED if s['status'] == 'offline' else _DIM
        print(f"  [{s['id']}] {_WHITE}{s['label'] or s['host']}{_NC} — {status_color}{s['status']}{_NC}")
        print(f"      {_DIM}{s['username']}@{s['host']}:{s['ssh_port']}{_NC}")
    print()


def cmd_vps_add():
    """Interactively add a VPS server."""
    import asyncio

    print(f"\n  {_CYAN}Add VPS Server{_NC}")
    print(f"  {'='*40}")

    host = input("  Host (IP): ").strip()
    if not host:
        print(f"  {_RED}Host is required.{_NC}")
        return
    username = input("  Username [root]: ").strip() or "root"
    password = input("  Password: ").strip()
    if not password:
        print(f"  {_RED}Password is required.{_NC}")
        return
    port_str = input("  SSH Port [22]: ").strip()
    port = int(port_str) if port_str.isdigit() else 22
    label = input("  Label (optional): ").strip() or host

    print(f"\n  Testing connection...", end=" ", flush=True)

    async def _add():
        from .vps_manager import test_connection
        from . import database as db
        from .config import DATA_DIR
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        await db.init_db()

        test = await test_connection(host, username, password, port)
        if not test.get("ok"):
            return test

        vps_id = await db.add_vps_server(
            host=host, username=username, password=password,
            ssh_port=port, label=label, os_info=test.get("os_info", ""),
        )
        await db.close_db()
        return {"ok": True, "id": vps_id, "os_info": test.get("os_info", "")}

    result = asyncio.run(_add())
    if result.get("ok"):
        print(f"{_GREEN}OK{_NC}")
        print(f"  ID: {result['id']}")
        print(f"  OS: {result.get('os_info', '')[:60]}")
    else:
        print(f"{_RED}FAILED{_NC}")
        print(f"  Error: {result.get('error', 'Unknown')}")


def cmd_vps_install():
    """Auto-install on a VPS."""
    import asyncio

    args = sys.argv[3:]
    if not args:
        print(f"  Usage: {CMD} vps install <vps_id>")
        return

    vps_id = int(args[0])

    async def _install():
        from . import database as db
        from .config import DATA_DIR
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        await db.init_db()

        server = await db.get_vps_server(vps_id)
        if not server:
            return {"ok": False, "error": "VPS not found"}

        from .vps_manager import auto_install
        result = await auto_install(
            server["host"], server["username"], server["password"], server["ssh_port"],
        )
        await db.close_db()
        return result

    print(f"  Installing on VPS #{vps_id}... (this may take a few minutes)")
    result = asyncio.run(_install())

    if result.get("ok"):
        for step in result.get("steps", []):
            icon = f"{_GREEN}OK{_NC}" if step["status"] == "done" else f"{_RED}FAIL{_NC}"
            print(f"  [{icon}] {step['step']}")
        print(f"\n  {_GREEN}Installation complete!{_NC}")
    else:
        print(f"  {_RED}Failed: {result.get('error', 'Unknown')}{_NC}")


def cmd_vps():
    """Route vps subcommands."""
    args = sys.argv[2:]
    subcmd = args[0] if args else "list"

    subcmds = {
        "list": cmd_vps_list,
        "add": cmd_vps_add,
        "install": cmd_vps_install,
    }

    if subcmd in subcmds:
        subcmds[subcmd]()
    else:
        print(f"\n  Usage:")
        print(f"    {CMD} vps list              List registered VPS servers")
        print(f"    {CMD} vps add               Add a VPS server (interactive)")
        print(f"    {CMD} vps install <id>      Auto-install on a VPS")


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
        "addaccounts": cmd_addaccounts,
        "mcp": cmd_mcp,
        "tunnel": cmd_tunnel,
        "vps": cmd_vps,
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
