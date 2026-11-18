"""Service-to-service authentication against Entra ID using MSAL."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import msal

from dataverse_mcp.config import Settings
from dataverse_mcp.errors import AuthenticationError


class TokenProvider(Protocol):
    async def get_token(self) -> str: ...


class MsalTokenProvider:
    """Client-credentials flow for a Dataverse application user.

    MSAL keeps tokens in an in-memory cache and `acquire_token_for_client`
    consults it before calling Entra ID, so repeated calls are cheap and a new
    token is only requested close to expiry.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._scopes = [settings.scope]
        self._app: msal.ConfidentialClientApplication | None = None
        # One refresh at a time: when the token expires, concurrent tool calls
        # would otherwise all race to Entra ID for a new one.
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            # MSAL is synchronous and does network I/O (including authority
            # discovery when the app is built), so all of it runs off the event loop.
            result = await asyncio.to_thread(self._acquire)
        token = result.get("access_token")
        if not token:
            raise AuthenticationError(_describe_failure(result))
        return token

    def _acquire(self) -> dict[str, Any]:
        try:
            if self._app is None:
                # Built lazily so the server can start, list its tools and report a
                # readable error even when Entra ID is unreachable or the tenant is wrong.
                self._app = msal.ConfidentialClientApplication(
                    client_id=self._settings.client_id,
                    client_credential=self._settings.client_secret.get_secret_value(),
                    authority=f"https://login.microsoftonline.com/{self._settings.tenant_id}",
                )
            return self._app.acquire_token_for_client(self._scopes)
        except ValueError as exc:
            # MSAL raises ValueError for an unknown tenant or unreachable authority.
            return {"error": "invalid_authority", "error_description": str(exc)}


def _describe_failure(result: dict[str, object]) -> str:
    error = result.get("error", "unknown_error")
    description = str(result.get("error_description", "")).split("\r\n")[0]
    return f"Could not obtain a Dataverse token from Entra ID ({error}). {description}".strip()


class StaticTokenProvider:
    """Fixed token, for tests and for local experiments with a token from the Azure CLI."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def get_token(self) -> str:
        return self._token
