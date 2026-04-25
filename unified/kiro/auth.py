"""Authentication manager for Kiro API — adapted for unified proxy.

Simplified from kiro-gateway auth.py:
- No file/SQLite credential loading (tokens come from our DB)
- No credential saving (our DB handles persistence)
- Supports both Kiro Desktop Auth and AWS SSO OIDC refresh
- Thread-safe refresh using asyncio.Lock
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional

import httpx

from .config import TOKEN_REFRESH_THRESHOLD, get_kiro_refresh_url, get_kiro_api_host, get_kiro_q_host
from .utils import get_machine_fingerprint

log = logging.getLogger("unified.kiro.auth")


class AuthType(Enum):
    KIRO_DESKTOP = "kiro_desktop"
    AWS_SSO_OIDC = "aws_sso_oidc"


class KiroAuthManager:
    """Manages token lifecycle for a single Kiro account.

    Created per-request from DB-stored tokens. Handles:
    - Token expiry checking
    - Automatic refresh via Kiro Desktop Auth or AWS SSO OIDC
    - Thread-safe refresh with asyncio.Lock

    Usage:
        auth = KiroAuthManager(
            access_token="...",
            refresh_token="...",
            profile_arn="arn:aws:...",
            region="us-east-1",
        )
        token = await auth.get_access_token()
    """

    def __init__(
        self,
        access_token: str = "",
        refresh_token: str = "",
        profile_arn: str = "",
        region: str = "us-east-1",
        expires_at: Optional[datetime] = None,
        # AWS SSO OIDC fields (optional)
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ):
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._profile_arn = profile_arn
        self._region = region
        self._expires_at = expires_at
        self._client_id = client_id
        self._client_secret = client_secret
        self._lock = asyncio.Lock()
        self._fingerprint = get_machine_fingerprint()

        # Detect auth type
        if client_id and client_secret:
            self._auth_type = AuthType.AWS_SSO_OIDC
        else:
            self._auth_type = AuthType.KIRO_DESKTOP

        # Compute API hosts
        self._refresh_url = get_kiro_refresh_url(region)
        self._api_host = get_kiro_api_host(region)
        self._q_host = get_kiro_q_host(region)

        # Track if tokens were updated (caller should persist back to DB)
        self.tokens_updated = False
        self._new_access_token: Optional[str] = None
        self._new_refresh_token: Optional[str] = None
        self._new_expires_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Token state
    # ------------------------------------------------------------------

    def is_token_expiring_soon(self) -> bool:
        if not self._expires_at:
            return True
        now = datetime.now(timezone.utc)
        threshold = now.timestamp() + TOKEN_REFRESH_THRESHOLD
        return self._expires_at.timestamp() <= threshold

    def is_token_expired(self) -> bool:
        if not self._expires_at:
            return True
        return datetime.now(timezone.utc) >= self._expires_at

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    async def _refresh_token_request(self) -> None:
        if self._auth_type == AuthType.AWS_SSO_OIDC:
            await self._refresh_token_aws_sso_oidc()
        else:
            await self._refresh_token_kiro_desktop()

    async def _refresh_token_kiro_desktop(self) -> None:
        if not self._refresh_token:
            raise ValueError("Refresh token is not set")

        log.info("Refreshing Kiro token via Desktop Auth...")
        payload = {"refreshToken": self._refresh_token}
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"KiroIDE-0.7.45-{self._fingerprint}",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(self._refresh_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        new_access = data.get("accessToken")
        new_refresh = data.get("refreshToken")
        expires_in = data.get("expiresIn", 3600)
        new_profile = data.get("profileArn")

        if not new_access:
            raise ValueError(f"Response missing accessToken: {data}")

        self._access_token = new_access
        if new_refresh:
            self._refresh_token = new_refresh
        if new_profile:
            self._profile_arn = new_profile
        self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

        # Mark for caller to persist
        self.tokens_updated = True
        self._new_access_token = self._access_token
        self._new_refresh_token = self._refresh_token
        self._new_expires_at = self._expires_at

        log.info("Token refreshed via Desktop Auth, expires: %s", self._expires_at.isoformat())

    async def _refresh_token_aws_sso_oidc(self) -> None:
        if not self._refresh_token:
            raise ValueError("Refresh token is not set")
        if not self._client_id or not self._client_secret:
            raise ValueError("Client ID/secret required for AWS SSO OIDC")

        log.info("Refreshing Kiro token via AWS SSO OIDC...")
        from .config import get_aws_sso_oidc_url
        url = get_aws_sso_oidc_url(self._region)

        payload = {
            "grantType": "refresh_token",
            "clientId": self._client_id,
            "clientSecret": self._client_secret,
            "refreshToken": self._refresh_token,
        }
        headers = {"Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code != 200:
                log.error("AWS SSO OIDC refresh failed: %d %s", response.status_code, response.text)
                response.raise_for_status()
            result = response.json()

        new_access = result.get("accessToken")
        new_refresh = result.get("refreshToken")
        expires_in = result.get("expiresIn", 3600)

        if not new_access:
            raise ValueError(f"AWS SSO OIDC response missing accessToken: {result}")

        self._access_token = new_access
        if new_refresh:
            self._refresh_token = new_refresh
        self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

        self.tokens_updated = True
        self._new_access_token = self._access_token
        self._new_refresh_token = self._refresh_token
        self._new_expires_at = self._expires_at

        log.info("Token refreshed via AWS SSO OIDC, expires: %s", self._expires_at.isoformat())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_access_token(self) -> str:
        """Get a valid access token, refreshing if needed."""
        async with self._lock:
            if self._access_token and not self.is_token_expiring_soon():
                return self._access_token
            try:
                await self._refresh_token_request()
            except Exception:
                # If refresh fails but token isn't expired yet, use it
                if self._access_token and not self.is_token_expired():
                    log.warning("Token refresh failed, using existing token until expiry")
                    return self._access_token
                raise
            if not self._access_token:
                raise ValueError("Failed to obtain access token")
            return self._access_token

    async def force_refresh(self) -> str:
        """Force a token refresh."""
        async with self._lock:
            await self._refresh_token_request()
            return self._access_token

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def profile_arn(self) -> str:
        return self._profile_arn or ""

    @property
    def region(self) -> str:
        return self._region

    @property
    def api_host(self) -> str:
        return self._api_host

    @property
    def q_host(self) -> str:
        return self._q_host

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def auth_type(self) -> AuthType:
        return self._auth_type

    def get_updated_tokens(self) -> dict:
        """Return updated tokens for DB persistence (call after request)."""
        if not self.tokens_updated:
            return {}
        result = {}
        if self._new_access_token:
            result["kiro_access_token"] = self._new_access_token
        if self._new_refresh_token:
            result["kiro_refresh_token"] = self._new_refresh_token
        if self._new_expires_at:
            result["kiro_expires_at"] = self._new_expires_at.isoformat()
        return result
