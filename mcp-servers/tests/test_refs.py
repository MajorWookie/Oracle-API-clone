"""`$ref` resolution: bounded expansion, cycle safety, schema summarizing."""

from __future__ import annotations

from typing import Any

from oracle_fusion_mcp.refs import parse_ref, resolve, summarize_schema


class FakeSource:
    """Stands in for SpecIndex.component."""

    def __init__(self, components: dict[str, dict[str, Any]]) -> None:
        self._components = components

    def component(self, section: str, name: str) -> Any | None:
        return self._components.get(section, {}).get(name)


def test_parse_ref_handles_component_refs_and_rejects_external() -> None:
    assert parse_ref("#/components/schemas/Item") == ("schemas", "Item")
    assert parse_ref("https://example.com/schema.json") is None
    assert parse_ref("#/definitions/Item") is None


def test_refs_expand_into_the_document() -> None:
    source = FakeSource({"schemas": {"Item": {"type": "object", "properties": {"id": {"type": "string"}}}}})
    result = resolve({"schema": {"$ref": "#/components/schemas/Item"}}, source)
    assert result["schema"]["properties"]["id"]["type"] == "string"


def test_self_referential_schema_terminates() -> None:
    """Oracle parent/child schemas reference themselves; expansion must not hang."""
    source = FakeSource(
        {"schemas": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/components/schemas/Node"}}}}}
    )
    result = resolve({"$ref": "#/components/schemas/Node"}, source, max_depth=5)
    assert result["properties"]["child"]["$ref_unexpanded"] == "Node"


def test_same_name_in_two_sections_is_not_a_cycle() -> None:
    """Oracle reuses one name across `requestBodies` and `schemas`.

    `workOrders-item-post-request` exists in both, and the requestBodies entry
    refers to the schemas entry. Keying cycle detection on the name alone made
    that ordinary reference look circular and hid the request body of nearly
    every POST operation in the SCM and CX specs.
    """
    name = "workOrders-item-post-request"
    source = FakeSource(
        {
            "requestBodies": {
                name: {"content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}}}
            },
            "schemas": {
                name: {"type": "object", "properties": {"WorkOrderNumber": {"type": "string"}}}
            },
        }
    )
    result = resolve({"$ref": f"#/components/requestBodies/{name}"}, source, max_depth=3)
    schema = result["content"]["application/json"]["schema"]
    assert "$ref_unexpanded" not in schema
    assert schema["properties"]["WorkOrderNumber"]["type"] == "string"


def test_a_true_cycle_within_one_section_is_still_caught() -> None:
    source = FakeSource(
        {"schemas": {"A": {"properties": {"a": {"$ref": "#/components/schemas/A"}}}}}
    )
    result = resolve({"$ref": "#/components/schemas/A"}, source, max_depth=6)
    assert result["properties"]["a"]["$ref_unexpanded"] == "A"


def test_depth_limit_leaves_a_named_marker() -> None:
    source = FakeSource(
        {
            "schemas": {
                "A": {"properties": {"b": {"$ref": "#/components/schemas/B"}}},
                "B": {"properties": {"c": {"$ref": "#/components/schemas/C"}}},
                "C": {"type": "string"},
            }
        }
    )
    result = resolve({"$ref": "#/components/schemas/A"}, source, max_depth=1)
    assert result["properties"]["b"]["$ref_unexpanded"] == "B"


def test_missing_component_is_reported_not_raised() -> None:
    result = resolve({"$ref": "#/components/schemas/Nope"}, FakeSource({}))
    assert result["$ref_unexpanded"] == "Nope"
    assert "not found" in result["note"]


def test_sibling_keys_survive_expansion() -> None:
    source = FakeSource({"schemas": {"Item": {"type": "object"}}})
    result = resolve(
        {"$ref": "#/components/schemas/Item", "description": "an item"}, FakeSource({"schemas": {"Item": {"type": "object"}}})
    )
    assert result["type"] == "object"
    assert result["description"] == "an item"
    assert source is not None


def test_node_budget_caps_runaway_expansion() -> None:
    big = {"properties": {f"p{i}": {"type": "string"} for i in range(500)}}
    result = resolve(big, FakeSource({}), max_nodes=20)
    rendered = str(result)
    assert "size limit" in rendered


def test_summarize_schema_condenses_properties_and_reports_omissions() -> None:
    schema = {
        "type": "object",
        "required": ["Id"],
        "properties": {
            f"Field{i}": {"type": "string", "description": "x" * 500} for i in range(80)
        },
    }
    summary = summarize_schema(schema, max_properties=10)
    assert len(summary["properties"]) == 10
    assert "70 further properties" in summary["properties_omitted"]
    assert summary["required"] == ["Id"]
    # Long descriptions are clipped so a summary stays readable.
    assert len(next(iter(summary["properties"].values()))["description"]) <= 180


def test_required_properties_are_never_dropped_by_the_cap() -> None:
    """A create body's required fields must survive truncation, wherever they sort."""
    properties = {f"Optional{i}": {"type": "string"} for i in range(100)}
    properties["zzRequired"] = {"type": "string"}
    schema = {"type": "object", "required": ["zzRequired"], "properties": properties}

    summary = summarize_schema(schema, max_properties=5)
    assert "zzRequired" in summary["properties"]
    assert summary["properties"]["zzRequired"]["required"] is True
    # And it sorts ahead of the optional ones.
    assert next(iter(summary["properties"])) == "zzRequired"


def test_summarize_schema_handles_arrays() -> None:
    summary = summarize_schema({"type": "array", "items": {"type": "object", "properties": {"a": {"type": "integer"}}}})
    assert summary["type"] == "array"
    assert summary["items"]["properties"]["a"]["type"] == "integer"
