"""Bounded `$ref` resolution against a compiled spec index.

Oracle's schemas are deeply nested and self-referential — a single SCM resource
schema can transitively pull in thousands of others, and parent/child links make
true cycles common. Expanding a `$ref` graph naively either never terminates or
returns megabytes that would swamp the context window.

`resolve` therefore expands refs breadth-first with a hard depth cap and cycle
detection. Anything it declines to expand is replaced by a marker naming the
schema, so the model can request it explicitly if it actually needs the detail.
"""

from __future__ import annotations

from typing import Any, Protocol

#: How deep to expand nested `$ref`s inside a single operation detail.
DEFAULT_MAX_DEPTH = 3

#: Guard against a single expansion producing an unbounded number of nodes.
DEFAULT_MAX_NODES = 4000


class ComponentSource(Protocol):
    """The slice of `SpecIndex` that `resolve` depends on."""

    def component(self, section: str, name: str) -> Any | None: ...


def parse_ref(ref: str) -> tuple[str, str] | None:
    """Split `#/components/schemas/Foo` into `("schemas", "Foo")`.

    Returns None for external or otherwise unsupported refs.
    """
    if not ref.startswith("#/components/"):
        return None
    parts = ref.split("/")
    if len(parts) < 4:
        return None
    # Names can legitimately contain slashes once URL-decoded; rejoin the tail.
    section = parts[2]
    name = "/".join(parts[3:]).replace("~1", "/").replace("~0", "~")
    return section, name


def resolve(
    node: Any,
    source: ComponentSource,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> Any:
    """Return `node` with `$ref`s expanded, bounded by depth and node count.

    Unexpanded refs become `{"$ref_unexpanded": "<name>", "note": "..."}` so the
    result stays valid JSON and self-describing.
    """
    counter = _Counter(max_nodes)
    return _walk(node, source, 0, max_depth, counter, frozenset())


#: Cycle detection keys on (section, name), not name alone. Oracle reuses one
#: name across sections — `workOrders-item-post-request` exists in both
#: `components/requestBodies` and `components/schemas`, and the requestBodies
#: entry refers to the schemas entry. Keying on the name alone made that ordinary
#: cross-section reference look circular and hid the body schema of nearly every
#: POST operation in the SCM and CX specs.


class _Counter:
    """Mutable node budget shared across one resolution."""

    __slots__ = ("remaining",)

    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _truncation_marker(name: str, reason: str) -> dict[str, str]:
    return {
        "$ref_unexpanded": name,
        "note": f"Not expanded ({reason}). Use describe_schema with this name for the full definition.",
    }


def _walk(
    node: Any,
    source: ComponentSource,
    depth: int,
    max_depth: int,
    counter: _Counter,
    seen: frozenset[tuple[str, str]],
) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            parsed = parse_ref(ref)
            if parsed is None:
                return dict(node)  # external ref — pass through untouched
            section, name = parsed

            if (section, name) in seen:
                return _truncation_marker(name, "circular reference")
            if depth >= max_depth:
                return _truncation_marker(name, "depth limit")
            if not counter.take():
                return _truncation_marker(name, "size limit")

            target = source.component(section, name)
            if target is None:
                return _truncation_marker(name, "not found in spec components")

            expanded = _walk(
                target, source, depth + 1, max_depth, counter, seen | {(section, name)}
            )
            # Preserve sibling keys alongside the ref (description, nullable, ...).
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            if siblings and isinstance(expanded, dict):
                return {**expanded, **_walk(siblings, source, depth, max_depth, counter, seen)}
            return expanded

        result: dict[str, Any] = {}
        for key, value in node.items():
            if not counter.take():
                result["$truncated"] = "size limit reached"
                break
            result[key] = _walk(value, source, depth, max_depth, counter, seen)
        return result

    if isinstance(node, list):
        items: list[Any] = []
        for value in node:
            if not counter.take():
                items.append({"$truncated": "size limit reached"})
                break
            items.append(_walk(value, source, depth, max_depth, counter, seen))
        return items

    return node


def summarize_schema(schema: Any, *, max_properties: int = 60) -> Any:
    """Condense a resolved schema into something compact enough to read.

    Object schemas are reduced to a property name -> type/description mapping.
    Oracle resource schemas routinely carry hundreds of columns, so the property
    list is capped and the omission is reported inline.
    """
    if not isinstance(schema, dict):
        return schema

    schema_type = schema.get("type")
    if schema_type == "array" or "items" in schema:
        return {"type": "array", "items": summarize_schema(schema.get("items", {}))}

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        # Not an object schema — return the useful scalar facets only.
        keep = ("type", "format", "enum", "description", "example", "nullable", "$ref_unexpanded")
        return {k: schema[k] for k in keep if k in schema} or schema

    summary: dict[str, Any] = {"type": "object"}
    if schema.get("description"):
        summary["description"] = schema["description"]
    if schema.get("required"):
        summary["required"] = schema["required"]

    # Oracle resources carry hundreds of columns, most of them optional. Required
    # fields are what a caller must supply, so they are never the ones dropped by
    # the property cap.
    required = set(schema.get("required") or ())
    ordered = sorted(properties.items(), key=lambda item: item[0] not in required)

    fields: dict[str, Any] = {}
    for name, definition in ordered[:max_properties]:
        if not isinstance(definition, dict):
            fields[name] = definition
            continue
        entry: dict[str, Any] = {}
        if "type" in definition:
            entry["type"] = definition["type"]
        if "format" in definition:
            entry["format"] = definition["format"]
        if "enum" in definition:
            entry["enum"] = definition["enum"]
        if definition.get("readOnly"):
            entry["readOnly"] = True
        if "$ref_unexpanded" in definition:
            entry["$ref_unexpanded"] = definition["$ref_unexpanded"]
        if name in required:
            entry["required"] = True
        description = definition.get("description")
        if description:
            entry["description"] = description[:180]
        fields[name] = entry or {"type": "object"}

    summary["properties"] = fields
    omitted = len(properties) - len(fields)
    if omitted > 0:
        summary["properties_omitted"] = (
            f"{omitted} further properties omitted. Use describe_schema for the full list."
        )
    return summary
