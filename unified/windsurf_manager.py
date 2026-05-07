"""Windsurf sidecar manager — spawn/stop/health check the Node.js WindsurfAPI process."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx

from .config import WINDSURF_SIDECAR_PORT, WINDSURF_UPSTREAM, WINDSURF_INTERNAL_KEY, BASE_DIR, DATA_DIR

log = logging.getLogger("unified.windsurf_manager")

SIDECAR_DIR = BASE_DIR / "windsurf"
SIDECAR_ENTRY = SIDECAR_DIR / "src" / "index.js"
SIDECAR_LOG = DATA_DIR / "windsurf_sidecar.log"
HEALTH_URL = f"{WINDSURF_UPSTREAM}/health"
LOGIN_URL = f"{WINDSURF_UPSTREAM}/auth/login"


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is alive (cross-platform)."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _kill_pid(pid: int) -> None:
    """Kill a process by PID (cross-platform)."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        else:
            os.kill(pid, 15)  # SIGTERM
            time.sleep(2)
            try:
                os.kill(pid, 0)
                os.kill(pid, 9)  # SIGKILL if still alive
            except OSError:
                pass
    except Exception as e:
        log.warning("Failed to kill PID %d: %s", pid, e)


def _kill_port(port: int) -> None:
    """Kill any process listening on the given port (cross-platform)."""
    try:
        if os.name == "nt":
            # Find PID on port
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    parts = line.strip().split()
                    pid = int(parts[-1])
                    if pid > 0:
                        log.info("Killing existing process on port %d (PID %d)", port, pid)
                        subprocess.run(
                            ["taskkill", "/F", "/PID", str(pid), "/T"],
                            capture_output=True, timeout=10,
                        )
        else:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5,
            )
            for pid_str in result.stdout.strip().splitlines():
                pid = int(pid_str.strip())
                if pid > 0:
                    log.info("Killing existing process on port %d (PID %d)", port, pid)
                    os.kill(pid, 15)
    except Exception as e:
        log.debug("kill_port(%d) error (non-fatal): %s", port, e)


