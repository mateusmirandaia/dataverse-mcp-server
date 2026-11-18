import asyncio
from typing import ClassVar

import pytest

from dataverse_mcp import auth
from dataverse_mcp.errors import AuthenticationError
from tests.conftest import make_settings


class FakeConfidentialClient:
    """Stands in for msal.ConfidentialClientApplication, which talks to Entra ID on use."""

    instances: ClassVar[list["FakeConfidentialClient"]] = []
    result: ClassVar[dict] = {"access_token": "token-1", "expires_in": 3599}

    def __init__(self, client_id, client_credential, authority):
        self.client_id = client_id
        self.authority = authority
        self.scopes_seen: list[list[str]] = []
        FakeConfidentialClient.instances.append(self)

    def acquire_token_for_client(self, scopes):
        self.scopes_seen.append(scopes)
        return self.result


@pytest.fixture
def fake_msal(monkeypatch):
    FakeConfidentialClient.instances.clear()
    monkeypatch.setattr(auth.msal, "ConfidentialClientApplication", FakeConfidentialClient)
    return FakeConfidentialClient


async def test_requests_token_for_environment_scope(fake_msal):
    provider = auth.MsalTokenProvider(make_settings())

    assert await provider.get_token() == "token-1"

    app = fake_msal.instances[0]
    assert app.authority == "https://login.microsoftonline.com/00000000-0000-0000-0000-000000000000"
    assert app.scopes_seen == [["https://contoso-test.crm.dynamics.com/.default"]]


async def test_concurrent_callers_are_serialised(fake_msal):
    provider = auth.MsalTokenProvider(make_settings())
    tokens = await asyncio.gather(*(provider.get_token() for _ in range(5)))
    assert tokens == ["token-1"] * 5


async def test_client_app_is_built_lazily_and_once(fake_msal):
    provider = auth.MsalTokenProvider(make_settings())
    assert fake_msal.instances == []

    await provider.get_token()
    await provider.get_token()

    assert len(fake_msal.instances) == 1


async def test_failure_is_reported_without_trace_ids(fake_msal, monkeypatch):
    monkeypatch.setattr(
        FakeConfidentialClient,
        "result",
        {
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid client secret provided.\r\nTrace ID: abc",
        },
        raising=False,
    )
    provider = auth.MsalTokenProvider(make_settings())

    with pytest.raises(AuthenticationError) as excinfo:
        await provider.get_token()

    message = str(excinfo.value)
    assert "invalid_client" in message
    assert "AADSTS7000215" in message
    assert "Trace ID" not in message


async def test_unknown_tenant_becomes_authentication_error(monkeypatch):
    def refuse(**_kwargs):
        raise ValueError("Unable to get authority configuration")

    monkeypatch.setattr(auth.msal, "ConfidentialClientApplication", refuse)
    provider = auth.MsalTokenProvider(make_settings())

    with pytest.raises(AuthenticationError, match="invalid_authority"):
        await provider.get_token()
