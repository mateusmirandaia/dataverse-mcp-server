"""Small, strict helpers for building Dataverse Web API (OData v4) requests.

Agents supply table names, column names and filter expressions. Anything that
ends up in a URL path is validated against a narrow grammar so a tool call can
never be turned into a request for an arbitrary endpoint.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from dataverse_mcp.errors import InvalidQueryError

# Logical names, schema names and entity set names all fit this shape.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_ORDER_TERM = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\s+(asc|desc))?$", re.IGNORECASE)
_ENTITY_ID = re.compile(r"\(([0-9a-fA-F-]{36})\)\s*$")

MAX_FILTER_LENGTH = 2000

FORMATTED_VALUE = "OData.Community.Display.V1.FormattedValue"
LOOKUP_LOGICAL_NAME = "Microsoft.Dynamics.CRM.lookuplogicalname"


def validate_identifier(name: str, kind: str = "name") -> str:
    candidate = (name or "").strip()
    if not _IDENTIFIER.fullmatch(candidate):
        raise InvalidQueryError(
            f"{candidate!r} is not a valid {kind}. Use the logical name, e.g. 'account' or 'new_project'."
        )
    return candidate


def validate_guid(value: str) -> str:
    # The classic UI and many exports show record ids wrapped in braces.
    cleaned = (value or "").strip().strip("{}")
    try:
        return str(uuid.UUID(cleaned))
    except ValueError:
        raise InvalidQueryError(f"{value!r} is not a valid record id (expected a GUID).") from None


def quote_string(value: str) -> str:
    """Quote a string literal for OData. Single quotes are escaped by doubling."""
    return "'" + value.replace("'", "''") + "'"


def build_select(columns: Iterable[str] | str | None) -> str | None:
    if columns is None:
        return None
    if isinstance(columns, str):
        columns = columns.split(",")
    names = [validate_identifier(c, "column name") for c in columns if c.strip()]
    return ",".join(dict.fromkeys(names)) or None


def build_orderby(orderby: str | None) -> str | None:
    if not orderby or not orderby.strip():
        return None
    terms = []
    for raw in orderby.split(","):
        match = _ORDER_TERM.fullmatch(raw.strip())
        if not match:
            raise InvalidQueryError(
                f"Invalid orderby term {raw.strip()!r}. Use 'column' or 'column desc', comma separated."
            )
        column, direction = match.groups()
        terms.append(f"{column} {direction.lower()}" if direction else column)
    return ",".join(terms)


def check_filter(expression: str | None) -> str | None:
    """Light sanity checks only; Dataverse itself is the real parser.

    The expression travels as a query-string value (httpx encodes it), so it
    cannot add other query options or change the path. What we guard against
    here is accidental garbage: pasted URLs, whole query strings, runaway size.
    """
    if expression is None or not expression.strip():
        return None
    expression = expression.strip()
    if len(expression) > MAX_FILTER_LENGTH:
        raise InvalidQueryError(f"Filter is longer than {MAX_FILTER_LENGTH} characters.")
    if any(ch in expression for ch in "\r\n\t"):
        raise InvalidQueryError("Filter must be a single line.")
    if expression.startswith(("$filter", "?", "http")) or "&$" in expression:
        raise InvalidQueryError(
            "Pass only the filter expression, e.g. \"statecode eq 0 and contains(name,'contoso')\"."
        )
    return expression


def clamp_top(requested: int | None, default: int, maximum: int) -> int:
    # Large result sets are the easiest way for an agent to burn its context
    # window and the org's API allocation at the same time, so the ceiling is
    # enforced here regardless of what the caller asks for.
    if requested is None:
        return default
    return max(1, min(requested, maximum))


_BIND_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*@odata\.bind$")
_BIND_VALUE = re.compile(r"^/?[A-Za-z_][A-Za-z0-9_]*\([0-9a-fA-F-]{36}\)$")


def validate_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    """Check a create/update body before it is sent.

    Lookups cannot be set through the ``_x_value`` property that queries
    return; Dataverse wants ``navigationproperty@odata.bind`` pointing at
    ``/entityset(id)``. That is the most common mistake agents make, so it gets
    its own message.
    """
    if not isinstance(data, Mapping) or not data:
        raise InvalidQueryError("Provide at least one column to write, as a JSON object.")
    body: dict[str, Any] = {}
    for key, value in data.items():
        if key.startswith("_") and key.endswith("_value"):
            raise InvalidQueryError(
                f"{key!r} is read-only. To set a lookup, use "
                "'<navigation property>@odata.bind': '/<entity set>(<id>)', "
                "e.g. 'parentcustomerid_account@odata.bind': '/accounts(<id>)'."
            )
        if _BIND_KEY.fullmatch(key):
            if not isinstance(value, str) or not _BIND_VALUE.fullmatch(value.strip()):
                raise InvalidQueryError(f"{key!r} must look like '/accounts(<id>)'.")
            body[key] = value.strip()
            continue
        body[validate_identifier(key, "column name")] = value
    return body


def parse_entity_id(header_value: str | None) -> str | None:
    """Extract the GUID from an OData-EntityId header returned on create."""
    if not header_value:
        return None
    match = _ENTITY_ID.search(header_value)
    return match.group(1).lower() if match else None


def simplify_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Make a Web API record easier for a model to read.

    Annotations such as ``statuscode@OData.Community.Display.V1.FormattedValue``
    are folded into ``_formatted`` and ``_lookup_table`` maps instead of being
    left as noisy sibling keys. OData bookkeeping fields are dropped.
    """
    plain: dict[str, Any] = {}
    formatted: dict[str, Any] = {}
    lookup_tables: dict[str, str] = {}
    for key, value in record.items():
        if key.startswith("@odata."):
            continue
        field, _, annotation = key.partition("@")
        if not annotation:
            plain[key] = value
        elif annotation == FORMATTED_VALUE:
            formatted[field] = value
        elif annotation == LOOKUP_LOGICAL_NAME:
            lookup_tables[field] = value
    if formatted:
        plain["_formatted"] = formatted
    if lookup_tables:
        plain["_lookup_table"] = lookup_tables
    return plain
