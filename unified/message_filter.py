"""Message content filter — scan & replace blocked patterns before upstream."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

log = logging.getLogger("unified.message_filter")

# ---------------------------------------------------------------------------
# Cache — reload rules from DB every 30 seconds
# ---------------------------------------------------------------------------

_cache: list[dict] = []
_cache_ts: float = 0
_CACHE_TTL = 30.0


async def _load_rules() -> list[dict]:
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache and (now - _cache_ts) < _CACHE_TTL:
        return _cache
    from . import database as db
    _cache = await db.get_filters(enabled_only=True)
    _cache_ts = now
    return _cache


def invalidate_cache() -> None:
    """Force next call to _load_rules to re-fetch from DB."""
    global _cache_ts
    _cache_ts = 0


# ---------------------------------------------------------------------------
# Core replacement logic
# ---------------------------------------------------------------------------

def _apply_rules(text: str, rules: list[dict]) -> tuple[str, list[int]]:
    """Apply filter rules to a text string.

    Returns (filtered_text, list_of_hit_filter_ids).
    """
    hits: list[int] = []
    for rule in rules:
        find = rule["find_text"]
        replace = rule["replace_text"]
        is_regex = bool(rule.get("is_regex", 0))

        if is_regex:
            try:
                pattern = re.compile(find, re.IGNORECASE)
                new_text, count = pattern.subn(replace, text)
                if count > 0:
                    text = new_text
                    hits.append(rule["id"])
            except re.error:
                continue
        else:
            if find in text:
                text = text.replace(find, replace)
                hits.append(rule["id"])
    return text, hits


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def filter_messages(body: dict) -> dict:
    """Filter messages in a request body dict.

    Scans every message's ``content`` field (string or Anthropic-style list)
    and applies all enabled replacement rules.  Modifies *body* in-place and
    returns it.
    """
    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        return body

    rules = await _load_rules()
    if not rules:
        return body

    all_hits: list[int] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")

        # Plain string content
        if isinstance(content, str) and content:
            filtered, hits = _apply_rules(content, rules)
            if hits:
                msg["content"] = filtered
                all_hits.extend(hits)

        # Anthropic-style content blocks (list of dicts with "text" key)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    filtered, hits = _apply_rules(block["text"], rules)
                    if hits:
                        block["text"] = filtered
                        all_hits.extend(hits)

    # Bump hit counters (best-effort, don't block the request)
    if all_hits:
        from . import database as db
        unique_hits = set(all_hits)
        for fid in unique_hits:
            try:
                await db.increment_filter_hit(fid)
            except Exception:
                pass
        log.info("Filtered %d rule(s) in request", len(unique_hits))

    return body
