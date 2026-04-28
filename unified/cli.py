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

    # ── Done ──
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
        ok = sum(1 for j in jobs if j.get("status") == "success")
        fail = sum(1 for j in jobs if j.get("status") == "failed")
        print(f"  Result: {_GREEN}{ok} OK{_NC}, {_RED}{fail} failed{_NC}, total {total}")
        return
    elif not running and total == 0:
        print(f"{_DIM}No batch running{_NC}")
        return

    print(f"{_GREEN}Connected{_NC} ({done}/{total} done)")
    print(f"  {_DIM}Press Ctrl+C to detach (batch keeps running){_NC}\n")

    # Build account map from jobs for fail logging
    account_map = {j.get("email", "?"): "?" for j in jobs}
    _aa_stream_progress(account_map)


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
