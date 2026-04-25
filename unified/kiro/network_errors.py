"""Network error classification for Kiro API calls.

Ported from kiro-gateway network_errors.py.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from enum import Enum
from typing import List

import httpx


class ErrorCategory(Enum):
    DNS_RESOLUTION = "dns_resolution"
    CONNECTION_REFUSED = "connection_refused"
    CONNECTION_RESET = "connection_reset"
    NETWORK_UNREACHABLE = "network_unreachable"
    TIMEOUT_CONNECT = "timeout_connect"
    TIMEOUT_READ = "timeout_read"
    SSL_ERROR = "ssl_error"
    PROXY_ERROR = "proxy_error"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    UNKNOWN = "unknown"


@dataclass
class NetworkErrorInfo:
    category: ErrorCategory
    user_message: str
    troubleshooting_steps: List[str] = field(default_factory=list)
    technical_details: str = ""
    is_retryable: bool = True
    suggested_http_code: int = 502


def classify_network_error(error: Exception) -> NetworkErrorInfo:
    """Classify a network error into a category with user-friendly info."""
    if isinstance(error, httpx.ConnectError):
        return _classify_connect_error(error)
    if isinstance(error, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return _classify_timeout_error(error)
    if isinstance(error, httpx.TimeoutException):
        return _classify_timeout_error(error)
    if isinstance(error, httpx.TooManyRedirects):
        return NetworkErrorInfo(
            category=ErrorCategory.TOO_MANY_REDIRECTS,
            user_message="Too many redirects while connecting to Kiro API.",
            troubleshooting_steps=["Check API endpoint URL", "Check proxy configuration"],
            technical_details=str(error),
            is_retryable=False,
            suggested_http_code=502,
        )
    return NetworkErrorInfo(
        category=ErrorCategory.UNKNOWN,
        user_message="An unexpected network error occurred.",
        troubleshooting_steps=["Check network connectivity", "Try again later"],
        technical_details=str(error),
        is_retryable=True,
        suggested_http_code=502,
    )


def _classify_connect_error(error: httpx.ConnectError) -> NetworkErrorInfo:
    cause = error.__cause__
    error_str = str(error).lower()

    # DNS resolution failure
    if isinstance(cause, socket.gaierror) or "getaddrinfo" in error_str or "name or service not known" in error_str:
        return NetworkErrorInfo(
            category=ErrorCategory.DNS_RESOLUTION,
            user_message="Cannot resolve Kiro API hostname. DNS lookup failed.",
            troubleshooting_steps=[
                "Check your internet connection",
                "Check DNS settings",
                "Try using a different DNS server (e.g. 8.8.8.8)",
                "If using VPN, check VPN connection",
            ],
            technical_details=str(error),
            is_retryable=True,
            suggested_http_code=502,
        )

    # Connection refused
    if "connection refused" in error_str or "errno 111" in error_str:
        return NetworkErrorInfo(
            category=ErrorCategory.CONNECTION_REFUSED,
            user_message="Connection to Kiro API was refused.",
            troubleshooting_steps=[
                "Check if the API endpoint is correct",
                "Check firewall settings",
                "The service may be temporarily down",
            ],
            technical_details=str(error),
            is_retryable=True,
            suggested_http_code=502,
        )

    # Connection reset
    if "connection reset" in error_str or "errno 104" in error_str:
        return NetworkErrorInfo(
            category=ErrorCategory.CONNECTION_RESET,
            user_message="Connection to Kiro API was reset.",
            troubleshooting_steps=[
                "Check network stability",
                "Try again in a few seconds",
                "If using proxy, check proxy health",
            ],
            technical_details=str(error),
            is_retryable=True,
            suggested_http_code=502,
        )

    # Network unreachable
    if "network is unreachable" in error_str or "errno 101" in error_str:
        return NetworkErrorInfo(
            category=ErrorCategory.NETWORK_UNREACHABLE,
            user_message="Network is unreachable. Cannot connect to Kiro API.",
            troubleshooting_steps=[
                "Check your internet connection",
                "Check routing table",
                "If using VPN, reconnect",
            ],
            technical_details=str(error),
            is_retryable=True,
            suggested_http_code=502,
        )

    # SSL errors
    if "ssl" in error_str or "certificate" in error_str:
        return NetworkErrorInfo(
            category=ErrorCategory.SSL_ERROR,
            user_message="SSL/TLS error connecting to Kiro API.",
            troubleshooting_steps=[
                "Check system clock (certificate validation requires correct time)",
                "Update CA certificates",
                "If using proxy, check proxy SSL configuration",
            ],
            technical_details=str(error),
            is_retryable=False,
            suggested_http_code=502,
        )

    return NetworkErrorInfo(
        category=ErrorCategory.UNKNOWN,
        user_message="Failed to connect to Kiro API.",
        troubleshooting_steps=["Check network connectivity", "Try again later"],
        technical_details=str(error),
        is_retryable=True,
        suggested_http_code=502,
    )


def _classify_timeout_error(error: Exception) -> NetworkErrorInfo:
    if isinstance(error, httpx.ConnectTimeout):
        return NetworkErrorInfo(
            category=ErrorCategory.TIMEOUT_CONNECT,
            user_message="Connection to Kiro API timed out.",
            troubleshooting_steps=[
                "Check network connectivity",
                "The API may be experiencing high load",
                "Try again in a few seconds",
            ],
            technical_details=str(error),
            is_retryable=True,
            suggested_http_code=504,
        )

    if isinstance(error, httpx.ReadTimeout):
        return NetworkErrorInfo(
            category=ErrorCategory.TIMEOUT_READ,
            user_message="Kiro API response timed out (read timeout).",
            troubleshooting_steps=[
                "The model may need more time to respond",
                "Try a simpler prompt",
                "Try again later",
            ],
            technical_details=str(error),
            is_retryable=True,
            suggested_http_code=504,
        )

    return NetworkErrorInfo(
        category=ErrorCategory.TIMEOUT_READ,
        user_message="Request to Kiro API timed out.",
        troubleshooting_steps=["Check network", "Try again"],
        technical_details=str(error),
        is_retryable=True,
        suggested_http_code=504,
    )


def format_error_for_user(info: NetworkErrorInfo, api_format: str = "openai") -> dict:
    """Format error info as an API error response."""
    if api_format == "openai":
        return {
            "error": {
                "message": info.user_message,
                "type": "proxy_error",
                "code": info.category.value,
            }
        }
    return {
        "type": "error",
        "error": {
            "type": info.category.value,
            "message": info.user_message,
        },
    }


def get_short_error_message(info: NetworkErrorInfo) -> str:
    """Single-line error message for logging."""
    return f"[{info.category.value}] {info.user_message}"
