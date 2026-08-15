"""Compile an Oracle OpenAPI spec into a compact SQLite index.

The specs are far too large to load at server start — SCM alone is 287 MB of
JSON describing 10,335 operations and 22,405 schemas. Parsing that on every
stdio launch would take tens of seconds and gigabytes of RAM.

This module does the parse once, ahead of time, and writes a SQLite database
containing:

  * one row per operation, with its fully merged parameter list
  * an FTS5 index over path/summary/description/tag for `search_operations`
  * the spec's `components` sections, for `$ref` resolution at query time

Operation and component bodies are stored as zlib-compressed JSON blobs, which
keeps the SCM index roughly 8x smaller than the raw spec.

Usage:
    uv run oracle-fusion-build-index            # build all three
    uv run oracle-fusion-build-index scm cx     # build a subset
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
import time
import zlib
from pathlib import Path
from typing import Any, Iterator

from .paths import normalize_path
from .specs import ALL_SPECS, SPECS_BY_KEY, SpecDef

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

#: Methods that only read state. Everything else mutates and is routed to the
#: write/delete tools, per the MCP read/write tool-split requirement.
READ_METHODS = frozenset({"get", "head", "options"})

#: Component sections worth carrying into the index for `$ref` resolution.
COMPONENT_SECTIONS = ("schemas", "parameters", "requestBodies", "responses", "headers")

SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE operations (
    op_id       TEXT PRIMARY KEY,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,   -- normalized, absolute, host-free
    raw_path    TEXT NOT NULL,   -- the spec's original path key
    kind        TEXT NOT NULL,   -- 'read' | 'write' | 'delete'
    category    TEXT,            -- first segment of the tag
    tag         TEXT,            -- full tag
    resource    TEXT,            -- root resource segment of the path
    summary     TEXT,
    detail      BLOB NOT NULL    -- zlib-compressed JSON of the merged operation
);

CREATE INDEX idx_operations_kind     ON operations(kind);
CREATE INDEX idx_operations_category ON operations(category);
CREATE INDEX idx_operations_tag      ON operations(tag);
CREATE INDEX idx_operations_resource ON operations(resource);
CREATE INDEX idx_operations_path     ON operations(path);

CREATE VIRTUAL TABLE operations_fts USING fts5(
    op_id UNINDEXED,
    path,
    summary,
    description,
    tag,
    resource,
    tokenize = 'porter unicode61'
);

CREATE TABLE components (
    section TEXT NOT NULL,
    name    TEXT NOT NULL,
    body    BLOB NOT NULL,       -- zlib-compressed JSON
    PRIMARY KEY (section, name)
);
"""


