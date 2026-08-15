"""The compiled index: build correctness, search behaviour, id derivation."""

from __future__ import annotations

from oracle_fusion_mcp.index import SpecIndex, escape_fts_query


def test_build_records_the_read_write_delete_split(mini_index: SpecIndex) -> None:
    counts = mini_index.counts()
    assert counts["total"] == 7
    assert counts["read"] == 4  # 3 GETs on POs/items + the legacy GET
    assert counts["write"] == 2  # POST + PATCH
    assert counts["delete"] == 1


def test_paths_are_stored_with_the_base_path_applied(mini_index: SpecIndex) -> None:
    found = mini_index.get("getall_purchaseOrders")
    assert found is not None
    operation, _ = found
    assert operation.path == "/fscmRestApi/resources/11.13.18.05/purchaseOrders"


def test_path_item_parameters_are_merged_into_each_operation(mini_index: SpecIndex) -> None:
    """The shared REST-Framework-Version header must appear on both operations."""
    for op_id in ("getall_purchaseOrders", "create_purchaseOrder"):
        found = mini_index.get(op_id)
        assert found is not None
        names = {p.get("name") for p in found[1]["parameters"]}
        assert "REST-Framework-Version" in names
    # Operation-level parameters survive the merge too.
    found = mini_index.get("getall_purchaseOrders")
    assert found is not None
    assert {"q", "limit"} <= {p.get("name") for p in found[1]["parameters"]}


def test_operations_without_an_operation_id_still_get_one(mini_index: SpecIndex) -> None:
    operations, _ = mini_index.operations_for_tag("Legacy")
    assert len(operations) == 1
    assert operations[0].op_id
    assert operations[0].method == "GET"


def test_search_ranks_and_reports_totals(mini_index: SpecIndex) -> None:
    operations, total = mini_index.search("purchase order")
    assert total >= 4
    assert any(op.op_id == "getall_purchaseOrders" for op in operations)


def test_search_filters_by_kind_and_method(mini_index: SpecIndex) -> None:
    reads, _ = mini_index.search("purchase order", kind="read")
    assert reads and all(op.kind == "read" for op in reads)

    deletes, _ = mini_index.search("purchase order", kind="delete")
    assert deletes and all(op.method == "DELETE" for op in deletes)

    posts, _ = mini_index.search("purchase order", method="POST")
    assert posts and all(op.method == "POST" for op in posts)


def test_search_limit_is_respected_and_total_still_reported(mini_index: SpecIndex) -> None:
    operations, total = mini_index.search("purchase order", limit=1)
    assert len(operations) == 1
    assert total > 1


def test_prefix_matching_finds_partial_words(mini_index: SpecIndex) -> None:
    operations, _ = mini_index.search("inventor")
    assert any(op.op_id == "getall_inventoryItems" for op in operations)


def test_categories_and_resources_are_derived_from_tags(mini_index: SpecIndex) -> None:
    categories = dict(mini_index.list_categories())
    assert categories["Procurement"] == 5
    assert categories["Inventory Management"] == 1

    resources, total = mini_index.list_resources(category="Procurement")
    assert total == 1
    assert resources[0][0] == "Procurement/Purchase Orders"


def test_components_are_retrievable(mini_index: SpecIndex) -> None:
    schema = mini_index.component("schemas", "PurchaseOrder")
    assert schema["required"] == ["OrderNumber"]
    assert mini_index.component("schemas", "Missing") is None


def test_unknown_operation_returns_none(mini_index: SpecIndex) -> None:
    assert mini_index.get("no_such_operation") is None


def test_find_similar_supports_did_you_mean(mini_index: SpecIndex) -> None:
    similar = mini_index.find_similar("purchaseOrder")
    assert any("purchaseOrder" in op.op_id for op in similar)


class TestFtsEscaping:
    """FTS5 treats several characters as operators; user text must not break the query."""

    def test_quotes_and_operators_are_neutralized(self) -> None:
        for hostile in ('a "b" c', "a AND* (b)", "foo^bar", "x - y", 'unbalanced "quote'):
            assert escape_fts_query(hostile)

    def test_empty_input_yields_empty_query(self) -> None:
        assert escape_fts_query("   ") == ""
        assert escape_fts_query('"*()') == ""

    def test_last_term_gets_a_prefix_wildcard(self) -> None:
        assert escape_fts_query("purchase ord") == '"purchase" AND "ord"*'


def test_hostile_search_input_does_not_raise(mini_index: SpecIndex) -> None:
    for hostile in ('"', "*", "AND", "a OR b", "NEAR(x y)", "col:value", "-neg"):
        operations, total = mini_index.search(hostile)
        assert isinstance(operations, list)
        assert total >= 0
