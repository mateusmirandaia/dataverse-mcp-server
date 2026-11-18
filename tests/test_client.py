import json

import httpx
import pytest

from dataverse_mcp import odata
from dataverse_mcp.auth import StaticTokenProvider
from dataverse_mcp.client import DataverseClient
from dataverse_mcp.errors import DataverseError, InvalidQueryError, WritesDisabledError
from tests.conftest import entity_set_route, make_settings

RECORD_ID = "7d577253-3ef0-4a0a-bb7f-8335c2596e70"


async def test_query_sends_odata_headers_and_page_size(client, api):
    entity_set_route(api, "account", "accounts")
    route = api.get("/accounts").respond(json={"value": [{"name": "Contoso"}]})

    result = await client.query("account", select=["name"], filter="statecode eq 0", orderby="name")

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.headers["OData-MaxVersion"] == "4.0"
    assert "odata.maxpagesize=10" in request.headers["Prefer"]
    assert odata.FORMATTED_VALUE in request.headers["Prefer"]
    assert request.url.params["$select"] == "name"
    assert request.url.params["$filter"] == "statecode eq 0"
    assert "$top" not in request.url.params
    assert result == {"table": "account", "count": 1, "more_records": False, "records": [{"name": "Contoso"}]}


async def test_query_clamps_page_size_to_configured_maximum(client, api):
    entity_set_route(api, "account", "accounts")
    route = api.get("/accounts").respond(json={"value": []})

    await client.query("account", top=10_000)

    assert "odata.maxpagesize=50" in route.calls.last.request.headers["Prefer"]


async def test_query_reports_truncation_instead_of_following_next_link(client, api):
    entity_set_route(api, "account", "accounts")
    route = api.get("/accounts").respond(
        json={
            "value": [{"name": "A"}],
            "@odata.nextLink": f"{client.settings.api_base_url}accounts?$skiptoken=x",
        }
    )

    result = await client.query("account", top=1)

    assert route.call_count == 1
    assert result["more_records"] is True
    assert "Narrow the filter" in result["note"]


async def test_entity_set_name_is_cached(client, api):
    metadata = entity_set_route(api, "opportunity", "opportunities")
    api.get("/opportunities").respond(json={"value": []})

    await client.query("opportunity")
    await client.query("opportunity")

    assert metadata.call_count == 1


async def test_invalid_table_name_never_reaches_the_network(client, api):
    with pytest.raises(InvalidQueryError):
        await client.query("accounts?$top=5000")
    assert not api.calls


async def test_get_record_normalises_id(client, api):
    entity_set_route(api, "contact", "contacts")
    route = api.get(f"/contacts({RECORD_ID})").respond(json={"fullname": "Ana Souza", "@odata.etag": "x"})

    record = await client.get_record("contact", "{" + RECORD_ID.upper() + "}", select="fullname")

    assert route.called
    assert record == {"fullname": "Ana Souza"}


async def test_list_tables_hides_system_tables_by_default(client, api):
    api.get("/EntityDefinitions").respond(
        json={
            "value": [
                _table("account", "accounts", "Account", customizable=True),
                _table("new_project", "new_projects", "Project", customizable=True, custom=True),
                _table("asyncoperation", "asyncoperations", "System Job", customizable=False),
                _table("accountleads", "accountleadscollection", None, customizable=True, intersect=True),
            ]
        }
    )

    tables = await client.list_tables()
    assert [t["logical_name"] for t in tables] == ["account", "new_project"]

    everything = await client.list_tables(include_system=True)
    assert len(everything) == 4

    assert [t["logical_name"] for t in await client.list_tables(search="proj")] == ["new_project"]


async def test_list_tables_primes_entity_set_cache(client, api):
    api.get("/EntityDefinitions").respond(
        json={"value": [_table("new_project", "new_projectses", "Project", customizable=True)]}
    )
    query = api.get("/new_projectses").respond(json={"value": []})

    await client.list_tables()
    await client.query("new_project")

    assert query.called


