"""Exceptions of the Trading212 web API client."""

from __future__ import annotations

from typing import Any


class Trading212Error(Exception):
    """Base class for all errors raised by this package."""


class ConfigError(Trading212Error):
    """Configuration is incomplete or invalid."""


class NetworkError(Trading212Error):
    """Server not reachable (DNS, proxy, timeout, ...)."""


class ApiError(Trading212Error):
    """The server returned an error.

    Trading212 usually answers with
    ``{"code": "BusinessException", "context": {"type": "...", ...}}``.
    """

    def __init__(self, status: int, code: str | None, error_type: str | None,
                 context: dict[str, Any] | None, url: str, body: str = "") -> None:
        self.status = status
        self.code = code
        self.error_type = error_type
        self.context = context or {}
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} at {url}: {code or '-'} / {error_type or '-'}")


class AuthenticationError(ApiError):
    """Login failed (wrong password, invalid session, ...)."""


class TwoFactorRequired(AuthenticationError):
    """The server asks for a 2FA code, but none was provided."""


class InvalidSession(AuthenticationError):
    """The session has expired or is invalid."""
