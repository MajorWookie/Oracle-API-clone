"""Assemble Postman collections from a compiled spec index.

Folders mirror the spec's own taxonomy: tag category, then root resource. Item
order and folder order are alphabetical so that regenerating an unchanged spec
produces no diff.

Two things are bounded here rather than in the schema layer:

  * **Child nesting.** SCM nests `/child/` segments up to seven deep. Beyond
    `max_child_depth` an operation is recorded in the skipped manifest instead of
    emitted, so the omission is auditable rather than silent.
  * **Collection size.** A single collection holding all of SCM would be tens of
    megabytes, which Postman imports slowly at best. When a collection exceeds
    `max_bytes` it is split per category, and a category that is still too large
    is split again into numbered parts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..index import Operation, SpecIndex
from ..specs import SpecDef
from . import collection as pc
from .dedupe import SchemaPool
from .examples import request_body_example

#: Default ceiling on `/child/` nesting. Depth 2 keeps roughly 9 in 10 operations.
DEFAULT_MAX_CHILD_DEPTH = 2

#: Split a collection when its serialized form exceeds this many bytes.
DEFAULT_MAX_BYTES = 15 * 1024 * 1024

COLLECTION_PREAMBLE = """\
Generated from `{source}` by `oracle-fusion-build-postman`.

{blurb}

**Setup.** Import the companion environment file, set `host` to your pod
(`your-pod.fa.us2.oraclecloud.com`), then fill in `username` and `password`. The
collection authenticates with HTTP Basic at the collection level; for OAuth,
change the collection's auth type to Bearer Token and use `{{{{token}}}}`.

Every request URL is built as `{{{{baseUrl}}}}/{{{{basePath}}}}/...`, where
`basePath` is a collection variable defaulting to `{base_path}`. Change it in one
place if Oracle has bumped the resource version on your pod. Endpoints that carry
a different API root of their own are absolute and unaffected.

**Framework query parameters.** Oracle's collection resources accept a shared
set of query parameters, listed disabled on every applicable request:

- `q` — filter expression, e.g. `WorkOrderNumber='WO-1001'`
- `fields` — comma-separated list of attributes to return
- `limit` / `offset` — page size and starting row
- `orderBy` — sort, e.g. `CreationDate:desc`
- `expand` — include named child resources inline
- `finder` — named finder, e.g. `PrimaryKey;OrderId=300100`
- `totalResults` — set `true` to include the total row count
- `onlyData` — set `true` to omit `links` from the payload

**Request bodies** carry each schema's required fields where it declares any,
otherwise a capped sample of writable fields. `$ref` expansion stops at depth 3;
a self-referential schema shows `<recursive: Name>`. Treat a body as a starting
point, not a complete payload.

**Caveats.** Base paths are inferred from the URLs the specs embed rather than
declared in a `servers` block, and no request here has been issued against a
live pod. Operations nested more than {max_child_depth} `/child/` levels deep are
not included; see `skipped-operations.tsv`.
"""


@dataclass
class BuildStats:
    """What one spec's build produced, for the CLI summary."""

    key: str
    emitted: int = 0
    skipped_depth: int = 0
    bodies: int = 0
    truncated_bodies: int = 0
    files: list[Path] = field(default_factory=list)
    pool: dict[str, int] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.files if path.exists())


def child_depth(path: str) -> int:
    """How many `/child/` levels a path descends."""
    return path.count("/child/")


def item_name(operation: Operation) -> str:
    """A readable request name: the spec's summary, or the method and path."""
    if operation.summary:
        return pc.trim(operation.summary, 120)
    return f"{operation.method} {operation.path.rsplit('/', 1)[-1] or operation.path}"


def build_item(
    operation: Operation,
    detail: dict[str, Any],
    index: SpecIndex,
    pool: SchemaPool,
    stats: BuildStats,
    base_path: str = "",
) -> dict[str, Any]:
    """Convert one indexed operation into a Postman request item."""
    content_type, body, truncated = request_body_example(detail, index, pool=pool)
    if body is not None:
        stats.bodies += 1
        if truncated:
            stats.truncated_bodies += 1

    description = pc.trim(detail.get("description") or operation.summary, 240)
    if truncated:
        description = (
            f"{description}\n\nBody is abbreviated — expansion stopped at the depth or "
            "property limit."
        ).strip()

    return pc.request_item(
        name=item_name(operation),
        method=operation.method,
        path=operation.path,
        operation_id=operation.op_id,
        parameters=detail.get("parameters") or [],
        body=body,
        content_type=content_type,
        description=description,
        base_path=base_path,
    )


