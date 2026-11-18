import json

import httpx
import pytest
from mcp import Client

from dataverse_mcp.auth import StaticTokenProvider
from dataverse_mcp.client import DataverseClient
from dataverse_mcp.server import create_server
from tests.conftest import entity_set_route, make_settings

READ_TOOLS = {"list_tables", "describe_table", "query_records", "get_record"}
WRITE_TOOLS = {"create_record", "update_record"}


def build(**overrides):
    settings = make_settings(**overrides)
    http = httpx.AsyncClient(base_url=settings.api_base_url)
    return create_server(settings, DataverseClient(settings, StaticTokenProvider("t"), http))


@pytest.mark.usefixtures("api")
async def test_read_only_by_default_exposes_no_write_tools():
    async with Client(build()) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}

    assert set(tools) == READ_TOOLS
    assert all(t.annotations.read_only_hint for t in tools.values())


@pytest.mark.usefixtures("api")
async def test_write_tools_appear_only_when_enabled():
    async with Client(build(allow_writes=True)) as session:
        names = {t.name for t in (await session.list_tools()).tools}

    assert names == READ_TOOLS | WRITE_TOOLS


async def test_query_records_returns_structured_rows(api):
    entity_set_route(api, "account", "accounts")
    api.get("/accounts").respond(json={"value": [{"name": "Contoso", "accountid": "1"}]})

    async with Client(build()) as session:
        result = await session.call_tool("query_records", {"table": "account", "select": ["name"], "top": 3})

    assert not result.is_error
    assert result.structured_content["records"] == [{"name": "Contoso", "accountid": "1"}]


async def test_dataverse_errors_become_tool_errors(api):
    entity_set_route(api, "account", "accounts")
    api.get("/accounts").respond(
        400, json={"error": {"code": "0x80060888", "message": "Could not find a property named 'nmae'."}}
    )

    async with Client(build()) as session:
        result = await session.call_tool("query_records", {"table": "account", "select": ["nmae"]})

    assert result.is_error
    assert "Could not find a property named 'nmae'" in result.content[0].text


async def test_invalid_input_is_rejected_before_any_request(api):
    async with Client(build()) as session:
        result = await session.call_tool("get_record", {"table": "account", "record_id": "42"})

    assert result.is_error
    assert "GUID" in result.content[0].text
    assert not api.calls


async def test_update_tool_round_trip(api):
    record_id = "7d577253-3ef0-4a0a-bb7f-8335c2596e70"
    entity_set_route(api, "account", "accounts")
    api.patch(f"/accounts({record_id})").respond(204)

    async with Client(build(allow_writes=True)) as session:
        result = await session.call_tool(
            "update_record", {"table": "account", "record_id": record_id, "data": {"telephone1": "555"}}
        )

    assert result.structured_content == {"table": "account", "id": record_id, "updated": ["telephone1"]}


async def test_connection_resource_reports_limits_and_identity(api):
    api.get("/WhoAmI").respond(json={"UserId": "u", "BusinessUnitId": "b", "OrganizationId": "o"})

    async with Client(build()) as session:
        result = await session.read_resource("dataverse://connection")

    info = json.loads(result.contents[0].text)
    assert info["writes_enabled"] is False
    assert info["max_top"] == 50
    assert info["identity"]["user_id"] == "u"


@pytest.mark.usefixtures("api")
async def test_explore_table_prompt():
    async with Client(build()) as session:
        prompt = await session.get_prompt("explore_table", {"table": "account"})

    assert "describe_table on 'account'" in prompt.messages[0].content.text
