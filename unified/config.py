"""Unified AI Proxy — configuration constants and model routing."""

import os
from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
LISTEN_HOST = os.getenv("UNIFIED_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("UNIFIED_PORT", "1430"))

# DEPRECATED: Kiro-Go binary no longer needed — API calls handled directly in Python.
# Kept for backward compat with account_manager credit refresh (will fail gracefully if Go not running).
KIRO_UPSTREAM = os.getenv("KIRO_UPSTREAM", "http://127.0.0.1:1434")
CODEBUDDY_UPSTREAM = os.getenv("CODEBUDDY_UPSTREAM", "https://www.codebuddy.ai")
WAVESPEED_UPSTREAM = os.getenv("WAVESPEED_UPSTREAM", "https://llm.wavespeed.ai")

# Gumloop
GUMLOOP_API_BASE = os.getenv("GUMLOOP_API_BASE", "https://api.gumloop.com")
GUMLOOP_WS_URL = os.getenv("GUMLOOP_WS_URL", "wss://ws.gumloop.com/ws/gummies")
GL_DEFAULT_CREDITS = 0.0  # No credit tracking — usage count only

# ---------------------------------------------------------------------------
# Auth / Admin
# ---------------------------------------------------------------------------
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "kUcingku0")
KIRO_ADMIN_PASSWORD = os.getenv("KIRO_ADMIN_PASSWORD", "kUcingku0")

# ---------------------------------------------------------------------------
# Credit defaults
# ---------------------------------------------------------------------------
KIRO_DEFAULT_CREDITS = 550.0
CB_DEFAULT_CREDITS = 250.0
WS_DEFAULT_CREDITS = 1.0  # $1 trial credit

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
_VERSION_FILE = BASE_DIR.parent / "VERSION"
VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "0.0.0"

# Central API for version check
CENTRAL_API_URL = os.getenv("CENTRAL_API_URL", "https://unified-api.roubot71.workers.dev")

# Database
# ---------------------------------------------------------------------------
DATA_DIR = BASE_DIR / "data"
DB_PATH = str(DATA_DIR / "unified.db")

# ---------------------------------------------------------------------------
# Login scripts
# ---------------------------------------------------------------------------
AUTH_DIR = BASE_DIR.parent          # auth/
AUTH_SCRIPT = AUTH_DIR / "login.py"
WAVESPEED_DIR = BASE_DIR.parent / "wavespeed"
WAVESPEED_SCRIPT = WAVESPEED_DIR / "register.py"
GUMLOOP_SCRIPT = AUTH_DIR / "gumloop_login.py"
# Windows uses Scripts/, Linux uses bin/
_VENV_BIN = "Scripts" if os.name == "nt" else "bin"
PYTHON_BIN = AUTH_DIR / ".venv" / _VENV_BIN / "python"

# ---------------------------------------------------------------------------
# Proxy (outbound)
# ---------------------------------------------------------------------------
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() in ("true", "1", "yes")
PROXY_URL = os.getenv("PROXY_URL", "")  # e.g. http://user:pass@host:port or socks5://host:port

# ---------------------------------------------------------------------------
# Tier enum
# ---------------------------------------------------------------------------

class Tier(str, Enum):
    STANDARD = "standard"       # Kiro
    MAX = "max"                 # CodeBuddy
    WAVESPEED = "wavespeed"     # WaveSpeed LLM
    MAX_GL = "max_gl"           # Gumloop


# ---------------------------------------------------------------------------
# Model → Tier routing table
# ---------------------------------------------------------------------------
_STANDARD_MODELS = [
    "auto",
    "claude-sonnet-4.5",
    "claude-sonnet-4",
    "claude-haiku-4.5",
    "deepseek-3.2",
    "minimax-m2.5",
    "minimax-m2.1",
    "glm-5",
    "qwen3-coder-next",
]

_MAX_MODELS = [
    "gemini-3.1-pro",
    "gemini-3.1-flash-lite",
    "gemini-3.0-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gpt-5.4",
    "gpt-5.2",
    "gpt-5.3-codex",
    "gpt-5.2-codex",
    "gpt-5.1",
    "gpt-5.1-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
    "deepseek-v3-2-volc",
    "claude-opus-4.6",
    "kimi-k2.5",
]

