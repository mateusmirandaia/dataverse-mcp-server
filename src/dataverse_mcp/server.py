"""MCP server definition and the `dataverse-mcp` entry point."""

import json
import logging
import sys
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Annotated, Any, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from dataverse_mcp import __version__
from dataverse_mcp.auth import MsalTokenProvider
from dataverse_mcp.client import DataverseClient
from dataverse_mcp.config import Settings
from dataverse_mcp.errors import DataverseMCPError

log = logging.getLogger("dataverse_mcp")

T = TypeVar("T")

INSTRUCTIONS = """\
Tools for reading (and, if enabled, writing) Microsoft Dataverse / Dynamics 365 data.
Start with list_tables to find the table, then describe_table to learn its columns
before querying. Tables and columns are addressed by logical name (e.g. 'account',
'emailaddress1'). Lookup columns are queried as _<name>_value; describe_table shows
the exact name under 'query_as'. Results are capped per call; when 'more_records'
is true, refine the filter instead of asking for more rows.
"""

Table = Annotated[str, Field(description="Logical name of the table, e.g. 'account' or 'contact'.")]
RecordId = Annotated[str, Field(description="Record id (GUID).")]
Columns = Annotated[
    list[str] | None,
    Field(description="Columns to return (logical names). Omit only when you need every column."),
]
Payload = Annotated[
    dict[str, Any],
    Field(
        description=(
            "Column values keyed by logical name. Set lookups with "
            "'<navigation property>@odata.bind': '/<entity set>(<id>)'."
        )
    ),
]


async def _guard(call: Awaitable[T]) -> T:
    """Report expected failures to the model as tool errors instead of crashes."""
    try:
        return await call
    except DataverseMCPError as exc:
        raise ToolError(str(exc)) from exc


def create_server(settings: Settings, client: DataverseClient) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await client.aclose()

    server = MCPServer(
        "dataverse",
        title="Dataverse",
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
    )
    _register_read_tools(server, client, settings)
    if settings.allow_writes:
        _register_write_tools(server, client)
    else:
        log.info("Write tools are disabled (DATAVERSE_ALLOW_WRITES is not true).")
    _register_context(server, client, settings)
    return server


def _register_read_tools(server: MCPServer, client: DataverseClient, settings: Settings) -> None:
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=read_only)
    async def list_tables(
        search: Annotated[
            str | None, Field(description="Case-insensitive text to match in name or display name.")
        ] = None,
        include_system: Annotated[
            bool, Field(description="Include non-customizable and intersect (N:N) tables.")
        ] = False,
    ) -> dict[str, Any]:
        """List Dataverse tables available to this connection."""
        tables = await _guard(client.list_tables(search=search, include_system=include_system))
        return {"count": len(tables), "tables": tables}

    @server.tool(annotations=read_only)
    async def describe_table(table: Table) -> dict[str, Any]:
        """Describe a table: entity set, primary columns, and every readable column with its type."""
        return await _guard(client.describe_table(table))

    @server.tool(annotations=read_only)
    async def query_records(
        table: Table,
        select: Columns = None,
        filter: Annotated[
            str | None,
            Field(
                description="OData $filter expression, e.g. \"statecode eq 0 and contains(name,'contoso')\"."
            ),
        ] = None,
        orderby: Annotated[
            str | None, Field(description="Comma separated, e.g. 'createdon desc,name'.")
        ] = None,
        top: Annotated[
            int | None,
            Field(
                description=f"Rows to return (default {settings.default_top}, maximum {settings.max_top})."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Query rows from a table using OData options. Choice and lookup labels are in '_formatted'."""
        return await _guard(client.query(table, select=select, filter=filter, orderby=orderby, top=top))

    @server.tool(annotations=read_only)
    async def get_record(table: Table, record_id: RecordId, select: Columns = None) -> dict[str, Any]:
        """Fetch a single row by id."""
        return await _guard(client.get_record(table, record_id, select=select))


def _register_write_tools(server: MCPServer, client: DataverseClient) -> None:
    @server.tool(
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
    )
    async def create_record(table: Table, data: Payload) -> dict[str, Any]:
        """Create a row and return its id."""
        record_id = await _guard(client.create_record(table, data))
        return {"table": table, "id": record_id}

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
        )
    )
    async def update_record(table: Table, record_id: RecordId, data: Payload) -> dict[str, Any]:
        """Update columns on an existing row. Fails if the row does not exist; never creates one."""
        await _guard(client.update_record(table, record_id, data))
        return {"table": table, "id": record_id, "updated": sorted(data)}


def _register_context(server: MCPServer, client: DataverseClient, settings: Settings) -> None:
    @server.resource(
        "dataverse://connection",
        name="connection",
        description="Environment, identity and limits of this Dataverse connection.",
        mime_type="application/json",
    )
    async def connection() -> str:
        info: dict[str, Any] = {
            "environment": settings.url,
            "writes_enabled": settings.allow_writes,
            "default_top": settings.default_top,
            "max_top": settings.max_top,
        }
        try:
            info["identity"] = await client.whoami()
        except DataverseMCPError as exc:
            info["identity_error"] = str(exc)
        return json.dumps(info, indent=2)

    @server.prompt(description="Walk through a table's structure and a small sample of its data.")
    def explore_table(table: str) -> str:
        return (
            f"Use describe_table on '{table}' and summarise its purpose, primary name column, "
            "required columns and lookups. Then call query_records with a handful of meaningful "
            "columns and top=5 to show representative rows. Point out anything that looks like "
            "data quality issues (empty required-looking fields, inconsistent formats)."
        )


def main() -> None:
    # stdout is the MCP stdio channel; anything else written there corrupts the protocol.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings()
    except ValidationError as exc:
        print("dataverse-mcp: invalid configuration (see .env.example):", file=sys.stderr)
        for error in exc.errors():
            field = "_".join(map(str, error["loc"])).upper()
            prefix = f"DATAVERSE_{field}: " if field else ""
            print(f"  - {prefix}{error['msg']}", file=sys.stderr)
        raise SystemExit(2) from None

    client = DataverseClient(settings, MsalTokenProvider(settings))
    create_server(settings, client).run()


if __name__ == "__main__":
    main()
