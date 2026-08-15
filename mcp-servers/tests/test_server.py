"""End-to-end tool behaviour, driven through an in-memory MCP client."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastmcp import Client

from oracle_fusion_mcp.config import Config
from oracle_fusion_mcp.server import create_server

EXPECTED_TOOLS = {
    "search_operations",
    "list_categories",
    "list_resources",
    "list_operations",
    "describe_operation",
    "describe_schema",
    "invoke_read",
    "invoke_write",
    "invoke_delete",
}

READ_ONLY_TOOLS = {
    "search_operations",
    "list_categories",
    "list_resources",
    "list_operations",
    "describe_operation",
    "describe_schema",
    "invoke_read",
}


async def call(server: Any, name: str, arguments: dict[str, Any] | None = None) -> str:
    async with Client(server) as client:
        result = await client.call_tool(name, arguments or {})
        return result.content[0].text


async def call_json(server: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call a tool and parse its JSON payload, ignoring any trailing body block."""
    text = await call(server, name, arguments)
    decoder = json.JSONDecoder()
    return decoder.raw_decode(text)[0]


def stub_transport(server: Any, handler: Any) -> None:
    """Point the server's HTTP client at a mock transport instead of a real pod."""
    config = server.oracle_config
    fusion = server.oracle_client
    fusion._client = httpx.AsyncClient(
        base_url=config.base_url(),
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
    )


@pytest.fixture
def server(mini_config: Config) -> Any:
    return create_server(mini_config.definition, mini_config)


class TestToolSurface:
    async def test_exposes_exactly_the_search_and_execute_tools(self, server: Any) -> None:
        async with Client(server) as client:
            names = {t.name for t in await client.list_tools()}
        assert names == EXPECTED_TOOLS

    async def test_read_tools_are_annotated_read_only(self, server: Any) -> None:
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}
        for name in READ_ONLY_TOOLS:
            assert tools[name].annotations.readOnlyHint is True, name

    async def test_mutating_tools_are_not_read_only_and_delete_is_destructive(
        self, server: Any
    ) -> None:
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert tools["invoke_write"].annotations.readOnlyHint is False
        assert tools["invoke_delete"].annotations.destructiveHint is True
        # A create/update is not itself destructive, which drives host UX.
        assert tools["invoke_write"].annotations.destructiveHint is False

    async def test_every_tool_carries_a_title(self, server: Any) -> None:
        async with Client(server) as client:
            for tool in await client.list_tools():
                assert tool.annotations.title, tool.name


class TestDiscovery:
    async def test_search_returns_operation_ids_and_totals(self, server: Any) -> None:
        payload = await call_json(server, "search_operations", {"query": "purchase order"})
        assert payload["total_matches"] >= 4
        assert any(m["operation_id"] == "getall_purchaseOrders" for m in payload["matches"])

    async def test_search_miss_explains_how_to_recover(self, server: Any) -> None:
        payload = await call_json(server, "search_operations", {"query": "zzzz nonexistent"})
        assert payload["matches"] == []
        assert "list_categories" in payload["note"]

    async def test_search_rejects_an_invalid_kind(self, server: Any) -> None:
        with pytest.raises(Exception, match="kind must be one of"):
            await call(server, "search_operations", {"query": "x", "kind": "bogus"})

    async def test_list_categories_counts_operations(self, server: Any) -> None:
        payload = await call_json(server, "list_categories")
        names = {c["name"]: c["operations"] for c in payload["categories"]}
        assert names["Procurement"] == 5

    async def test_list_operations_for_an_unknown_resource_is_a_soft_miss(
        self, server: Any
    ) -> None:
        payload = await call_json(server, "list_operations", {"resource": "Nope"})
        assert payload["operations"] == []
        assert "list_resources" in payload["note"]


class TestDescribe:
    async def test_describe_operation_reports_placeholders_and_routing(
        self, server: Any
    ) -> None:
        payload = await call_json(server, "describe_operation", {"operation_id": "get_purchaseOrder"})
        assert payload["path_placeholders"] == ["OrderId"]
        assert payload["invoke_with"] == "invoke_read"
        assert any(p["name"] == "OrderId" and p["required"] for p in payload["path_parameters"])

    async def test_describe_operation_expands_the_request_body(self, server: Any) -> None:
        payload = await call_json(server, "describe_operation", {"operation_id": "create_purchaseOrder"})
        body = payload["request_body"]
        assert body["required"] is True
        assert "OrderNumber" in body["schema"]["properties"]

    async def test_describe_operation_routes_writes_to_the_write_tool(self, server: Any) -> None:
        payload = await call_json(server, "describe_operation", {"operation_id": "delete_purchaseOrder"})
        assert payload["invoke_with"] == "invoke_delete"

    async def test_unknown_operation_id_suggests_alternatives(self, server: Any) -> None:
        with pytest.raises(Exception, match="Did you mean"):
            await call(server, "describe_operation", {"operation_id": "purchaseOrder"})

    async def test_response_schema_is_opt_in(self, server: Any) -> None:
        without = await call_json(server, "describe_operation", {"operation_id": "get_purchaseOrder"})
        assert "response_schema" not in without
        with_schema = await call_json(
            server, "describe_operation", {"operation_id": "get_purchaseOrder", "include_response_schema": True}
        )
        assert "OrderNumber" in with_schema["response_schema"]["schema"]["properties"]

    async def test_describe_schema_expands_a_named_component(self, server: Any) -> None:
        payload = await call_json(server, "describe_schema", {"name": "PurchaseOrder"})
        assert "OrderNumber" in payload["schema"]["properties"]

    async def test_describe_schema_rejects_unknown_names(self, server: Any) -> None:
        with pytest.raises(Exception, match="No schema named"):
            await call(server, "describe_schema", {"name": "Nope"})