async def test_describe_table_hides_shadow_columns_and_explains_lookups(client, api):
    api.get("/EntityDefinitions(LogicalName='account')").respond(
        json={
            "LogicalName": "account",
            "EntitySetName": "accounts",
            "PrimaryIdAttribute": "accountid",
            "PrimaryNameAttribute": "name",
            "DisplayName": {"UserLocalizedLabel": {"Label": "Account"}},
            "Attributes": [
                _attribute("name", "StringType", required="ApplicationRequired"),
                _attribute("primarycontactid", "LookupType"),
                _attribute("primarycontactidname", "StringType", attribute_of="primarycontactid"),
                _attribute("hidden", "StringType", readable=False),
            ],
        }
    )

    table = await client.describe_table("account")

    assert table["entity_set"] == "accounts"
    assert table["display_name"] == "Account"
    columns = {c["name"]: c for c in table["columns"]}
    assert set(columns) == {"name", "primarycontactid"}
    assert columns["name"]["required"] is True
    assert columns["name"]["type"] == "String"
    assert columns["primarycontactid"]["query_as"] == "_primarycontactid_value"


async def test_create_record_returns_id_from_header(api):
    client = _writable_client()
    entity_set_route(api, "account", "accounts")
    route = api.post("/accounts").respond(
        204, headers={"OData-EntityId": f"{client.settings.api_base_url}accounts({RECORD_ID})"}
    )

    assert await client.create_record("account", {"name": "Contoso"}) == RECORD_ID
    assert json.loads(route.calls.last.request.content) == {"name": "Contoso"}


async def test_update_record_refuses_to_upsert(api):
    client = _writable_client()
    entity_set_route(api, "account", "accounts")
    route = api.patch(f"/accounts({RECORD_ID})").respond(204)

    await client.update_record("account", RECORD_ID, {"name": "Contoso Ltd"})

    assert route.calls.last.request.headers["If-Match"] == "*"


async def test_writes_are_blocked_when_disabled(client, api):
    with pytest.raises(WritesDisabledError):
        await client.create_record("account", {"name": "x"})
    with pytest.raises(WritesDisabledError):
        await client.update_record("account", RECORD_ID, {"name": "x"})
    assert not api.calls


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "access token"),
        (403, "security role"),
        (404, "list_tables"),
        (500, "HTTP 500"),
    ],
)
async def test_error_payload_is_mapped_to_actionable_message(client, api, status, expected):
    entity_set_route(api, "account", "accounts")
    api.get("/accounts").respond(
        status,
        json={
            "error": {"code": "0x80040220", "message": "Principal user is missing prvReadAccount privilege."}
        },
    )

    with pytest.raises(DataverseError) as excinfo:
        await client.query("account")

    error = excinfo.value
    assert error.status == status
    assert error.code == "0x80040220"
    assert expected in str(error)
    assert "prvReadAccount" in str(error)


async def test_throttling_reports_retry_after(client, api):
    entity_set_route(api, "account", "accounts")
    api.get("/accounts").respond(
        429, headers={"Retry-After": "12"}, json={"error": {"message": "Rate limit"}}
    )

    with pytest.raises(DataverseError, match="Retry after 12 seconds"):
        await client.query("account")


async def test_non_json_error_body(client, api):
    entity_set_route(api, "account", "accounts")
    api.get("/accounts").respond(502, text="<html>Bad Gateway</html>")

    with pytest.raises(DataverseError, match="Bad Gateway"):
        await client.query("account")


async def test_timeout_is_reported_cleanly(client, api):
    entity_set_route(api, "account", "accounts")
    api.get("/accounts").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(DataverseError, match="did not respond within 30 seconds"):
        await client.query("account")


def _writable_client() -> DataverseClient:
    settings = make_settings(allow_writes=True)
    return DataverseClient(
        settings, StaticTokenProvider("test-token"), httpx.AsyncClient(base_url=settings.api_base_url)
    )


def _table(logical_name, entity_set, label, *, customizable, custom=False, intersect=False):
    return {
        "LogicalName": logical_name,
        "EntitySetName": entity_set,
        "DisplayName": {"UserLocalizedLabel": {"Label": label} if label else None},
        "IsCustomEntity": custom,
        "IsCustomizable": {"Value": customizable},
        "IsIntersect": intersect,
    }


def _attribute(name, type_name, *, required="None", attribute_of=None, readable=True):
    return {
        "LogicalName": name,
        "AttributeTypeName": {"Value": type_name},
        "DisplayName": {"UserLocalizedLabel": None},
        "RequiredLevel": {"Value": required},
        "IsValidForRead": readable,
        "IsValidForCreate": True,
        "IsValidForUpdate": True,
        "AttributeOf": attribute_of,
    }
