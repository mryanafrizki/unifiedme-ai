from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

from unified.gumloop.auth import GumloopAuth
from unified.gumloop.client import send_chat, update_gummie_config
from unified.gumloop.turnstile import TurnstileSolver


def load_config(config_path: Path) -> dict[str, Any]:
    return json.loads(config_path.read_text(encoding="utf-8"))


def build_auth(config: dict[str, Any]) -> GumloopAuth:
    return GumloopAuth(
        refresh_token=config.get("refresh_token", ""),
        user_id=config.get("user_id", ""),
        id_token=config.get("id_token", ""),
    )


async def prepare_gummie(config: dict[str, Any], system_prompt: str) -> tuple[GumloopAuth, TurnstileSolver, str, str]:
    auth = build_auth(config)
    await auth.refresh()
    solver = TurnstileSolver(config.get("captcha_api_key", ""))
    gummie_id = config.get("gummie_id", "")
    model = config.get("model", "gl-claude-opus-4-7").removeprefix("gl-")
    await update_gummie_config(
        gummie_id=gummie_id,
        auth=auth,
        model_name=model,
        system_prompt=system_prompt,
    )
    return auth, solver, gummie_id, model


async def stream_gumloop_events(
    config: dict[str, Any],
    messages: list[dict[str, Any]],
    system_prompt: str,
    interaction_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    auth, solver, gummie_id, _model = await prepare_gummie(config, system_prompt)
    async for event in send_chat(
        gummie_id=gummie_id,
        messages=messages,
        auth=auth,
        turnstile=solver,
        interaction_id=interaction_id,
    ):
        yield event