def load_spec(path: Path) -> dict[str, Any]:
    """Read a spec from `.json` or `.json.gz` without decompressing to disk."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def pack(obj: Any) -> bytes:
    """Compress a JSON-serializable object for storage."""
    return zlib.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), level=6)


def classify(method: str) -> str:
    """Bucket an HTTP method into the read / write / delete tool split."""
    if method in READ_METHODS:
        return "read"
    if method == "delete":
        return "delete"
    return "write"


def merge_parameters(
    path_item: dict[str, Any], operation: dict[str, Any]
) -> list[dict[str, Any]]:
    """Combine path-item-level parameters with the operation's own.

    OpenAPI lets a path item declare parameters shared by every operation under
    it. Operation-level parameters override path-level ones with the same
    name+location, so the operation's entries win.
    """
    shared = path_item.get("parameters") or []
    own = operation.get("parameters") or []

    def identity(param: dict[str, Any]) -> tuple[str, str]:
        # `$ref` parameters have no name until resolved; key them by the ref itself.
        if "$ref" in param:
            return ("$ref", param["$ref"])
        return (param.get("name", ""), param.get("in", ""))

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for param in shared:
        if isinstance(param, dict):
            merged[identity(param)] = param
    for param in own:
        if isinstance(param, dict):
            merged[identity(param)] = param
    return list(merged.values())


def derive_op_id(operation: dict[str, Any], method: str, path: str, taken: set[str]) -> str:
    """Produce a stable, unique operation id.

    Prefers the spec's `operationId`. The Common Features spec leaves 83
    operations without one and reuses three ids (`search`, `read`, `update`)
    across different resources, so collisions and blanks fall back to a
    method+path slug, which is unique by construction.
    """
    raw = (operation.get("operationId") or "").strip()
    if raw and raw not in taken:
        return raw

    slug = path.strip("/").replace("/", ".").replace("{", "").replace("}", "")
    candidate = f"{method.upper()} {slug}" if not raw else f"{raw} [{method.upper()} {slug}]"
    if candidate not in taken:
        return candidate

    suffix = 2
    while f"{candidate}#{suffix}" in taken:
        suffix += 1
    return f"{candidate}#{suffix}"


def iter_operations(spec: dict[str, Any], definition: SpecDef) -> Iterator[dict[str, Any]]:
    """Yield one index row per operation in the spec."""
    taken: set[str] = set()

    for raw_path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue

        path = normalize_path(
            raw_path, definition.default_base_path, enabled=definition.normalize_paths
        )
        # The resource segment is the first path element after the API base.
        base_len = len(definition.default_base_path.strip("/").split("/"))
        segments = path.strip("/").split("/")
        resource = segments[base_len] if len(segments) > base_len else segments[-1]

        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue

            op_id = derive_op_id(operation, method, path, taken)
            taken.add(op_id)

            tags = operation.get("tags") or []
            tag = tags[0] if tags else ""
            category = tag.split("/")[0] if tag else ""

            detail = dict(operation)
            detail["parameters"] = merge_parameters(path_item, operation)
            detail["method"] = method.upper()
            detail["path"] = path

            yield {
                "op_id": op_id,
                "method": method.upper(),
                "path": path,
                "raw_path": raw_path,
                "kind": classify(method),
                "category": category,
                "tag": tag,
                "resource": resource,
                "summary": (operation.get("summary") or "").strip(),
                "description": (operation.get("description") or "").strip(),
                "detail": detail,
            }


def build(definition: SpecDef, spec_root: Path, out_dir: Path) -> Path:
    """Compile one spec into its SQLite index. Returns the database path."""
    source = spec_root / definition.spec_filename
    if not source.exists():
        raise FileNotFoundError(
            f"Spec not found: {source}\n"
            f"Expected the Oracle spec files in {spec_root}."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / definition.index_filename
    staging = destination.with_suffix(".db.tmp")
    staging.unlink(missing_ok=True)

    started = time.monotonic()
    print(f"[{definition.key}] reading {source.name} ...", flush=True)
    spec = load_spec(source)

    connection = sqlite3.connect(staging)
    try:
        # Bulk-load settings: this database is rebuilt from scratch on failure,
        # so durability during the build buys nothing.
        connection.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
        connection.executescript(SCHEMA)

        count = 0
        by_kind: dict[str, int] = {}
        for row in iter_operations(spec, definition):
            connection.execute(
                "INSERT INTO operations "
                "(op_id, method, path, raw_path, kind, category, tag, resource, summary, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    row["op_id"],
                    row["method"],
                    row["path"],
                    row["raw_path"],
                    row["kind"],
                    row["category"],
                    row["tag"],
                    row["resource"],
                    row["summary"],
                    pack(row["detail"]),
                ),
            )
            connection.execute(
                "INSERT INTO operations_fts "
                "(op_id, path, summary, description, tag, resource) VALUES (?,?,?,?,?,?)",
                (
                    row["op_id"],
                    row["path"],
                    row["summary"],
                    row["description"][:4000],
                    row["tag"],
                    row["resource"],
                ),
            )
            count += 1
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1

        components = spec.get("components", {})
        component_count = 0
        for section in COMPONENT_SECTIONS:
            entries = components.get(section) or {}
            for name, body in entries.items():
                connection.execute(
                    "INSERT OR REPLACE INTO components (section, name, body) VALUES (?,?,?)",
                    (section, name, pack(body)),
                )
                component_count += 1

        info = spec.get("info", {})
        meta = {
            "spec_key": definition.key,
            "spec_title": info.get("title", definition.server_name),
            "spec_version": str(info.get("version", "")),
            "openapi": str(spec.get("openapi", "")),
            "default_base_path": definition.default_base_path,
            "source_filename": definition.spec_filename,
            "operation_count": str(count),
            "component_count": str(component_count),
            "read_count": str(by_kind.get("read", 0)),
            "write_count": str(by_kind.get("write", 0)),
            "delete_count": str(by_kind.get("delete", 0)),
            "index_format": "1",
        }
        connection.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", meta.items()
        )

        connection.commit()
        connection.executescript("INSERT INTO operations_fts(operations_fts) VALUES('optimize');")
        connection.commit()
        connection.execute("VACUUM")
        connection.commit()
    finally:
        connection.close()

    staging.replace(destination)
    size_mb = destination.stat().st_size / 1_048_576
    elapsed = time.monotonic() - started
    print(
        f"[{definition.key}] {count:,} operations "
        f"({by_kind.get('read', 0):,} read / {by_kind.get('write', 0):,} write / "
        f"{by_kind.get('delete', 0):,} delete), "
        f"{component_count:,} components -> {destination.name} "
        f"({size_mb:.1f} MB, {elapsed:.1f}s)",
        flush=True,
    )
    return destination


def default_spec_root() -> Path:
    """The repo root, where the Oracle spec files live (one level above this package)."""
    return Path(__file__).resolve().parents[3]


def default_index_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "indexes"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="oracle-fusion-build-index",
        description="Compile Oracle Fusion OpenAPI specs into SQLite search indexes.",
    )
    parser.add_argument(
        "specs",
        nargs="*",
        choices=[s.key for s in ALL_SPECS],
        help="Which specs to build. Defaults to all three.",
    )
    parser.add_argument(
        "--spec-root",
        type=Path,
        default=default_spec_root(),
        help="Directory holding the Oracle spec files.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=default_index_dir(),
        help="Directory to write the .db indexes into.",
    )
    args = parser.parse_args(argv)

    selected = [SPECS_BY_KEY[k] for k in args.specs] if args.specs else list(ALL_SPECS)

    failures = 0
    for definition in selected:
        try:
            build(definition, args.spec_root, args.out)
        except FileNotFoundError as error:
            print(f"[{definition.key}] SKIPPED: {error}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