def group_operations(
    index: SpecIndex, max_child_depth: int, stats: BuildStats
) -> tuple[dict[str, dict[str, list[tuple[Operation, dict[str, Any]]]]], list[tuple[Operation, str]]]:
    """Bucket operations by category then resource, collecting what was skipped."""
    tree: dict[str, dict[str, list[tuple[Operation, dict[str, Any]]]]] = {}
    skipped: list[tuple[Operation, str]] = []

    for operation, detail in index.iter_all():
        depth = child_depth(operation.path)
        if depth > max_child_depth:
            stats.skipped_depth += 1
            skipped.append((operation, f"child depth {depth} > {max_child_depth}"))
            continue
        category = operation.category or "Uncategorized"
        resource = operation.resource or "misc"
        tree.setdefault(category, {}).setdefault(resource, []).append((operation, detail))

    return tree, skipped


def _folders_for(
    tree: dict[str, dict[str, list[tuple[Operation, dict[str, Any]]]]],
    index: SpecIndex,
    pool: SchemaPool,
    stats: BuildStats,
    base_path: str = "",
) -> dict[str, dict[str, Any]]:
    """One Postman folder per category, each holding one folder per resource."""
    folders: dict[str, dict[str, Any]] = {}
    for category in sorted(tree):
        resource_folders = []
        for resource in sorted(tree[category]):
            items = []
            for operation, detail in tree[category][resource]:
                items.append(build_item(operation, detail, index, pool, stats, base_path))
                stats.emitted += 1
            resource_folders.append(pc.folder(resource, items))
        folders[category] = pc.folder(category, resource_folders)
    return folders


def _sized(document: dict[str, Any]) -> tuple[str, int]:
    text = json.dumps(document, separators=(",", ":"), ensure_ascii=False)
    return text, len(text.encode("utf-8"))


def _chunk(folders: list[dict[str, Any]], max_bytes: int) -> Iterator[list[dict[str, Any]]]:
    """Split folders into groups whose serialized size stays under `max_bytes`.

    A single folder larger than the limit is yielded alone; splitting inside one
    resource would scatter related requests across files for no real benefit.
    """
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for entry in folders:
        size = len(json.dumps(entry, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        if current and current_bytes + size > max_bytes:
            yield current
            current, current_bytes = [], 0
        current.append(entry)
        current_bytes += size
    if current:
        yield current


def safe_filename(name: str) -> str:
    """A filename that survives every platform, preserving readability."""
    cleaned = "".join("-" if c in '/\\:*?"<>|' else c for c in name)
    return " ".join(cleaned.split()).strip(". ")


def build_collections(
    definition: SpecDef,
    index: SpecIndex,
    out_dir: Path,
    *,
    max_child_depth: int = DEFAULT_MAX_CHILD_DEPTH,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[BuildStats, list[tuple[Operation, str]]]:
    """Write one or more Postman collections for `definition`. Returns stats."""
    stats = BuildStats(key=definition.key)
    pool = SchemaPool()

    tree, skipped = group_operations(index, max_child_depth, stats)
    folders = _folders_for(tree, index, pool, stats, definition.default_base_path)
    stats.pool = pool.stats()

    title = index.title or definition.server_name
    description = COLLECTION_PREAMBLE.format(
        source=definition.spec_filename,
        blurb=definition.blurb,
        base_path=definition.default_base_path or "(none — paths are absolute)",
        max_child_depth=max_child_depth,
    )
    # `basePath` is spec-specific so it lives on the collection; the pod host and
    # credentials are shared and come from the environment file, which takes
    # precedence over these defaults.
    variables = [
        ("baseUrl", "https://{{host}}"),
        ("basePath", definition.default_base_path.strip("/")),
        ("host", "your-pod.fa.us2.oraclecloud.com"),
    ]

    ordered = [folders[category] for category in sorted(folders)]
    whole = pc.collection(
        name=title, description=description, items=ordered, variables=variables
    )
    text, size = _sized(whole)

    out_dir.mkdir(parents=True, exist_ok=True)

    if size <= max_bytes:
        destination = out_dir / f"{safe_filename(title)}.postman_collection.json"
        destination.write_text(text, encoding="utf-8")
        stats.files.append(destination)
        return stats, skipped

    # Too large for one file: one collection per category, chunked further if a
    # single category still exceeds the limit.
    for category in sorted(folders):
        groups = list(_chunk(folders[category]["item"], max_bytes))
        for number, group in enumerate(groups, start=1):
            suffix = f" (part {number})" if len(groups) > 1 else ""
            name = f"{title} — {category}{suffix}"
            document = pc.collection(
                name=name, description=description, items=group, variables=variables
            )
            body, _ = _sized(document)
            destination = out_dir / f"{safe_filename(name)}.postman_collection.json"
            destination.write_text(body, encoding="utf-8")
            stats.files.append(destination)

    return stats, skipped


def write_skipped_manifest(
    path: Path, rows: list[tuple[str, Operation, str]]
) -> None:
    """Record every omitted operation so nothing disappears silently."""
    lines = ["spec\tmethod\tpath\toperation_id\treason"]
    for spec_key, operation, reason in rows:
        lines.append(
            f"{spec_key}\t{operation.method}\t{operation.path}\t{operation.op_id}\t{reason}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
