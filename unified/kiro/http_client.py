"""HTTP client for Kiro API with retry logic.

Ported from kiro-gateway http_client.py.
Handles 403 (token refresh), 429 (backoff), 5xx (backoff), timeouts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from .config import MAX_RETRIES, BASE_RETRY_DELAY, FIRST_TOKEN_MAX_RETRIES, STREAMING_READ_TIMEOUT
from .auth import KiroAuthManager
from .utils import get_kiro_headers
from .network_errors import classify_network_error, get_short_error_message, NetworkErrorInfo

log = logging.getLogger("unified.kiro.http_client")


class KiroHttpClient:
    """HTTP client for Kiro API with automatic retry.

    - 403: refreshes token and retries
    - 429: exponential backoff
    - 5xx: exponential backoff
    - Timeouts: exponential backoff
    """

    def __init__(
        self,
        auth_manager: KiroAuthManager,
        shared_client: Optional[httpx.AsyncClient] = None,
    ):
        self.auth_manager = auth_manager
        self._shared_client = shared_client
        self._owns_client = shared_client is None
        self.client: Optional[httpx.AsyncClient] = shared_client

    async def _get_client(self, stream: bool = False) -> httpx.AsyncClient:
        if self._shared_client is not None:
            return self._shared_client
        if self.client is None or self.client.is_closed:
            if stream:
                timeout_config = httpx.Timeout(
                    connect=30.0,
                    read=STREAMING_READ_TIMEOUT,
                    write=30.0,
                    pool=30.0,
                )
            else:
                timeout_config = httpx.Timeout(timeout=300.0)
            self.client = httpx.AsyncClient(timeout=timeout_config, follow_redirects=True)
        return self.client

    async def close(self) -> None:
        if not self._owns_client:
            return
        if self.client and not self.client.is_closed:
            try:
                await self.client.aclose()
            except Exception as e:
                log.warning("Error closing HTTP client: %s", e)

    async def request_with_retry(
        self,
        method: str,
        url: str,
        json_data: Optional[dict] = None,
        params: Optional[dict] = None,
        stream: bool = False,
    ) -> httpx.Response:
        """Make a request with automatic retry on transient errors."""
        max_retries = FIRST_TOKEN_MAX_RETRIES if stream else MAX_RETRIES
        client = await self._get_client(stream=stream)
        last_error: Optional[Exception] = None
        last_error_info: Optional[NetworkErrorInfo] = None
        last_response: Optional[httpx.Response] = None

        for attempt in range(max_retries):
            try:
                token = await self.auth_manager.get_access_token()
                headers = get_kiro_headers(self.auth_manager, token)
                request_kwargs: dict = {"headers": headers}
                if json_data is not None:
                    request_kwargs["json"] = json_data
                if params is not None:
                    request_kwargs["params"] = params

                if stream:
                    headers["Connection"] = "close"
                    req = client.build_request(method, url, **request_kwargs)
                    response = await client.send(req, stream=True)
                else:
                    response = await client.request(method, url, **request_kwargs)

                if response.status_code == 200:
                    return response

                if response.status_code == 403:
                    log.warning("403 received, refreshing token (attempt %d/%d)", attempt + 1, MAX_RETRIES)
                    await self.auth_manager.force_refresh()
                    continue

                if response.status_code == 429:
                    last_response = response
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    log.warning("429 rate limited, waiting %.1fs (attempt %d/%d)", delay, attempt + 1, max_retries)
                    await asyncio.sleep(delay)
                    continue

                if 500 <= response.status_code < 600:
                    last_response = response
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    log.warning("HTTP %d, waiting %.1fs (attempt %d/%d)", response.status_code, delay, attempt + 1, max_retries)
                    await asyncio.sleep(delay)
                    continue

                # Other status codes (400, 404, etc.) — return as-is
                return response

            except httpx.TimeoutException as e:
                last_error = e
                error_info = classify_network_error(e)
                last_error_info = error_info
                short_msg = get_short_error_message(error_info)
                if error_info.is_retryable and attempt < max_retries - 1:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    log.warning("%s — waiting %.1fs (attempt %d/%d)", short_msg, delay, attempt + 1, max_retries)
                    await asyncio.sleep(delay)
                else:
                    log.error("%s — no more retries (attempt %d/%d)", short_msg, attempt + 1, max_retries)
                    if not error_info.is_retryable:
                        break

            except httpx.RequestError as e:
                last_error = e
                error_info = classify_network_error(e)
                last_error_info = error_info
                short_msg = get_short_error_message(error_info)
                if error_info.is_retryable and attempt < max_retries - 1:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    log.warning("%s — waiting %.1fs (attempt %d/%d)", short_msg, delay, attempt + 1, max_retries)
                    await asyncio.sleep(delay)
                else:
                    log.error("%s — no more retries (attempt %d/%d)", short_msg, attempt + 1, max_retries)
                    if not error_info.is_retryable:
                        break

        # Exhausted retries
        if last_response is not None:
            log.warning("Retries exhausted for HTTP %d, returning last response", last_response.status_code)
            return last_response

        if last_error_info:
            error_message = last_error_info.user_message
            if last_error_info.troubleshooting_steps:
                error_message += "\n\nTroubleshooting:\n"
                for i, step in enumerate(last_error_info.troubleshooting_steps, 1):
                    error_message += f"{i}. {step}\n"
            raise httpx.RequestError(error_message.strip())

        raise httpx.RequestError(f"Request failed after {max_retries} attempts")

    async def __aenter__(self) -> KiroHttpClient:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
