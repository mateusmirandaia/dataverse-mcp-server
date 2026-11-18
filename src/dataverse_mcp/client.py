"""Async client for the parts of the Dataverse Web API this server exposes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from dataverse_mcp import odata
from dataverse_mcp.auth import TokenProvider
from dataverse_mcp.config import Settings
from dataverse_mcp.errors import DataverseError, WritesDisabledError

# Only the two annotations we actually use. Asking for "*" roughly doubles the
# payload with values no model needs (associated navigation properties etc.).
_ANNOTATIONS = f'odata.include-annotations="{odata.FORMATTED_VALUE},{odata.LOOKUP_LOGICAL_NAME}"'

_TABLE_COLUMNS = "LogicalName,EntitySetName,DisplayName,IsCustomEntity,IsCustomizable,IsIntersect"
_ATTRIBUTE_COLUMNS = (
    "LogicalName,AttributeTypeName,DisplayName,RequiredLevel,"
    "IsValidForRead,IsValidForCreate,IsValidForUpdate,AttributeOf,IsPrimaryId,IsPrimaryName"
)
_LOOKUP_TYPES = {"LookupType", "CustomerType", "OwnerType"}

_STATUS_HINTS = {
    401: "Dataverse rejected the access token. Check the tenant, client id and secret.",
    403: (
        "The application user is not allowed to do this. Check the security role assigned "
        "to it in the Power Platform admin center."
    ),
    404: "Not found. Check the table name with list_tables, and the record id.",
    412: "The record changed or does not exist (precondition failed).",
}


class DataverseClient:
    def __init__(
        self,
        settings: Settings,
        tokens: TokenProvider,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._tokens = tokens
        self._http = http or httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=httpx.Timeout(settings.timeout_seconds),
        )
        self._entity_sets: dict[str, str] = {}
        self._tables: list[dict[str, Any]] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> DataverseClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- metadata -----------------------------------------------------------

    async def list_tables(
        self, search: str | None = None, include_system: bool = False
    ) -> list[dict[str, Any]]:
        """Tables visible to the application user, most useful first.

        By default only customizable, non-intersect tables are returned. That
        drops several hundred internal tables (plugin traces, async operations,
        N:N link tables) that would otherwise drown the ones people care about.
        """
        tables = await self._load_tables()
        if not include_system:
            tables = [t for t in tables if t["customizable"] and not t["intersect"]]
        if search:
            needle = search.strip().lower()
            tables = [
                t
                for t in tables
                if needle in t["logical_name"] or needle in (t["display_name"] or "").lower()
            ]
        return [
            {k: t[k] for k in ("logical_name", "entity_set", "display_name", "is_custom")} for t in tables
        ]

    async def describe_table(self, table: str) -> dict[str, Any]:
        logical_name = odata.validate_identifier(table, "table name")
        data = await self._get_json(
            f"EntityDefinitions(LogicalName={odata.quote_string(logical_name)})",
            params={
                "$select": "LogicalName,EntitySetName,PrimaryIdAttribute,PrimaryNameAttribute,DisplayName",
                "$expand": f"Attributes($select={_ATTRIBUTE_COLUMNS})",
            },
        )
        self._entity_sets[logical_name] = data["EntitySetName"]
        columns = [
            _describe_attribute(a)
            for a in data.get("Attributes", [])
            # AttributeOf marks shadow columns (e.g. the *name of a lookup) that
            # cannot be selected on their own.
            if a.get("IsValidForRead") and not a.get("AttributeOf")
        ]
        columns.sort(key=lambda c: c["name"])
        return {
            "logical_name": data["LogicalName"],
            "entity_set": data["EntitySetName"],
            "display_name": _label(data.get("DisplayName")),
            "primary_id": data.get("PrimaryIdAttribute"),
            "primary_name": data.get("PrimaryNameAttribute"),
            "columns": columns,
        }

    async def entity_set_name(self, table: str) -> str:
        """Resolve a logical name ("account") to its entity set ("accounts").

        The Web API addresses tables by entity set name, which is usually but
        not reliably the plural of the logical name (think "opportunities", or
        custom tables whose set name was edited), so it is looked up once and cached.
        """
        logical_name = odata.validate_identifier(table, "table name")
        if logical_name not in self._entity_sets:
            data = await self._get_json(
                f"EntityDefinitions(LogicalName={odata.quote_string(logical_name)})",
                params={"$select": "EntitySetName"},
            )
            self._entity_sets[logical_name] = data["EntitySetName"]
        return self._entity_sets[logical_name]

    # -- reads --------------------------------------------------------------

    async def query(
        self,
        table: str,
        *,
        select: list[str] | str | None = None,
        filter: str | None = None,
        orderby: str | None = None,
        top: int | None = None,
    ) -> dict[str, Any]:
        page_size = odata.clamp_top(top, self.settings.default_top, self.settings.max_top)
        params = _compact(
            {
                "$select": odata.build_select(select),
                "$filter": odata.check_filter(filter),
                "$orderby": odata.build_orderby(orderby),
            }
        )
        entity_set = await self.entity_set_name(table)
        # Page size goes in the Prefer header rather than $top on purpose: with
        # $top, Dataverse never returns @odata.nextLink, and we would have no way
        # to tell the agent that the result was cut short.
        data = await self._get_json(
            entity_set, params=params, prefer=f"odata.maxpagesize={page_size},{_ANNOTATIONS}"
        )
        records = [odata.simplify_record(r) for r in data.get("value", [])]
        result: dict[str, Any] = {
            "table": table,
            "count": len(records),
            "more_records": "@odata.nextLink" in data,
            "records": records,
        }
        if result["more_records"]:
            result["note"] = (
                f"Only the first {page_size} rows were returned. Narrow the filter, "
                "or ask for specific columns and a smaller set."
            )
        return result

    async def get_record(
        self, table: str, record_id: str, select: list[str] | str | None = None
    ) -> dict[str, Any]:
        guid = odata.validate_guid(record_id)
        params = _compact({"$select": odata.build_select(select)})
        entity_set = await self.entity_set_name(table)
        data = await self._get_json(f"{entity_set}({guid})", params=params, prefer=_ANNOTATIONS)
        return odata.simplify_record(data)

    async def whoami(self) -> dict[str, Any]:
        data = await self._get_json("WhoAmI")
        return {
            "user_id": data.get("UserId"),
            "business_unit_id": data.get("BusinessUnitId"),
            "organization_id": data.get("OrganizationId"),
        }

    # -- writes -------------------------------------------------------------

    async def create_record(self, table: str, data: Mapping[str, Any]) -> str:
        self._require_writes()
        body = odata.validate_payload(data)
        entity_set = await self.entity_set_name(table)
        response = await self._request("POST", entity_set, json=body)
        record_id = odata.parse_entity_id(response.headers.get("OData-EntityId"))
        if record_id is None:
            raise DataverseError("Record was created but Dataverse did not return its id.")
        return record_id

    async def update_record(self, table: str, record_id: str, data: Mapping[str, Any]) -> None:
        self._require_writes()
        guid = odata.validate_guid(record_id)
        body = odata.validate_payload(data)
        entity_set = await self.entity_set_name(table)
        # PATCH on Dataverse is an upsert: a mistyped id would silently create a
        # new record. If-Match: * turns that into a 404 instead.
        await self._request("PATCH", f"{entity_set}({guid})", json=body, headers={"If-Match": "*"})

    def _require_writes(self) -> None:
        if not self.settings.allow_writes:
            raise WritesDisabledError()

    # -- transport ----------------------------------------------------------

    async def _load_tables(self) -> list[dict[str, Any]]:
        if self._tables is None:
            data = await self._get_json(
                "EntityDefinitions",
                params={"$select": _TABLE_COLUMNS, "$filter": "IsPrivate eq false"},
            )
            tables = [_describe_table(t) for t in data.get("value", [])]
            tables.sort(key=lambda t: t["logical_name"])
            self._entity_sets.update({t["logical_name"]: t["entity_set"] for t in tables})
            self._tables = tables
        return self._tables

    async def _get_json(
        self, path: str, *, params: Mapping[str, str] | None = None, prefer: str | None = None
    ) -> dict[str, Any]:
        headers = {"Prefer": prefer} if prefer else None
        response = await self._request("GET", path, params=params, headers=headers)
        return response.json()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        token = await self._tokens.get_token()
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            **(headers or {}),
        }
        try:
            response = await self._http.request(
                method, path, params=params, json=json, headers=request_headers
            )
        except httpx.TimeoutException:
            raise DataverseError(
                f"Dataverse did not respond within {self.settings.timeout_seconds:g} seconds. "
                "Try a narrower query."
            ) from None
        except httpx.TransportError as exc:
            raise DataverseError(f"Could not reach Dataverse: {exc}") from None
        if response.is_error:
            raise error_from_response(response)
        return response


def error_from_response(response: httpx.Response) -> DataverseError:
    """Turn a Web API error payload into a message an agent can act on.

    Dataverse returns ``{"error": {"code": "0x80040217", "message": "..."}}``.
    The message is usually precise (it names the missing column or the
    privilege), so it is kept verbatim and prefixed with a short hint.
    """
    status = response.status_code
    code, detail = _parse_error_body(response)
    if status == 429:
        retry_after = response.headers.get("Retry-After", "a few")
        hint = f"Dataverse service protection limit reached. Retry after {retry_after} seconds."
    else:
        hint = _STATUS_HINTS.get(status, f"Dataverse returned HTTP {status}.")
    message = f"{hint} Details: {detail}" if detail else hint
    return DataverseError(message, status=status, code=code)


def _parse_error_body(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        error = response.json().get("error", {})
    except (ValueError, AttributeError):
        text = response.text.strip()
        return None, text[:500] or None
    if not isinstance(error, dict):
        return None, None
    return error.get("code"), error.get("message")


def _label(display_name: Mapping[str, Any] | None) -> str | None:
    # UserLocalizedLabel is null for tables and columns that were never labelled.
    label = (display_name or {}).get("UserLocalizedLabel") or {}
    return label.get("Label")


def _describe_table(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "logical_name": raw["LogicalName"],
        "entity_set": raw.get("EntitySetName"),
        "display_name": _label(raw.get("DisplayName")),
        "is_custom": bool(raw.get("IsCustomEntity")),
        "customizable": bool((raw.get("IsCustomizable") or {}).get("Value")),
        "intersect": bool(raw.get("IsIntersect")),
    }


def _describe_attribute(raw: Mapping[str, Any]) -> dict[str, Any]:
    name = raw["LogicalName"]
    type_name = (raw.get("AttributeTypeName") or {}).get("Value", "Unknown")
    column: dict[str, Any] = {
        "name": name,
        "type": type_name.removesuffix("Type"),
        "display_name": _label(raw.get("DisplayName")),
        "required": (raw.get("RequiredLevel") or {}).get("Value")
        in ("ApplicationRequired", "SystemRequired"),
        "can_create": bool(raw.get("IsValidForCreate")),
        "can_update": bool(raw.get("IsValidForUpdate")),
    }
    if type_name in _LOOKUP_TYPES:
        # Queries expose lookups as _<name>_value; $select/$filter on the bare
        # name fails with a "could not find a property" error.
        column["query_as"] = f"_{name}_value"
    return column


def _compact(params: Mapping[str, str | None]) -> dict[str, str]:
    return {k: v for k, v in params.items() if v is not None}
