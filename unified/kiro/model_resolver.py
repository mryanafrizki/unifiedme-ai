"""Model name normalization for Kiro API.

Ported from kiro-gateway model_resolver.py.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from .config import HIDDEN_MODELS, MODEL_ALIASES

log = logging.getLogger("unified.kiro.model_resolver")


def normalize_model_name(model: str) -> str:
    """Normalize model name to canonical form.

    Handles patterns like:
    - claude-haiku-4-5-20251001 -> claude-haiku-4.5
    - claude-sonnet-4-20250514 -> claude-sonnet-4
    - claude-3-7-sonnet-20250219 -> claude-3.7-sonnet
    - claude-haiku-4.5-20251001 -> claude-haiku-4.5
    - claude-4.5-opus-high -> claude-opus-4.5
    """
    original = model

    # Check aliases first
    if model in MODEL_ALIASES:
        resolved = MODEL_ALIASES[model]
        log.debug("Alias: %s -> %s", model, resolved)
        return resolved

    # Pattern 1: claude-{family}-{major}-{minor}-{date} -> claude-{family}-{major}.{minor}
    m = re.match(r'^(claude-(?:haiku|sonnet|opus))-(\d+)-(\d+)-\d{8,}$', model)
    if m:
        result = f"{m.group(1)}-{m.group(2)}.{m.group(3)}"
        if result != original:
            log.debug("Normalized: %s -> %s", original, result)
        return result

    # Pattern 2: claude-{family}-{major}-{date} -> claude-{family}-{major}
    m = re.match(r'^(claude-(?:haiku|sonnet|opus))-(\d+)-\d{8,}$', model)
    if m:
        result = f"{m.group(1)}-{m.group(2)}"
        if result != original:
            log.debug("Normalized: %s -> %s", original, result)
        return result

    # Pattern 3: claude-{major}-{minor}-{family}-{date} -> claude-{major}.{minor}-{family}
    m = re.match(r'^claude-(\d+)-(\d+)-(haiku|sonnet|opus)-\d{8,}$', model)
    if m:
        result = f"claude-{m.group(1)}.{m.group(2)}-{m.group(3)}"
        if result != original:
            log.debug("Normalized: %s -> %s", original, result)
        return result

    # Pattern 4: claude-{family}-{major}.{minor}-{date} -> claude-{family}-{major}.{minor}
    m = re.match(r'^(claude-(?:haiku|sonnet|opus)-\d+\.\d+)-\d{8,}$', model)
    if m:
        result = m.group(1)
        if result != original:
            log.debug("Normalized: %s -> %s", original, result)
        return result

    # Pattern 5: claude-{major}.{minor}-{family}-{suffix} -> claude-{family}-{major}.{minor}
    m = re.match(r'^claude-(\d+\.\d+)-(haiku|sonnet|opus)(?:-\w+)?$', model)
    if m:
        result = f"claude-{m.group(2)}-{m.group(1)}"
        if result != original:
            log.debug("Normalized: %s -> %s", original, result)
        return result

    return model


def get_model_id_for_kiro(model: str) -> str:
    """Normalize model name and check hidden models lookup."""
    normalized = normalize_model_name(model)
    if normalized in HIDDEN_MODELS:
        internal = HIDDEN_MODELS[normalized]
        log.debug("Hidden model: %s -> %s", normalized, internal)
        return internal
    return normalized


def extract_model_family(model: str) -> Optional[str]:
    """Extract model family (haiku/sonnet/opus) from model name."""
    normalized = normalize_model_name(model)
    for family in ("haiku", "sonnet", "opus"):
        if family in normalized:
            return family
    return None


class ModelResolver:
    """4-layer model resolution: alias -> normalize -> cache -> hidden -> passthrough."""

    def __init__(self) -> None:
        self._cache: Dict[str, str] = {}

    def resolve(self, model: str) -> str:
        """Resolve model name through all layers."""
        # Layer 1: Check cache
        if model in self._cache:
            return self._cache[model]

        # Layer 2: Alias
        if model in MODEL_ALIASES:
            resolved = MODEL_ALIASES[model]
            self._cache[model] = resolved
            return resolved

        # Layer 3: Normalize
        normalized = normalize_model_name(model)

        # Layer 4: Hidden models
        if normalized in HIDDEN_MODELS:
            resolved = HIDDEN_MODELS[normalized]
            self._cache[model] = resolved
            return resolved

        # Passthrough
        self._cache[model] = normalized
        return normalized

    def clear_cache(self) -> None:
        self._cache.clear()
