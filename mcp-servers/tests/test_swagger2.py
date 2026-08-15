"""Swagger 2.0 to OpenAPI 3 upconversion, exercised on the shapes CPQ uses."""

from __future__ import annotations

from typing import Any

from oracle_fusion_mcp.swagger2 import (
    convert_operation,
    convert_parameter,
    convert_responses,
    is_swagger2,
    rewrite_refs,
    upconvert,
)

SWAGGER2: dict[str, Any] = {
    "swagger": "2.0",
    "info": {"title": "Test CPQ", "version": "19"},
    "schemes": ["https"],
    "paths": {
        "/rest/v19/things/{id}": {
            "parameters": [{"name": "id", "in": "path", "required": True, "type": "integer"}],
            "get": {
                "operationId": "getThing",
                "tags": ["Commerce/Things"],
                "produces": ["application/json"],
                "parameters": [{"name": "limit", "in": "query", "type": "integer", "default": 20}],
                "responses": {
                    "200": {
                        "description": "Success",
                        "schema": {"$ref": "#/definitions/Thing"},
                        "headers": {"X-Count": {"type": "integer", "description": "Total."}},
                    }
                },
            },
            "post": {
                "operationId": "createThing",
                "consumes": ["application/json"],
                "parameters": [
                    {
                        "in": "body",
                        "name": "body",
                        "required": True,
                        "schema": {"$ref": "#/definitions/Thing"},
                    }
                ],
                "responses": {"201": {"description": "Created"}},
            },
        },
        "/rest/v19/uploads": {
            "post": {
                "operationId": "upload",
                "consumes": ["multipart/form-data"],
                "parameters": [
                    {"in": "formData", "name": "file", "type": "file", "required": True},
                    {"in": "formData", "name": "note", "type": "string"},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        },
    },
    "definitions": {
        "Thing": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}, "next": {"$ref": "#/definitions/Thing"}},
        }
    },
}


def test_is_swagger2_only_matches_2_0() -> None:
    assert is_swagger2(SWAGGER2)
    assert not is_swagger2({"openapi": "3.0.0", "paths": {}})
    assert not is_swagger2({"swagger": "2.0", "openapi": "3.0.0"})


def test_upconvert_is_a_no_op_for_openapi_3() -> None:
    spec = {"openapi": "3.0.0", "paths": {"/x": {}}}
    assert upconvert(spec) is spec


def test_definitions_become_component_schemas_with_rewritten_refs() -> None:
    converted = upconvert(SWAGGER2)
    assert converted["openapi"] == "3.0.0"
    assert "definitions" not in converted and "swagger" not in converted
    schemas = converted["components"]["schemas"]
    assert "Thing" in schemas
    assert schemas["Thing"]["properties"]["next"]["$ref"] == "#/components/schemas/Thing"


def test_rewrite_refs_leaves_other_pointers_alone() -> None:
    node = {"$ref": "#/components/schemas/Kept", "nested": [{"$ref": "#/definitions/Moved"}]}
    rewritten = rewrite_refs(node)
    assert rewritten["$ref"] == "#/components/schemas/Kept"
    assert rewritten["nested"][0]["$ref"] == "#/components/schemas/Moved"


def test_body_parameter_becomes_request_body() -> None:
    operation = upconvert(SWAGGER2)["paths"]["/rest/v19/things/{id}"]["post"]
    body = operation["requestBody"]
    assert body["required"] is True
    assert body["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Thing"
    }
    # The body parameter must not survive in the parameter list.
    assert all(p.get("in") != "body" for p in operation.get("parameters", []))


def test_form_data_parameters_become_one_multipart_object_schema() -> None:
    operation = upconvert(SWAGGER2)["paths"]["/rest/v19/uploads"]["post"]
    content = operation["requestBody"]["content"]
    assert "multipart/form-data" in content
    schema = content["multipart/form-data"]["schema"]
    assert schema["required"] == ["file"]
    # `type: file` has no OpenAPI 3 equivalent; it becomes a binary string.
    assert schema["properties"]["file"] == {"type": "string", "format": "binary"}
    assert schema["properties"]["note"]["type"] == "string"


def test_bare_parameter_types_move_under_schema() -> None:
    converted = convert_parameter(
        {"name": "limit", "in": "query", "type": "integer", "format": "int64", "default": 20}
    )
    assert converted["schema"] == {"type": "integer", "format": "int64", "default": 20}
    assert converted["name"] == "limit" and converted["in"] == "query"
    assert "type" not in converted


def test_parameter_that_is_already_openapi_3_passes_through() -> None:
    already = {"name": "q", "in": "query", "schema": {"type": "string"}}
    assert convert_parameter(already) == already


def test_path_item_parameters_are_converted_too() -> None:
    item = upconvert(SWAGGER2)["paths"]["/rest/v19/things/{id}"]
    assert item["parameters"][0]["schema"] == {"type": "integer"}


def test_response_schema_moves_under_content_and_headers_gain_schemas() -> None:
    converted = convert_responses(
        {
            "200": {
                "description": "ok",
                "schema": {"type": "string"},
                "headers": {"X-Count": {"type": "integer"}},
            }
        },
        ["application/json"],
    )
    entry = converted["200"]
    assert entry["content"]["application/json"]["schema"] == {"type": "string"}
    assert entry["headers"]["X-Count"]["schema"] == {"type": "integer"}
    assert "schema" not in entry


def test_operation_falls_back_to_document_level_consumes() -> None:
    operation = convert_operation(
        {"parameters": [{"in": "body", "name": "body", "schema": {"type": "object"}}]},
        ["application/xml"],
        ["application/xml"],
    )
    assert "application/xml" in operation["requestBody"]["content"]


def test_consumes_and_produces_do_not_survive() -> None:
    operation = upconvert(SWAGGER2)["paths"]["/rest/v19/things/{id}"]["get"]
    assert "consumes" not in operation and "produces" not in operation
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Thing"
    }
