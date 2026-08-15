"""FastMCP server factory shared by all three Oracle Fusion specs.

The specs describe between 485 and 10,335 operations each — orders of magnitude
past the point where one tool per operation is viable, since every tool schema
costs context on every turn. These servers therefore use the search + execute
pattern: the catalog lives in a SQLite index and is reached through a fixed set
of nine tools.

Execution is split three ways — read, write and delete — so that hosts can
auto-approve retrievals while still prompting on mutations, and so no single
tool can both read and modify Fusion data.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import ApiError, FusionClient
from .config import Config, load
from .index import IndexNotBuilt, Operation, SpecIndex
from .paths import fill_path_params, path_placeholders
from .refs import resolve, summarize_schema
from .specs import SpecDef

#: Oracle's framework-level query parameters, accepted by most Fusion resources.
#: Documented in the execute tools so the model can page and filter without a
#: describe_operation round-trip for the common case.
FRAMEWORK_QUERY_HELP = (
    "Common Oracle query parameters: `q` (filter, e.g. \"ItemNumber='AS54888'\"), "
    "`fields` (comma-separated attributes to return), `expand` (child resources to "
    "inline), `limit` and `offset` (paging), `orderBy` (e.g. 'CreationDate:desc'), "
    "`totalResults=true` (include the total count), `finder` (named finder, "
    "e.g. 'PrimaryKey;Id=100')."
)


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _operations_payload(operations: list[Operation], total: int, shown: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "matches": [op.brief() for op in operations],
        "returned": len(operations),
        "total_matches": total,
    }
    if total > shown:
        payload["note"] = (
            f"Showing {len(operations)} of {total} matches. "
            f"Refine the query or raise `limit` to see more."
        )
    return payload


def create_server(definition: SpecDef, config: Config | None = None) -> FastMCP:
    """Build the MCP server for one spec."""
    config = config or load(definition)

    try:
        index = SpecIndex(config.index_path)
        index_error: str | None = None
        counts = index.counts()
        catalog_line = (
            f"{counts['total']:,} operations "
            f"({counts['read']:,} read, {counts['write']:,} write, {counts['delete']:,} delete)"
        )
    except IndexNotBuilt as error:
        # Start anyway so the failure is visible as a tool error rather than a
        # server that refuses to launch inside the host.
        index = None  # type: ignore[assignment]
        index_error = str(error)
        catalog_line = "catalog unavailable — index not built"

    client = FusionClient(config)

    status = (
        "Configured to call the pod at " + config.base_url()
        if config.configured
        else "Not configured for live calls (missing: " + ", ".join(config.missing()) + "). "
        "Catalog tools still work."
    )

    instructions = (
        f"{definition.blurb}\n\n"
        f"Catalog: {catalog_line}. Base path: {config.base_path}.\n"
        f"{status}\n\n"
        "Because the API is far too large to expose one tool per endpoint, operations are "
        "discovered through `search_operations` / `list_resources` and then run through "
        "`invoke_read`, `invoke_write` or `invoke_delete` using the operation_id.\n\n"
        "Oracle REST reference: https://docs.oracle.com/en/cloud/saas/index.html"
    )

    mcp = FastMCP(name=definition.server_name, instructions=instructions)

    def require_index() -> SpecIndex:
        if index is None:
            raise ToolError(index_error or "Index unavailable.")
        return index

    # ---------------------------------------------------------------- discovery

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Search API operations",
            readOnlyHint=True,
            openWorldHint=False,
        )
    )
    async def search_operations(
        query: Annotated[
            str,
            Field(description="Keywords to match against operation paths, summaries, descriptions and resource names. Example: 'purchase order lines'."),
        ],
        limit: Annotated[
            int, Field(description="Maximum results to return.", ge=1, le=100)
        ] = 20,
        kind: Annotated[
            str | None,
            Field(description="Restrict to 'read', 'write' or 'delete' operations."),
        ] = None,
        method: Annotated[
            str | None,
            Field(description="Restrict to one HTTP method, e.g. 'GET' or 'POST'."),
        ] = None,
        category: Annotated[
            str | None,
            Field(description="Restrict to one top-level category from list_categories."),
        ] = None,
    ) -> str:
        """Find API operations by keyword.

        Returns operation ids with their method, path and summary. The
        operation_id is what invoke_read / invoke_write / invoke_delete take.
        Searches the API catalog only — it does not read any data from the
        Fusion pod. Use describe_operation for a specific operation's parameters.
        """
        idx = require_index()
        if kind and kind not in {"read", "write", "delete"}:
            raise ToolError("kind must be one of: read, write, delete")

        operations, total = idx.search(
            query, limit=limit, kind=kind, method=method, category=category
        )
        if not operations:
            return _json(
                {
                    "matches": [],
                    "total_matches": 0,
                    "note": (
                        f"No operations matched {query!r}. Try fewer or more general "
                        f"keywords, or browse with list_categories / list_resources."
                    ),
                }
            )
        return _json(_operations_payload(operations, total, limit))

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List API categories", readOnlyHint=True, openWorldHint=False
        )
    )
    async def list_categories() -> str:
        """List the top-level functional areas of this API with operation counts.

        Categories are the coarsest grouping (for example 'Inventory Management').
        Use list_resources to see the resources inside one.
        """
        idx = require_index()
        categories = idx.list_categories()
        return _json(
            {
                "categories": [{"name": name, "operations": n} for name, n in categories],
                "count": len(categories),
            }
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List API resources", readOnlyHint=True, openWorldHint=False
        )
    )
    async def list_resources(
        category: Annotated[
            str | None,
            Field(description="Limit to one category from list_categories. Omit to list across all categories."),
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum resources to return.", ge=1, le=500)
        ] = 100,
        offset: Annotated[
            int, Field(description="Number of resources to skip, for paging.", ge=0)
        ] = 0,
    ) -> str:
        """List the API resources (tags), optionally within one category.

        Each entry names a resource and how many operations it has. Use
        list_operations to see the operations for one resource.
        """
        idx = require_index()
        resources, total = idx.list_resources(category, limit=limit, offset=offset)
        payload: dict[str, Any] = {
            "resources": [
                {"resource": tag, "category": cat, "operations": n} for tag, cat, n in resources
            ],
            "returned": len(resources),
            "total": total,
        }
        if offset + len(resources) < total:
            payload["note"] = (
                f"Showing {offset + 1}-{offset + len(resources)} of {total}. "
                f"Increase `offset` to page."
            )
        return _json(payload)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List operations for a resource", readOnlyHint=True, openWorldHint=False
        )
    )
    async def list_operations(
        resource: Annotated[
            str,
            Field(description="Exact resource/tag name as returned by list_resources."),
        ],
        kind: Annotated[
            str | None, Field(description="Restrict to 'read', 'write' or 'delete'.")
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum operations to return.", ge=1, le=200)
        ] = 50,
    ) -> str:
        """List every operation belonging to one resource.

        Use this after list_resources when you know which resource you want and
        need its full set of endpoints. For keyword lookup across all resources,
        use search_operations.
        """
        idx = require_index()
        operations, total = idx.operations_for_tag(resource, kind=kind, limit=limit)
        if not operations:
            return _json(
                {
                    "operations": [],
                    "note": (
                        f"No operations found for resource {resource!r}. "
                        f"Resource names must match list_resources exactly."
                    ),
                }
            )
        return _json(
            {
                "resource": resource,
                "operations": [op.brief() for op in operations],
                "returned": len(operations),
                "total": total,
            }
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Describe an operation", readOnlyHint=True, openWorldHint=False
        )
    )
    async def describe_operation(
        operation_id: Annotated[
            str, Field(description="Operation id from search_operations or list_operations.")
        ],
        include_response_schema: Annotated[
            bool,
            Field(description="Include a summary of the success response schema. Large for Oracle resources."),
        ] = False,
    ) -> str:
        """Show one operation's parameters, request body and required path values.

        Returns the path placeholders that must be supplied, each query parameter
        with its type and description, and the request body schema for write
        operations. Reads the catalog only — it does not call the Fusion pod.
        """
        idx = require_index()
        found = idx.get(operation_id)
        if found is None:
            similar = idx.find_similar(operation_id)
            hint = (
                " Did you mean: " + ", ".join(op.op_id for op in similar)
                if similar
                else " Use search_operations to find valid operation ids."
            )
            raise ToolError(f"No operation with id {operation_id!r}.{hint}")

        operation, detail = found
        resolved_params = [
            resolve(p, idx, max_depth=2) for p in detail.get("parameters", [])
        ]

        path_params: list[dict[str, Any]] = []
        query_params: list[dict[str, Any]] = []
        header_params: list[dict[str, Any]] = []
        for param in resolved_params:
            if not isinstance(param, dict):
                continue
            entry = {
                "name": param.get("name"),
                "required": bool(param.get("required")),
                "description": (param.get("description") or "")[:400],
            }
            schema = param.get("schema")
            if isinstance(schema, dict):
                entry["type"] = schema.get("type")
                if "enum" in schema:
                    entry["enum"] = schema["enum"]
                if "default" in schema:
                    entry["default"] = schema["default"]
            location = param.get("in")
            if location == "path":
                path_params.append(entry)
            elif location == "query":
                query_params.append(entry)
            elif location == "header":
                header_params.append(entry)

        payload: dict[str, Any] = {
            "operation_id": operation.op_id,
            "method": operation.method,
            "path": operation.path,
            "kind": operation.kind,
            "invoke_with": {
                "read": "invoke_read",
                "write": "invoke_write",
                "delete": "invoke_delete",
            }[operation.kind],
            "resource": operation.tag,
            "summary": operation.summary,
            "description": (detail.get("description") or "")[:2000],
            "path_placeholders": path_placeholders(operation.path),
            "path_parameters": path_params,
            "query_parameters": query_params,
        }
        if header_params:
            payload["header_parameters"] = header_params

        request_body = detail.get("requestBody")
        if request_body:
            resolved_body = resolve(request_body, idx, max_depth=3)
            content = (resolved_body or {}).get("content", {})
            for media_type, media in content.items():
                if isinstance(media, dict) and "schema" in media:
                    payload["request_body"] = {
                        "content_type": media_type,
                        "required": bool(resolved_body.get("required")),
                        "schema": summarize_schema(media["schema"]),
                    }
                    break

        if include_response_schema:
            responses = detail.get("responses", {})
            for status in ("200", "201", "default"):
                entry = responses.get(status)
                if not entry:
                    continue
                resolved_response = resolve(entry, idx, max_depth=3)
                content = (resolved_response or {}).get("content", {})
                for media in content.values():
                    if isinstance(media, dict) and "schema" in media:
                        payload["response_schema"] = {
                            "status": status,
                            "schema": summarize_schema(media["schema"]),
                        }
                        break
                break

        return _json(payload)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Describe a schema", readOnlyHint=True, openWorldHint=False
        )
    )
    async def describe_schema(
        name: Annotated[
            str,
            Field(description="Schema name, as reported in a `$ref_unexpanded` marker from describe_operation."),
        ],
        summarize: Annotated[
            bool,
            Field(description="Return a condensed property list rather than the raw schema."),
        ] = True,
    ) -> str:
        """Expand one schema by name from the spec's components.

        describe_operation truncates deeply nested schemas and marks them with
        `$ref_unexpanded`; this fetches the full definition for such a name.
        """
        idx = require_index()
        raw = idx.component("schemas", name)
        if raw is None:
            raise ToolError(
                f"No schema named {name!r} in this spec. Schema names come from the "
                f"`$ref_unexpanded` markers returned by describe_operation."
            )
        expanded = resolve(raw, idx, max_depth=2)
        return _json(
            {
                "name": name,
                "schema": summarize_schema(expanded) if summarize else expanded,
            }
        )

    # ---------------------------------------------------------------- execution

    async def _invoke(
        operation_id: str,
        allowed_kind: str,
        tool_name: str,
        path_params: dict[str, Any] | None,
        query: dict[str, Any] | None,
        body: Any = None,
    ) -> str:
        idx = require_index()
        found = idx.get(operation_id)
        if found is None:
            similar = idx.find_similar(operation_id)
            hint = (
                " Did you mean: " + ", ".join(op.op_id for op in similar)
                if similar
                else " Use search_operations to find valid operation ids."
            )
            raise ToolError(f"No operation with id {operation_id!r}.{hint}")

        operation, _detail = found
        if operation.kind != allowed_kind:
            correct = {
                "read": "invoke_read",
                "write": "invoke_write",
                "delete": "invoke_delete",
            }[operation.kind]
            raise ToolError(
                f"{operation_id!r} is a {operation.method} ({operation.kind}) operation and "
                f"cannot be run through {tool_name}. Use {correct} instead."
            )

        filled, missing = fill_path_params(operation.path, path_params or {})
        if missing:
            raise ToolError(
                f"Missing required path parameter(s): {', '.join(sorted(missing))}. "
                f"{operation_id!r} has the path {operation.path}. "
                f"Supply them in `path_params`."
            )

        try:
            response = await client.request(
                operation.method, filled, query=query, body=body
            )
        except ApiError as error:
            raise ToolError(str(error)) from error

        rendered = _json(response.body) if response.body is not None else ""
        rendered, truncated = _truncate(rendered, config.max_response_chars)

        header = {
            "operation_id": operation.op_id,
            "request": f"{operation.method} {filled}",
            "status": response.status,
        }
        if truncated:
            header["note"] = (
                f"Response truncated to {config.max_response_chars} characters. "
                f"Use the `fields` query parameter to select fewer attributes, or "
                f"`limit` to return fewer rows."
            )
        if not rendered:
            header["body"] = "<empty response>"
            return _json(header)
        return _json(header) + "\n" + rendered

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Run a read operation",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    async def invoke_read(
        operation_id: Annotated[
            str, Field(description="Operation id of a GET operation, from search_operations.")
        ],
        path_params: Annotated[
            dict[str, Any] | None,
            Field(description="Values for the `{...}` placeholders in the operation path, keyed by placeholder name."),
        ] = None,
        query: Annotated[
            dict[str, Any] | None,
            Field(description=f"Query string parameters. {FRAMEWORK_QUERY_HELP}"),
        ] = None,
    ) -> str:
        """Retrieve data from the Fusion pod via a GET operation.

        Accepts read operations only; write and delete operations are rejected
        and must go through invoke_write or invoke_delete. Returns the JSON
        response, truncated if very large.
        """
        return await _invoke(operation_id, "read", "invoke_read", path_params, query)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Run a create or update operation",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        )
    )
    async def invoke_write(
        operation_id: Annotated[
            str,
            Field(description="Operation id of a POST, PUT or PATCH operation, from search_operations."),
        ],
        body: Annotated[
            dict[str, Any] | None,
            Field(description="JSON request body. Use describe_operation to see the expected attributes."),
        ] = None,
        path_params: Annotated[
            dict[str, Any] | None,
            Field(description="Values for the `{...}` placeholders in the operation path, keyed by placeholder name."),
        ] = None,
        query: Annotated[
            dict[str, Any] | None,
            Field(description=f"Query string parameters. {FRAMEWORK_QUERY_HELP}"),
        ] = None,
    ) -> str:
        """Create or update records in the Fusion pod via POST, PUT or PATCH.

        This modifies live application data. Accepts write operations only; GET
        operations must go through invoke_read and DELETE through invoke_delete.
        PATCH requests are sent with Oracle's merge-patch content type.
        """
        return await _invoke(
            operation_id, "write", "invoke_write", path_params, query, body
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Run a delete operation",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    async def invoke_delete(
        operation_id: Annotated[
            str, Field(description="Operation id of a DELETE operation, from search_operations.")
        ],
        path_params: Annotated[
            dict[str, Any] | None,
            Field(description="Values for the `{...}` placeholders in the operation path, keyed by placeholder name."),
        ] = None,
        query: Annotated[
            dict[str, Any] | None,
            Field(description="Query string parameters."),
        ] = None,
    ) -> str:
        """Delete a record from the Fusion pod.

        This permanently removes live application data and cannot be undone.
        Accepts DELETE operations only.
        """
        return await _invoke(operation_id, "delete", "invoke_delete", path_params, query)

    # Attached for tests and for the entry points, which need to close the client.
    mcp.oracle_config = config  # type: ignore[attr-defined]
    mcp.oracle_index = index  # type: ignore[attr-defined]
    mcp.oracle_client = client  # type: ignore[attr-defined]
    return mcp
