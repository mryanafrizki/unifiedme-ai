"""Utility functions for Kiro API — headers, fingerprint, ID generation.

Ported from kiro-gateway utils.py.
"""

from __future__ import annotations

import hashlib
import os
import platform
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .auth import KiroAuthManager


def get_machine_fingerprint() -> str:
    """SHA256 of {hostname}-{username}-kiro-gateway."""
    hostname = platform.node()
    username = os.getenv("USER") or os.getenv("USERNAME") or "unknown"
    raw = f"{hostname}-{username}-kiro-gateway"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def get_kiro_headers(auth_manager: "KiroAuthManager", access_token: str) -> dict[str, str]:
    """Build headers mimicking Kiro IDE 0.7.45."""
    fingerprint = auth_manager.fingerprint
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": f"KiroIDE-0.7.45-{fingerprint}",
        "x-amz-user-agent": f"KiroIDE-0.7.45-{fingerprint}",
        "x-amzn-codewhisperer-optout": "true",
        "x-amzn-kiro-agent-mode": "FULL_AUTO",
        "amz-sdk-invocation-id": str(uuid.uuid4()),
    }


def generate_completion_id() -> str:
    """Generate a unique completion ID."""
    return f"chatcmpl-{uuid.uuid4().hex}"


def generate_conversation_id(messages: list | None = None) -> str:
    """Generate a stable conversation ID from messages, or random UUID."""
    if not messages or len(messages) == 0:
        return str(uuid.uuid4())

    # Hash first 3 + last message for stability
    parts = []
    for msg in messages[:3]:
        content = msg.get("content", "") if isinstance(msg, dict) else ""
        if isinstance(content, list):
            content = str(content)
        parts.append(str(content)[:200])

    if len(messages) > 3:
        last = messages[-1]
        content = last.get("content", "") if isinstance(last, dict) else ""
        if isinstance(content, list):
            content = str(content)
        parts.append(str(content)[:200])

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def generate_tool_call_id() -> str:
    """Generate a unique tool call ID."""
    return f"call_{uuid.uuid4().hex[:8]}"
