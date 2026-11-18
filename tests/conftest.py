from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from dataverse_mcp.auth import StaticTokenProvider
from dataverse_mcp.client import DataverseClient
from dataverse_mcp.config import Settings

ORG = "https://contoso-test.crm.dynamics.com"
API = f"{ORG}/api/data/v9.2"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "url": ORG,
        "tenant_id": "00000000-0000-0000-0000-000000000000",
        "client_id": "00000000-0000-0000-0000-000000000000",
        "client_secret": "not-a-real-secret",
        "default_top": 10,
        "max_top": 50,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def api() -> respx.MockRouter:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(settings: Settings, api: respx.MockRouter) -> AsyncIterator[DataverseClient]:
    http = httpx.AsyncClient(base_url=settings.api_base_url)
    async with DataverseClient(settings, StaticTokenProvider("test-token"), http) as dv:
        yield dv


def entity_set_route(api: respx.MockRouter, logical_name: str, entity_set: str) -> respx.Route:
    return api.get(f"/EntityDefinitions(LogicalName='{logical_name}')").respond(
        json={"EntitySetName": entity_set}
    )
