"""Kiro API configuration constants.

Ported from kiro-gateway config.py — only the constants needed
for direct API calls. Server/proxy settings live in unified/config.py.
"""

from __future__ import annotations

import os
from typing import Dict, List

# ---------------------------------------------------------------------------
# Kiro API URL Templates
# ---------------------------------------------------------------------------
KIRO_REFRESH_URL_TEMPLATE = "https://prod.{region}.auth.desktop.kiro.dev/refreshToken"
AWS_SSO_OIDC_URL_TEMPLATE = "https://oidc.{region}.amazonaws.com/token"
KIRO_API_HOST_TEMPLATE = "https://q.{region}.amazonaws.com"
KIRO_Q_HOST_TEMPLATE = "https://q.{region}.amazonaws.com"

# ---------------------------------------------------------------------------
# Token Settings
# ---------------------------------------------------------------------------
TOKEN_REFRESH_THRESHOLD: int = 600  # seconds before expiry to trigger refresh

# ---------------------------------------------------------------------------
# Retry Configuration
# ---------------------------------------------------------------------------
MAX_RETRIES: int = 3
BASE_RETRY_DELAY: float = 1.0

# ---------------------------------------------------------------------------
# Streaming Timeouts
# ---------------------------------------------------------------------------
FIRST_TOKEN_TIMEOUT: float = float(os.getenv("KIRO_FIRST_TOKEN_TIMEOUT", "15"))
STREAMING_READ_TIMEOUT: float = float(os.getenv("KIRO_STREAMING_READ_TIMEOUT", "300"))
FIRST_TOKEN_MAX_RETRIES: int = int(os.getenv("KIRO_FIRST_TOKEN_MAX_RETRIES", "3"))

# ---------------------------------------------------------------------------
# Hidden Models (display_name -> internal Kiro model ID)
# ---------------------------------------------------------------------------
HIDDEN_MODELS: Dict[str, str] = {
    "claude-3.7-sonnet": "CLAUDE_3_7_SONNET_20250219_V1_0",
}

# ---------------------------------------------------------------------------
# Model Aliases
# ---------------------------------------------------------------------------
MODEL_ALIASES: Dict[str, str] = {
    "auto-kiro": "auto",
}

HIDDEN_FROM_LIST: List[str] = ["auto"]

# ---------------------------------------------------------------------------
# Fallback Models (DNS failure recovery)
# ---------------------------------------------------------------------------
FALLBACK_MODELS: List[Dict[str, str]] = [
    {"modelId": "auto"},
    {"modelId": "claude-sonnet-4"},
    {"modelId": "claude-haiku-4.5"},
    {"modelId": "claude-sonnet-4.5"},
    {"modelId": "claude-opus-4.5"},
]

# ---------------------------------------------------------------------------
# Model Cache
# ---------------------------------------------------------------------------
MODEL_CACHE_TTL: int = 3600
DEFAULT_MAX_INPUT_TOKENS: int = 200_000

# ---------------------------------------------------------------------------
# Tool Description Handling
# ---------------------------------------------------------------------------
TOOL_DESCRIPTION_MAX_LENGTH: int = int(os.getenv("KIRO_TOOL_DESC_MAX_LENGTH", "10000"))

# ---------------------------------------------------------------------------
# Truncation Recovery
# ---------------------------------------------------------------------------
TRUNCATION_RECOVERY: bool = os.getenv("KIRO_TRUNCATION_RECOVERY", "true").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Fake Reasoning (thinking mode via tag injection)
# ---------------------------------------------------------------------------
_FAKE_REASONING_RAW = os.getenv("KIRO_FAKE_REASONING", "").lower()
FAKE_REASONING_ENABLED: bool = _FAKE_REASONING_RAW not in ("false", "0", "no", "disabled", "off", "")

FAKE_REASONING_MAX_TOKENS: int = int(os.getenv("KIRO_FAKE_REASONING_MAX_TOKENS", "4000"))
FAKE_REASONING_BUDGET_CAP: int = int(os.getenv("KIRO_FAKE_REASONING_BUDGET_CAP", "10000"))

_FAKE_REASONING_HANDLING_RAW = os.getenv("KIRO_FAKE_REASONING_HANDLING", "as_reasoning_content").lower()
FAKE_REASONING_HANDLING: str = (
    _FAKE_REASONING_HANDLING_RAW
    if _FAKE_REASONING_HANDLING_RAW in ("as_reasoning_content", "remove", "pass", "strip_tags")
    else "as_reasoning_content"
)

FAKE_REASONING_OPEN_TAGS: List[str] = ["<thinking>", "<think>", "<reasoning>", "<thought>"]
FAKE_REASONING_INITIAL_BUFFER_SIZE: int = int(os.getenv("KIRO_FAKE_REASONING_BUFFER", "20"))

# ---------------------------------------------------------------------------
# Payload Size Guard
# ---------------------------------------------------------------------------
KIRO_MAX_PAYLOAD_BYTES: int = int(os.getenv("KIRO_MAX_PAYLOAD_BYTES", "600000"))
AUTO_TRIM_PAYLOAD: bool = os.getenv("KIRO_AUTO_TRIM_PAYLOAD", "false").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# WebSearch (MCP tool emulation)
# ---------------------------------------------------------------------------
WEB_SEARCH_ENABLED: bool = os.getenv("KIRO_WEB_SEARCH", "true").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Default region
# ---------------------------------------------------------------------------
DEFAULT_REGION: str = os.getenv("KIRO_REGION", "us-east-1")

# ---------------------------------------------------------------------------
# App version (for User-Agent)
# ---------------------------------------------------------------------------
APP_VERSION: str = "2.4-rc.1"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def get_kiro_refresh_url(region: str) -> str:
    return KIRO_REFRESH_URL_TEMPLATE.format(region=region)


def get_aws_sso_oidc_url(region: str) -> str:
    return AWS_SSO_OIDC_URL_TEMPLATE.format(region=region)


def get_kiro_api_host(region: str) -> str:
    return KIRO_API_HOST_TEMPLATE.format(region=region)


def get_kiro_q_host(region: str) -> str:
    return KIRO_Q_HOST_TEMPLATE.format(region=region)
