"""Authentication middleware — API key verification and admin auth."""

from __future__ import annotations

from fastapi import Request, HTTPException

from . import database as db
from .config import ADMIN_PASSWORD


async def verify_api_key(request: Request) -> dict:
    """Extract and verify the API key from the Authorization header.

    Returns the api_key row dict on success, raises 401 on failure.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty API key")

    key_row = await db.verify_api_key(token)
    if key_row is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return key_row


async def verify_admin(request: Request) -> bool:
    """Verify admin access via X-Admin-Password header or cookie.

    Returns True on success, raises 403 on failure.
    """
    password = (
        request.headers.get("X-Admin-Password", "")
        or request.cookies.get("admin_password", "")
        or request.query_params.get("password", "")
        or request.query_params.get("token", "")
    )
    if not password:
        # Also check Authorization: Bearer for admin
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            password = auth[7:].strip()

    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Invalid admin password")

    return True
