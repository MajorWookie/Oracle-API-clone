"""Smoke tests against the real compiled Oracle indexes.

Skipped when the indexes have not been built, so a fresh clone can still run the
rest of the suite.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client

from oracle_fusion_mcp.config import index_dir, load
from oracle_fusion_mcp.index import SpecIndex
from oracle_fusion_mcp.paths import contains_base_path, has_api_root
from oracle_fusion_mcp.server import create_server
from oracle_fusion_mcp.specs import ALL_SPECS, SpecDef

pytestmark = pytest.mark.parametrize("definition", ALL_SPECS, ids=[s.key for s in ALL_SPECS])


def require_mcp(definition: SpecDef) -> None:
    """Skip a server-level test for specs that are indexed but not wrapped over MCP."""
    if not definition.mcp_server:
        pytest.skip(f"{definition.key} is indexed for Postman export only")


def index_for(definition: SpecDef) -> SpecIndex:
    path = index_dir() / definition.index_filename
    if not path.exists():
        pytest.skip(f"{path.name} not built — run: uv run oracle-fusion-build-index")
    return SpecIndex(path)


#: The operation counts observed when the indexes were first compiled. A drift
#: here means a spec was refreshed, not that the code broke.
EXPECTED_MINIMUMS = {"scm": 10_000, "cx": 8_000, "common": 400, "cpq": 900}


def test_index_holds_the_full_catalog(definition: SpecDef) -> None:
    index = index_for(definition)
    counts = index.counts()
    assert counts["total"] >= EXPECTED_MINIMUMS[definition.key]
    assert counts["read"] and counts["write"] and counts["delete"]
    assert counts["read"] + counts["write"] + counts["delete"] == counts["total"]


def test_every_stored_path_is_absolute_and_rooted(definition: SpecDef) -> None:
    """No `<servername>` placeholder or bare resource may survive compilation.

    A path is rooted either by carrying a recognized Oracle API root or by
    already containing the spec's own base path — CPQ writes `/rest/v19/...`
    into its path keys, which is a root but not an `*Api` one.
    """
    index = index_for(definition)
    rows = index._connection.execute("SELECT op_id, path FROM operations").fetchall()
    for row in rows:
        path = row["path"]
        assert path.startswith("/"), row["op_id"]
        assert "servername" not in path.lower(), row["op_id"]
        assert "<" not in path and "://" not in path, row["op_id"]
        rooted = has_api_root(path) or contains_base_path(path, definition.default_base_path)
        assert rooted, f"{row['op_id']} -> {path}"


def test_no_path_is_double_prefixed(definition: SpecDef) -> None:
    """The base path must be applied exactly once.

    Checked in Python rather than SQL because SQLite's LIKE is case-insensitive,
    and both large specs contain a resource legitimately named `Resources` —
    CX's `/crmRestApi/resources/11.13.18.05/resources/{PartyNumber}` is correct,
    not a doubled prefix.
    """
    index = index_for(definition)
    version_segment = "/resources/11.13.18.05/"
    offenders = [
        dict(row)
        for row in index._connection.execute("SELECT op_id, path FROM operations")
        if row["path"].count("RestApi") > 1
        or row["path"].count(version_segment) > 1
        or row["path"].count(definition.default_base_path) > 1
    ]
    assert not offenders, offenders[:5]


#: A domain term each spec is certain to mention.
PROBES = {"scm": "inventory", "cx": "opportunity", "common": "user", "cpq": "pricing"}


def test_search_finds_a_known_domain_term(definition: SpecDef) -> None:
    index = index_for(definition)
    probe = PROBES[definition.key]
    operations, total = index.search(probe, limit=5)
    assert total > 0, probe
    assert operations


async def test_server_starts_and_lists_its_tools(definition: SpecDef) -> None:
    """The real server must construct and expose its tools without credentials."""
    require_mcp(definition)
    if not (index_dir() / definition.index_filename).exists():
        pytest.skip("index not built")
    server = create_server(definition, load(definition))
    async with Client(server) as client:
        tools = {t.name for t in await client.list_tools()}
    assert "search_operations" in tools
    assert "invoke_read" in tools and "invoke_write" in tools and "invoke_delete" in tools


async def test_describe_operation_works_on_a_real_operation(definition: SpecDef) -> None:
    require_mcp(definition)
    index = index_for(definition)
    probe = PROBES[definition.key]
    operations, _ = index.search(probe, kind="read", limit=1)
    if not operations:
        pytest.skip("no read operation matched the probe")

    server = create_server(definition, load(definition))
    async with Client(server) as client:
        result = await client.call_tool(
            "describe_operation", {"operation_id": operations[0].op_id}
        )
    payload = json.loads(result.content[0].text)
    assert payload["operation_id"] == operations[0].op_id
    assert payload["invoke_with"] == "invoke_read"


def test_deeply_nested_schemas_resolve_without_hanging(definition: SpecDef) -> None:
    """Oracle schemas are self-referential; describe must always terminate."""
    from oracle_fusion_mcp.refs import resolve

    index = index_for(definition)
    rows = index._connection.execute(
        "SELECT op_id FROM operations WHERE kind = 'read' LIMIT 25"
    ).fetchall()
    for row in rows:
        found = index.get(row["op_id"])
        assert found is not None
        resolved = resolve(found[1].get("responses", {}), index, max_depth=3)
        # Serializing proves there are no cycles left in the returned structure.
        assert len(json.dumps(resolved, default=str)) >= 0