class WindsurfSidecar:
    """Manages the WindsurfAPI Node.js sidecar process."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._pid: Optional[int] = None
        self._port: int = WINDSURF_SIDECAR_PORT
        self._ready: bool = False
        self._started_once: bool = False  # Prevent spam restarts

    def is_available(self) -> bool:
        """Check if sidecar can be started (Node.js + entry point exist)."""
        node_bin = shutil.which("node")
        if not node_bin:
            return False
        if not SIDECAR_ENTRY.exists():
            return False
        return True

    def is_running(self) -> bool:
        """Check if sidecar process is alive."""
        if self._pid and _is_pid_alive(self._pid):
            return True
        self._ready = False
        return False

    async def start(self) -> bool:
        """Spawn the Node.js sidecar. Returns True if started successfully.

        - Kills any existing process on the sidecar port first
        - Only starts once per proxy lifetime (no spam restarts)
        - LS binary window is hidden on Windows
        """
        # Already running? Just health check.
        if self.is_running() and await self.health():
            self._ready = True
            return True

        # Check prerequisites
        node_bin = shutil.which("node")
        if not node_bin:
            log.error("Node.js not found in PATH — cannot start Windsurf sidecar")
            return False

        if not SIDECAR_ENTRY.exists():
            log.error("Sidecar entry not found: %s — run 'git clone' first", SIDECAR_ENTRY)
            return False

        # Kill any existing process on the port (clean start)
        _kill_port(self._port)
        # Also kill our tracked PID if stale
        if self._pid and _is_pid_alive(self._pid):
            _kill_pid(self._pid)
        await asyncio.sleep(1)

        # Ensure data dir exists
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        env = {
            **os.environ,
            "PORT": str(self._port),
            "API_KEY": WINDSURF_INTERNAL_KEY,
            "LOG_LEVEL": "info",
            "DATA_DIR": str(SIDECAR_DIR),
        }

        log_fh = open(SIDECAR_LOG, "a", encoding="utf-8")
        log_fh.write(f"\n{'='*60}\n")
        log_fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting Windsurf sidecar\n")
        log_fh.write(f"  Port: {self._port}\n")
        log_fh.write(f"  Dir:  {SIDECAR_DIR}\n")
        log_fh.write(f"  Node: {node_bin}\n")
        log_fh.write(f"{'='*60}\n")
        log_fh.flush()

        try:
            cmd = [node_bin, str(SIDECAR_ENTRY)]
            if platform.system() == "Windows":
                # CREATE_NO_WINDOW hides both Node.js AND child LS binary windows
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(SIDECAR_DIR),
                    env=env,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(SIDECAR_DIR),
                    env=env,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            self._pid = self._proc.pid
            log.info("Windsurf sidecar spawned (PID %d, port %d)", self._pid, self._port)
        except Exception as e:
            log.error("Failed to spawn Windsurf sidecar: %s", e)
            log_fh.close()
            return False

        # Wait for health endpoint (up to 30s)
        for i in range(30):
            await asyncio.sleep(1)
            if not self.is_running():
                log.error("Windsurf sidecar exited prematurely")
                return False
            if await self.health():
                self._ready = True
                self._started_once = True
                log.info("Windsurf sidecar ready (took %ds)", i + 1)
                return True

        log.error("Windsurf sidecar did not become ready within 30s")
        return False

    async def stop(self) -> None:
        """Stop the sidecar process and all child processes (LS binary)."""
        if self._pid:
            if os.name == "nt":
                # /T flag kills the entire process tree (node + LS binary)
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(self._pid), "/T"],
                        capture_output=True, timeout=10,
                    )
                except Exception:
                    pass
            else:
                _kill_pid(self._pid)

        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass

        # Also kill anything left on the port
        _kill_port(self._port)

        self._proc = None
        self._pid = None
        self._ready = False
        self._started_once = False
        log.info("Windsurf sidecar stopped")

    async def health(self) -> bool:
        """Check sidecar health endpoint."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(HEALTH_URL)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("status") == "ok"
        except Exception:
            pass
        return False

    async def ensure_running(self) -> bool:
        """Start sidecar if not running. Returns True if ready.

        Only attempts start ONCE per proxy lifetime. If sidecar failed to start
        or crashed, don't spam restart — return False until proxy restarts.
        """
        # Already confirmed ready
        if self._ready and self.is_running():
            return True

        # Quick health check — maybe started externally (manual `node src/index.js`)
        if await self.health():
            self._ready = True
            return True

        # Already tried and failed this session — don't spam
        if self._started_once and not self.is_running():
            return False

        return await self.start()

    async def add_account(self, api_key: str, label: str = "") -> bool:
        """Add an account to the sidecar's pool via REST API."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    LOGIN_URL,
                    json={"api_key": api_key, "label": label},
                    headers={"Authorization": f"Bearer {WINDSURF_INTERNAL_KEY}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("id") or data.get("results"):
                        return True
                log.warning("Sidecar add_account failed: HTTP %d — %s", resp.status_code, resp.text[:200])
        except Exception as e:
            log.warning("Sidecar add_account error: %s", e)
        return False

    async def sync_accounts_from_db(self) -> int:
        """Sync all active Windsurf accounts from local DB to sidecar."""
        from . import database as db

        accounts = await db.get_accounts()
        synced = 0
        for acc in accounts:
            if acc.get("windsurf_status") == "ok" and acc.get("windsurf_api_key"):
                ok = await self.add_account(
                    api_key=acc["windsurf_api_key"],
                    label=acc.get("email", ""),
                )
                if ok:
                    synced += 1
        if synced:
            log.info("Synced %d Windsurf accounts to sidecar", synced)
        return synced


# Module-level singleton
windsurf_sidecar = WindsurfSidecar()
