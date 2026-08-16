"""Postman generation: URL shaping, example bodies, dedupe, and emitted collections."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from oracle_fusion_mcp.index import Operation, SpecIndex
from oracle_fusion_mcp.postman import collection as pc
from oracle_fusion_mcp.postman import emit
from oracle_fusion_mcp.postman.dedupe import SchemaPool, fingerprint
from oracle_fusion_mcp.postman.examples import build_example, request_body_example
from oracle_fusion_mcp.specs import COMMON, CPQ, SCM, SpecDef

BASE_PATH = "/fscmRestApi/resources/11.13.18.05"


class FakeSource:
    """A minimal `component(section, name)` provider for schema-level tests."""

    def __init__(self, schemas: dict[str, Any], **sections: dict[str, Any]) -> None:
        self._sections = {"schemas": schemas, **sections}

    def component(self, section: str, name: str) -> Any | None:
        return self._sections.get(section, {}).get(name)


# -- URLs -----------------------------------------------------------------


def test_whole_segment_placeholder_becomes_a_path_variable() -> None:
    url = pc.to_url(f"{BASE_PATH}/purchaseOrders/{{OrderId}}", base_path=BASE_PATH)
    assert url["path"] == ["{{basePath}}", "purchaseOrders", ":OrderId"]
    assert url["variable"] == [{"key": "OrderId", "value": ""}]
    assert url["raw"] == "{{baseUrl}}/{{basePath}}/purchaseOrders/:OrderId"


def test_base_path_collapses_to_a_single_variable() -> None:
    url = pc.to_url(f"{BASE_PATH}/workOrders", base_path=BASE_PATH)
    assert url["path"][0] == "{{basePath}}"
    assert "fscmRestApi" not in url["raw"]


def test_a_path_rooted_elsewhere_stays_absolute() -> None:
    """Common Features' `/ess` endpoints are not under the spec's base path."""
    url = pc.to_url("/ess/rest/scheduler/v1/requests", base_path=BASE_PATH)
    assert url["path"] == ["ess", "rest", "scheduler", "v1", "requests"]
    assert "{{basePath}}" not in url["raw"]


def test_placeholder_inside_a_larger_segment_becomes_a_variable_reference() -> None:
    """Postman's `:var` spans a whole segment; CPQ's `adminCustom{tableName}` cannot."""
    url = pc.to_url("/rest/v19/adminCustom{tableName}", base_path="/rest/v19")
    assert url["path"] == ["{{basePath}}", "adminCustom{{tableName}}"]
    assert url["_inlineVariables"] == ["tableName"]
    assert "variable" not in url


def test_inline_variables_are_reported_in_the_request_description() -> None:
    item = pc.request_item(
        name="Custom table",
        method="GET",
        path="/rest/v19/adminCustom{tableName}",
        operation_id="getCustom",
        base_path="/rest/v19",
    )
    assert "`tableName`" in item["request"]["description"]
    assert "_inlineVariables" not in item["request"]["url"]


# -- parameters and headers ----------------------------------------------


def test_optional_parameters_ship_disabled_and_required_ones_enabled() -> None:
    item = pc.request_item(
        name="List",
        method="GET",
        path=f"{BASE_PATH}/workOrders",
        operation_id="getall_workOrders",
        base_path=BASE_PATH,
        parameters=[
            {"name": "limit", "in": "query", "schema": {"type": "integer"}},
            {"name": "OrgId", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "REST-Framework-Version", "in": "header", "schema": {"type": "string"}},
        ],
    )
    query = {row["key"]: row for row in item["request"]["url"]["query"]}
    assert query["limit"]["disabled"] is True
    assert "disabled" not in query["OrgId"]
    # Only enabled parameters belong in the raw URL.
    assert item["request"]["url"]["raw"].endswith("?OrgId=")

    headers = {row["key"]: row for row in item["request"]["header"]}
    assert headers["Accept"]["value"] == "application/json"
    assert headers["REST-Framework-Version"]["disabled"] is True


def test_framework_parameters_get_the_shared_note_instead_of_oracle_prose() -> None:
    """`q` and friends repeat hundreds of characters across ~19k requests."""
    item = pc.request_item(
        name="List",
        method="GET",
        path=f"{BASE_PATH}/workOrders",
        operation_id="getall_workOrders",
        base_path=BASE_PATH,
        parameters=[
            {"name": "q", "in": "query", "description": "A" * 500, "schema": {"type": "string"}},
            {"name": "OrgId", "in": "query", "description": "B" * 500, "schema": {"type": "string"}},
        ],
    )
    rows = {row["key"]: row["description"] for row in item["request"]["url"]["query"]}
    assert rows["q"] == pc.FRAMEWORK_PARAM_NOTE
    # A parameter of the spec's own keeps its (trimmed) description.
    assert rows["OrgId"].startswith("B") and len(rows["OrgId"]) <= 161


def test_content_type_header_is_added_only_with_a_body() -> None:
    without = pc.request_item(
        name="Get", method="GET", path="/x", operation_id="get_x", base_path=""
    )
    assert all(row["key"] != "Content-Type" for row in without["request"]["header"])
    assert "body" not in without["request"]

    with_body = pc.request_item(
        name="Post",
        method="POST",
        path="/x",
        operation_id="post_x",
        base_path="",
        body={"A": 1},
        content_type="application/vnd.oracle.adf.action+json",
    )
    headers = {row["key"]: row["value"] for row in with_body["request"]["header"]}
    assert headers["Content-Type"] == "application/vnd.oracle.adf.action+json"
    assert json.loads(with_body["request"]["body"]["raw"]) == {"A": 1}


# -- descriptions and ids ------------------------------------------------


def test_trim_collapses_whitespace_and_truncates() -> None:
    assert pc.trim("  a\n\n  b  ") == "a b"
    trimmed = pc.trim("x" * 500, 50)
    assert len(trimmed) == 50 and trimmed.endswith("…")
    assert pc.trim(None) == ""


def test_ids_are_derived_from_names_so_regeneration_produces_no_diff() -> None:
    assert pc.stable_id("collection", "SCM") == pc.stable_id("collection", "SCM")
    assert pc.stable_id("collection", "SCM") != pc.stable_id("collection", "CX")


# -- example bodies ------------------------------------------------------


def test_a_self_referential_schema_terminates_with_a_marker() -> None:
    source = FakeSource(
        {
            "Node": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "next": {"$ref": "#/components/schemas/Node"}},
            }
        }
    )
    example, truncated = build_example({"$ref": "#/components/schemas/Node"}, source, max_depth=5)
    assert truncated
    flat = json.dumps(example)
    assert "<recursive: Node>" in flat
    # Proof of termination: the structure is finite and serializable.
    assert flat.count("<recursive: Node>") == 1


def test_a_two_schema_cycle_terminates() -> None:
    source = FakeSource(
        {
            "A": {"type": "object", "properties": {"b": {"$ref": "#/components/schemas/B"}}},
            "B": {"type": "object", "properties": {"a": {"$ref": "#/components/schemas/A"}}},
        }
    )
    example, _ = build_example({"$ref": "#/components/schemas/A"}, source, max_depth=10)
    assert "<recursive:" in json.dumps(example)


def test_depth_limit_emits_a_named_truncation_marker() -> None:
    source = FakeSource(
        {
            "One": {"type": "object", "properties": {"two": {"$ref": "#/components/schemas/Two"}}},
            "Two": {"type": "object", "properties": {"three": {"$ref": "#/components/schemas/Three"}}},
            "Three": {"type": "object", "properties": {"name": {"type": "string"}}},
        }
    )
    example, truncated = build_example({"$ref": "#/components/schemas/One"}, source, max_depth=2)
    assert truncated
    assert example["two"]["three"] == "<truncated: Three>"


def test_required_properties_define_the_body_when_declared() -> None:
    schema = {
        "type": "object",
        "required": ["OrderNumber", "SupplierId"],
        "properties": {
            "OrderNumber": {"type": "string"},
            "SupplierId": {"type": "integer"},
            "Comments": {"type": "string"},
        },
    }
    example, _ = build_example(schema, FakeSource({}))
    assert example == {"OrderNumber": "", "SupplierId": 0}


def test_a_required_name_with_no_property_definition_still_appears() -> None:
    schema = {"type": "object", "required": ["Mystery"], "properties": {}}
    example, _ = build_example(schema, FakeSource({}))
    assert "Mystery" in example


def test_without_required_a_capped_sample_of_writable_fields_is_used() -> None:
    schema = {
        "type": "object",
        "properties": {
            **{f"Field{n}": {"type": "string"} for n in range(20)},
            "ReadOnlyField": {"type": "string", "readOnly": True},
            "links": {"type": "array", "items": {"type": "string"}},
        },
    }
    example, truncated = build_example(schema, FakeSource({}), max_properties=5)
    assert len(example) == 5
    assert truncated
    assert "ReadOnlyField" not in example and "links" not in example


def test_values_follow_type_format_and_enum() -> None:
    schema = {
        "type": "object",
        "required": ["when", "day", "state", "count", "flag", "fixed"],
        "properties": {
            "when": {"type": "string", "format": "date-time"},
            "day": {"type": "string", "format": "date"},
            "state": {"type": "string", "enum": ["OPEN", "CLOSED"]},
            "count": {"type": "integer"},
            "flag": {"type": "boolean"},
            "fixed": {"type": "string", "example": "ABC"},
        },
    }
    example, _ = build_example(schema, FakeSource({}))
    assert example["when"].startswith("2026-01-01T")
    assert example["day"] == "2026-01-01"
    assert example["state"] == "OPEN"
    assert example["count"] == 0 and example["flag"] is False
    assert example["fixed"] == "ABC"


def test_arrays_are_emitted_with_one_representative_element() -> None:
    schema = {"type": "array", "items": {"type": "object", "required": ["Id"], "properties": {"Id": {"type": "integer"}}}}
    example, _ = build_example(schema, FakeSource({}))
    assert example == [{"Id": 0}]


def test_all_of_is_flattened() -> None:
    source = FakeSource(
        {"Base": {"type": "object", "required": ["Id"], "properties": {"Id": {"type": "integer"}}}}
    )
    schema = {
        "allOf": [
            {"$ref": "#/components/schemas/Base"},
            {"type": "object", "required": ["Name"], "properties": {"Name": {"type": "string"}}},
        ]
    }
    example, _ = build_example(schema, source)
    assert example == {"Id": 0, "Name": ""}


def test_node_budget_stops_a_runaway_expansion() -> None:
    wide = {
        "type": "object",
        "properties": {f"F{n}": {"type": "object", "properties": {"x": {"type": "string"}}} for n in range(50)},
    }
    example, truncated = build_example(wide, FakeSource({}), max_nodes=5, max_properties=50)
    assert truncated
    assert "node budget" in json.dumps(example)


# -- request body selection ----------------------------------------------


def test_request_body_prefers_json_over_other_content_types() -> None:
    operation = {
        "requestBody": {
            "content": {
                "application/xml": {"schema": {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}},
                "application/json": {"schema": {"type": "object", "required": ["b"], "properties": {"b": {"type": "string"}}}},
            }
        }
    }
    content_type, body, _ = request_body_example(operation, FakeSource({}))
    assert content_type == "application/json" and body == {"b": ""}


def test_request_body_falls_back_to_the_only_declared_content_type() -> None:
    operation = {
        "requestBody": {
            "content": {"application/vnd.oracle.adf.action+json": {"schema": {"type": "object"}}}
        }
    }
    content_type, _, _ = request_body_example(operation, FakeSource({}))
    assert content_type == "application/vnd.oracle.adf.action+json"


def test_a_request_body_behind_a_ref_is_resolved() -> None:
    source = FakeSource(
        {"Thing": {"type": "object", "required": ["Id"], "properties": {"Id": {"type": "integer"}}}},
        requestBodies={
            "thing-post": {
                "required": True,
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}},
            }
        },
    )
    operation = {"requestBody": {"$ref": "#/components/requestBodies/thing-post"}}
    content_type, body, _ = request_body_example(operation, source)
    assert content_type == "application/json" and body == {"Id": 0}


def test_an_operation_without_a_body_yields_nothing() -> None:
    assert request_body_example({}, FakeSource({})) == (None, None, False)


# -- dedupe --------------------------------------------------------------


def test_identical_schemas_pool_to_one_fingerprint() -> None:
    pool = SchemaPool()
    first = {"type": "object", "properties": {"a": {"type": "string"}}}
    # Same content, different key order and different name.
    second = {"properties": {"a": {"type": "string"}}, "type": "object"}
    assert fingerprint(first) == fingerprint(second)
    assert pool.register("First", first) == "First"
    assert pool.register("Second", second) == "First"
    assert pool.distinct == 1 and pool.duplicates == 1


def test_duplicate_schemas_reuse_a_cached_example() -> None:
    source = FakeSource(
        {
            "Left": {"type": "object", "required": ["v"], "properties": {"v": {"type": "string"}}},
            "Right": {"type": "object", "required": ["v"], "properties": {"v": {"type": "string"}}},
        }
    )
    pool = SchemaPool()
    left, _ = build_example({"$ref": "#/components/schemas/Left"}, source, pool=pool)
    right, _ = build_example({"$ref": "#/components/schemas/Right"}, source, pool=pool)
    assert left == right
    assert pool.hits == 1
    assert pool.stats()["duplicate_schemas"] == 1


def test_a_recursive_subtree_is_not_cached() -> None:
    """A cycle marker depends on the enclosing ref chain, so it must not be reused."""
    source = FakeSource(
        {"Node": {"type": "object", "properties": {"next": {"$ref": "#/components/schemas/Node"}}}}
    )
    pool = SchemaPool()
    build_example({"$ref": "#/components/schemas/Node"}, source, pool=pool, max_depth=4)
    assert pool.stats()["example_cache_size"] == 0


# -- emitted collections -------------------------------------------------


def test_child_depth_is_measured_from_the_path() -> None:
    assert emit.child_depth("/a/b") == 0
    assert emit.child_depth("/a/{i}/child/b") == 1
    assert emit.child_depth("/a/{i}/child/b/{j}/child/c") == 2


def test_safe_filename_strips_path_separators() -> None:
    assert emit.safe_filename("SCM — Order/Management") == "SCM — Order-Management"
    assert emit.safe_filename("  spaced   out  ") == "spaced out"


class FakeIndex:
    """Just enough of `SpecIndex` for `group_operations`."""

    title = "Fake Spec"

    def __init__(self, operations: list[Operation]) -> None:
        self._operations = operations

    def iter_all(self) -> Iterator[tuple[Operation, dict[str, Any]]]:
        for operation in self._operations:
            yield operation, {"parameters": []}

    def component(self, section: str, name: str) -> Any | None:
        return None


def _operation(path: str, op_id: str = "op") -> Operation:
    return Operation(
        op_id=op_id,
        method="GET",
        path=path,
        kind="read",
        category="Cat",
        tag="Cat/Res",
        resource="res",
        summary="",
    )


def test_operations_deeper_than_the_cap_are_skipped_and_recorded() -> None:
    operations = [
        _operation(f"{BASE_PATH}/orders", "a"),
        _operation(f"{BASE_PATH}/orders/{{i}}/child/lines", "b"),
        _operation(f"{BASE_PATH}/orders/{{i}}/child/lines/{{j}}/child/notes", "c"),
        _operation(f"{BASE_PATH}/orders/{{i}}/child/lines/{{j}}/child/notes/{{k}}/child/deep", "d"),
    ]
    stats = emit.BuildStats(key="fake")
    tree, skipped = emit.group_operations(FakeIndex(operations), 2, stats)
    assert stats.skipped_depth == 1
    assert [operation.op_id for operation, _ in skipped] == ["d"]
    assert len(tree["Cat"]["res"]) == 3


def test_skipped_manifest_is_tab_separated_with_a_header(tmp_path: Path) -> None:
    destination = tmp_path / "skipped.tsv"
    emit.write_skipped_manifest(
        destination, [("scm", _operation("/a/child/b", "deep_op"), "child depth 3 > 2")]
    )
    lines = destination.read_text(encoding="utf-8").strip().split("\n")
    assert lines[0].split("\t") == ["spec", "method", "path", "operation_id", "reason"]
    assert lines[1].split("\t") == ["scm", "GET", "/a/child/b", "deep_op", "child depth 3 > 2"]


# -- end to end over the compiled mini spec ------------------------------


def _requests(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for item in node.get("item", []):
        if "request" in item:
            yield item
        else:
            yield from _requests(item)


@pytest.fixture
def built(
    tmp_path: Path, mini_definition: SpecDef, mini_index: SpecIndex
) -> tuple[emit.BuildStats, dict[str, Any]]:
    stats, _ = emit.build_collections(mini_definition, mini_index, tmp_path)
    document = json.loads(stats.files[0].read_text(encoding="utf-8"))
    return stats, document


def test_the_collection_declares_the_v2_1_schema_and_basic_auth(
    built: tuple[emit.BuildStats, dict[str, Any]],
) -> None:
    _, document = built
    assert document["info"]["schema"] == pc.SCHEMA_URL
    assert document["auth"]["type"] == "basic"
    variables = {entry["key"]: entry["value"] for entry in document["variable"]}
    assert variables["baseUrl"] == "https://{{host}}"
    assert variables["basePath"] == BASE_PATH.strip("/")


def test_every_operation_in_the_index_becomes_a_request(
    built: tuple[emit.BuildStats, dict[str, Any]], mini_index: SpecIndex
) -> None:
    stats, document = built
    assert stats.emitted == mini_index.operation_count
    assert len(list(_requests(document))) == mini_index.operation_count


def test_folders_mirror_category_then_resource(
    built: tuple[emit.BuildStats, dict[str, Any]],
) -> None:
    _, document = built
    categories = {folder["name"] for folder in document["item"]}
    assert {"Procurement", "Inventory Management", "Legacy"} <= categories
    procurement = next(f for f in document["item"] if f["name"] == "Procurement")
    assert [child["name"] for child in procurement["item"]] == ["purchaseOrders"]


def test_no_ref_or_unexpanded_marker_survives_into_a_request(
    built: tuple[emit.BuildStats, dict[str, Any]],
) -> None:
    _, document = built
    for item in _requests(document):
        raw = item["request"].get("body", {}).get("raw", "")
        assert "$ref" not in raw
        assert "$ref_unexpanded" not in raw


def test_the_post_body_carries_exactly_the_required_fields(
    built: tuple[emit.BuildStats, dict[str, Any]],
) -> None:
    _, document = built
    post = next(item for item in _requests(document) if item["request"]["method"] == "POST")
    assert json.loads(post["request"]["body"]["raw"]) == {"OrderNumber": ""}


def test_a_cycle_read_through_the_real_index_is_cut_with_a_marker(
    mini_index: SpecIndex,
) -> None:
    """`Bin` -> `BinContent` -> `Bin`, resolved through a compiled SQLite index.

    Read through `SpecIndex` rather than a stub, so the cycle is resolved exactly
    as a real spec's would be.
    """
    example, truncated = build_example(
        {"$ref": "#/components/schemas/Bin"}, mini_index, max_depth=6
    )
    assert truncated
    assert example == {"BinCode": "", "Contents": {"Quantity": 0, "Bin": "<recursive: Bin>"}}


def test_required_only_pruning_can_stop_short_of_a_cycle(mini_index: SpecIndex) -> None:
    """Why production bodies show no recursion markers.

    `OrderLine` -> `PurchaseOrder` -> `OrderLine` is a genuine cycle, but
    `PurchaseOrder` declares `required`, so the body stops at those fields and
    never follows the edge back.
    """
    example, _ = build_example(
        {"$ref": "#/components/schemas/OrderLine"}, mini_index, max_depth=6
    )
    assert example == {"LineNumber": 0, "Parent": {"OrderNumber": ""}}


def test_output_is_byte_identical_when_regenerated(
    tmp_path: Path, mini_definition: SpecDef, mini_index: SpecIndex
) -> None:
    first, _ = emit.build_collections(mini_definition, mini_index, tmp_path / "one")
    second, _ = emit.build_collections(mini_definition, mini_index, tmp_path / "two")
    assert first.files[0].read_bytes() == second.files[0].read_bytes()


def test_an_oversized_collection_is_split_into_several(
    tmp_path: Path, mini_definition: SpecDef, mini_index: SpecIndex
) -> None:
    stats, _ = emit.build_collections(mini_definition, mini_index, tmp_path, max_bytes=2_000)
    assert len(stats.files) > 1
    names = [json.loads(path.read_text(encoding="utf-8"))["info"]["name"] for path in stats.files]
    assert all(mini_index.title in name for name in names)
    # Every request still lands somewhere.
    total = sum(
        len(list(_requests(json.loads(path.read_text(encoding="utf-8")))))
        for path in stats.files
    )
    assert total == mini_index.operation_count


# -- against the real compiled indexes -----------------------------------
#
# Skipped when the indexes have not been built, so a fresh clone still runs green.


def real_index(definition: SpecDef) -> SpecIndex:
    from oracle_fusion_mcp.config import index_dir

    path = index_dir() / definition.index_filename
    if not path.exists():
        pytest.skip(f"{path.name} not built — run: uv run oracle-fusion-build-index")
    return SpecIndex(path)


@pytest.mark.parametrize("definition", [COMMON, CPQ], ids=["common", "cpq"])
def test_a_real_spec_emits_a_usable_collection(definition: SpecDef, tmp_path: Path) -> None:
    """Common Features and CPQ are the awkward two: four path dialects, and Swagger 2.0."""
    index = real_index(definition)
    try:
        stats, skipped = emit.build_collections(definition, index, tmp_path)
        documents = [json.loads(path.read_text(encoding="utf-8")) for path in stats.files]
    finally:
        index.close()

    assert stats.emitted + stats.skipped_depth == index.operation_count
    emitted = [item for document in documents for item in _requests(document)]
    assert len(emitted) == stats.emitted

    for item in emitted:
        url = item["request"]["url"]
        assert url["raw"].startswith("{{baseUrl}}/")
        # No unresolved placeholder may reach a URL: every `{...}` is either a
        # Postman `:variable` or a `{{variable}}` reference.
        assert "{" not in url["raw"].replace("{{", "").replace("}}", "")
        body = item["request"].get("body", {}).get("raw", "")
        assert "$ref" not in body and "$ref_unexpanded" not in body


def test_no_ref_survives_a_body_anywhere_in_scm() -> None:
    """The widest available check on the schema layer: every SCM write body."""
    index = real_index(SCM)
    try:
        checked = 0
        for operation, detail in index.iter_all():
            if operation.kind == "read":
                continue
            _, body, _ = request_body_example(detail, index)
            if body is None:
                continue
            checked += 1
            flat = json.dumps(body)
            assert "$ref" not in flat, operation.op_id
            assert "$ref_unexpanded" not in flat, operation.op_id
    finally:
        index.close()
    assert checked > 1_000, f"expected thousands of SCM bodies, generated {checked}"