_MAX_GL_MODELS = [
    "gl-claude-opus-4-7",
    "gl-claude-opus-4-6",
    "gl-claude-sonnet-4-6",
    "gl-claude-sonnet-4-5",
    "gl-claude-haiku-4-5",
    "gl-gpt-5.4",
    "gl-gpt-5.4-mini",
    "gl-gpt-5.4-nano",
    "gl-gpt-5.3-code",
    "gl-gpt-5.2",
    "gl-gpt-5.2-codex",
]

# Dot-format aliases for Claude models (GPT already uses dots)
_MAX_GL_DOT_ALIASES = {
    "gl-claude-opus-4.7": "gl-claude-opus-4-7",
    "gl-claude-opus-4.6": "gl-claude-opus-4-6",
    "gl-claude-sonnet-4.6": "gl-claude-sonnet-4-6",
    "gl-claude-sonnet-4.5": "gl-claude-sonnet-4-5",
    "gl-claude-haiku-4.5": "gl-claude-haiku-4-5",
}

# WaveSpeed models use provider/model format — any model with "/" is WaveSpeed
# We also define popular aliases without the provider prefix
_WAVESPEED_ALIASES = [
    "new-claude-opus-4.7",
    "new-claude-opus-4.6",
    "new-claude-sonnet-4.5",
    "new-claude-sonnet-4",
    "new-claude-haiku-4.5",
    "new-gpt-5.2",
    "new-gpt-4o",
    "new-gemini-3-pro",
    "new-gemini-2.5-flash",
    "new-grok-4",
    "new-deepseek-r1",
    "new-llama-4-maverick",
    "new-qwen-max",
]

# Build lookup: model_name → Tier
MODEL_TIER: dict[str, Tier] = {}

for m in _STANDARD_MODELS:
    MODEL_TIER[m] = Tier.STANDARD
    MODEL_TIER[f"{m}-thinking"] = Tier.STANDARD

for m in _MAX_MODELS:
    MODEL_TIER[m] = Tier.MAX

for m in _WAVESPEED_ALIASES:
    MODEL_TIER[m] = Tier.WAVESPEED

for m in _MAX_GL_MODELS:
    MODEL_TIER[m] = Tier.MAX_GL
for alias in _MAX_GL_DOT_ALIASES:
    MODEL_TIER[alias] = Tier.MAX_GL

# Hidden from display lists (routing-only aliases + thinking variants)
_HIDDEN_ALIASES: set[str] = set(_MAX_GL_DOT_ALIASES.keys())
# Hide -thinking variants from model list (they still work for routing)
for _m in list(_STANDARD_MODELS):
    _thinking = f"{_m}-thinking"
    if _thinking in MODEL_TIER:
        _HIDDEN_ALIASES.add(_thinking)

# Flat lists for /v1/models
STANDARD_MODELS: list[str] = [k for k, v in MODEL_TIER.items() if v == Tier.STANDARD]
MAX_MODELS: list[str] = [k for k, v in MODEL_TIER.items() if v == Tier.MAX]
WAVESPEED_MODELS: list[str] = [k for k, v in MODEL_TIER.items() if v == Tier.WAVESPEED]
MAX_GL_MODELS: list[str] = [k for k, v in MODEL_TIER.items() if v == Tier.MAX_GL and k not in _HIDDEN_ALIASES]
ALL_MODELS: list[str] = [k for k in MODEL_TIER if k not in _HIDDEN_ALIASES]


def get_tier(model: str) -> Tier | None:
    """Return the tier for a model name, or None if unknown.

    WaveSpeed models can be:
    - Explicit alias: new-claude-opus-4.7
    - Direct provider format: anthropic/claude-opus-4.7 (any model with /)
    - Prefix: new-<anything>
    """
    # Check explicit mapping first
    tier = MODEL_TIER.get(model)
    if tier is not None:
        return tier
    # Any model with "gl-" prefix → MAX_GL (Gumloop)
    if model.startswith("gl-"):
        return Tier.MAX_GL
    # Any model with "new-" prefix → WaveSpeed
    if model.startswith("new-"):
        return Tier.WAVESPEED
    # Any model with "/" → WaveSpeed (provider/model format)
    if "/" in model:
        return Tier.WAVESPEED
    return None