class TestReadWriteSplit:
    """No tool may run an operation of a different kind."""

    async def test_read_tool_refuses_a_write_operation(self, server: Any) -> None:
        with pytest.raises(Exception, match="Use invoke_write instead"):
            await call(server, "invoke_read", {"operation_id": "create_purchaseOrder"})

    async def test_read_tool_refuses_a_delete_operation(self, server: Any) -> None:
        with pytest.raises(Exception, match="Use invoke_delete instead"):
            await call(server, "invoke_read", {"operation_id": "delete_purchaseOrder"})

    async def test_write_tool_refuses_a_read_operation(self, server: Any) -> None:
        with pytest.raises(Exception, match="Use invoke_read instead"):
            await call(server, "invoke_write", {"operation_id": "getall_purchaseOrders"})

    async def test_write_tool_refuses_a_delete_operation(self, server: Any) -> None:
        with pytest.raises(Exception, match="Use invoke_delete instead"):
            await call(server, "invoke_write", {"operation_id": "delete_purchaseOrder"})

    async def test_delete_tool_refuses_a_write_operation(self, server: Any) -> None:
        with pytest.raises(Exception, match="Use invoke_write instead"):
            await call(server, "invoke_delete", {"operation_id": "create_purchaseOrder"})


class TestInvocation:
    async def test_read_issues_the_expected_request(self, server: Any) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["method"] = request.method
            return httpx.Response(200, json={"items": [{"OrderNumber": "PO1"}], "count": 1})

        stub_transport(server, handler)
        text = await call(
            server,
            "invoke_read",
            {"operation_id": "get_purchaseOrder", "path_params": {"OrderId": "300100"}, "query": {"limit": 5}},
        )
        assert seen["method"] == "GET"
        assert "/fscmRestApi/resources/11.13.18.05/purchaseOrders/300100" in seen["url"]
        assert "limit=5" in seen["url"]
        assert "PO1" in text

    async def test_missing_path_parameter_is_caught_before_any_request(
        self, server: Any
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("no request should be made")

        stub_transport(server, handler)
        with pytest.raises(Exception, match="Missing required path parameter"):
            await call(server, "invoke_read", {"operation_id": "get_purchaseOrder"})

    async def test_patch_uses_oracle_merge_patch_content_type(self, server: Any) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["content_type"] = request.headers.get("content-type")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"OrderNumber": "PO1"})

        stub_transport(server, handler)
        await call(
            server,
            "invoke_write",
            {
                "operation_id": "update_purchaseOrder",
                "path_params": {"OrderId": "300100"},
                "body": {"Supplier": "Acme"},
            },
        )
        assert seen["content_type"] == "application/vnd.oracle.adf.resourceitem+json"
        assert seen["body"] == {"Supplier": "Acme"}

    async def test_post_uses_plain_json(self, server: Any) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["content_type"] = request.headers.get("content-type")
            return httpx.Response(201, json={"OrderNumber": "PO2"})

        stub_transport(server, handler)
        await call(server, "invoke_write", {"operation_id": "create_purchaseOrder", "body": {"OrderNumber": "PO2"}})
        assert seen["content_type"] == "application/json"

    async def test_none_query_values_are_dropped(self, server: Any) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={})

        stub_transport(server, handler)
        await call(
            server,
            "invoke_read",
            {"operation_id": "getall_purchaseOrders", "query": {"limit": 5, "q": None}},
        )
        assert "q=" not in seen["url"]

    async def test_oracle_error_documents_surface_their_detail(self, server: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "title": "Bad Request",
                    "detail": "The value for OrderNumber is invalid.",
                    "o:errorDetails": [{"detail": "Attribute OrderNumber is required."}],
                },
            )

        stub_transport(server, handler)
        with pytest.raises(Exception) as excinfo:
            await call(server, "invoke_read", {"operation_id": "getall_purchaseOrders"})
        message = str(excinfo.value)
        assert "The value for OrderNumber is invalid." in message
        assert "Attribute OrderNumber is required." in message

    async def test_auth_failure_names_the_env_vars_to_fix(self, server: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"title": "Unauthorized"})

        stub_transport(server, handler)
        with pytest.raises(Exception, match="ORACLE_FUSION_TEST_USERNAME"):
            await call(server, "invoke_read", {"operation_id": "getall_purchaseOrders"})

    async def test_large_responses_are_truncated_with_a_note(self, server: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [{"pad": "x" * 200} for _ in range(200)]})

        stub_transport(server, handler)
        text = await call(server, "invoke_read", {"operation_id": "getall_purchaseOrders"})
        assert "truncated" in text
        assert "fields" in text

    async def test_delete_reaches_the_pod_with_the_delete_method(self, server: Any) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            return httpx.Response(204)

        stub_transport(server, handler)
        text = await call(
            server, "invoke_delete", {"operation_id": "delete_purchaseOrder", "path_params": {"OrderId": "1"}}
        )
        assert seen["method"] == "DELETE"
        assert "204" in text


class TestUnconfiguredServer:
    """Without credentials the catalog still works; only live calls fail."""

    @pytest.fixture
    def server(self, unconfigured_config: Config) -> Any:
        return create_server(unconfigured_config.definition, unconfigured_config)

    async def test_catalog_tools_work_without_credentials(self, server: Any) -> None:
        payload = await call_json(server, "search_operations", {"query": "purchase"})
        assert payload["matches"]

    async def test_invocation_explains_what_is_missing(self, server: Any) -> None:
        with pytest.raises(Exception, match="not configured"):
            await call(server, "invoke_read", {"operation_id": "getall_purchaseOrders"})
