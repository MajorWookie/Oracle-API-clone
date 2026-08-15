"""Turn an OpenAPI schema into an example request body.

Postman has no `$ref`, so every body has to be materialized. Two properties of
the Oracle schemas make the naive version unusable:

  * They are self-referential. 11 SCM and 91 CX schemas sit on a `$ref` cycle
    (`PurchaseOrder` -> `OrderLine` -> `PurchaseOrder`), so an unguarded walk
    never terminates.
  * They are enormous. A single resource schema carries hundreds of columns and
    transitively pulls in thousands of other schemas.

So expansion is bounded on three axes — reference depth, node count, and
properties per object — and a reference already open on the current branch is
replaced by a `<recursive: Name>` marker rather than followed.

A body carries the schema's `required` properties when it declares any, on the
grounds that those are exactly what a caller must supply. When a schema declares
none — common in the Oracle specs — a capped sample of writable properties is
emitted instead, so the body is still a useful starting point rather than `{}`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..refs import parse_ref
from .dedupe import SchemaPool, fingerprint

#: How many `$ref` hops to follow before emitting a truncation marker.
DEFAULT_MAX_DEPTH = 3

#: Ceiling on nodes emitted for one body, so no single example can run away.
DEFAULT_MAX_NODES = 400

#: Properties emitted for an object that declares no `required` list.
DEFAULT_MAX_PROPERTIES = 12

#: Properties Oracle attaches to every resource that are never part of a payload.
IGNORED_PROPERTIES = frozenset({"links", "@context"})

_FORMAT_VALUES: dict[str, Any] = {
    "date-time": "2026-01-01T00:00:00+00:00",
    "date": "2026-01-01",
    "time": "00:00:00",
    "duration": "P1D",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "email": "user@example.com",
    "uri": "https://example.com",
    "url": "https://example.com",
    "hostname": "example.com",
    "ipv4": "192.0.2.1",
    "binary": "",
    "byte": "",
    "password": "",
    "int32": 0,
    "int64": 0,
    "float": 0,
    "double": 0,
}

_TYPE_VALUES: dict[str, Any] = {
    "string": "",
    "integer": 0,
    "number": 0,
    "boolean": False,
    "null": None,
}


class ComponentSource:
    """Structural type for the `component(section, name)` lookup `SpecIndex` provides."""

    def component(self, section: str, name: str) -> Any | None:  # pragma: no cover
        raise NotImplementedError


@dataclass
class _Context:
    """Mutable state shared across one body's expansion."""

    source: Any
    pool: SchemaPool
    max_depth: int = DEFAULT_MAX_DEPTH
    max_nodes: int = DEFAULT_MAX_NODES
    max_properties: int = DEFAULT_MAX_PROPERTIES
    nodes: int = 0
    #: Incremented whenever a cycle or limit marker is emitted. Used to decide
    #: whether a subtree's result is safe to memoize.
    markers: int = 0
    truncated: bool = field(default=False)

    def take(self) -> bool:
        if self.nodes >= self.max_nodes:
            self.truncated = True
            return False
        self.nodes += 1
        return True


def _scalar(schema: dict[str, Any]) -> Any:
    """A representative value for a non-object, non-array schema."""
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]

    fmt = schema.get("format")
    if isinstance(fmt, str) and fmt in _FORMAT_VALUES:
        return _FORMAT_VALUES[fmt]

    declared = schema.get("type")
    if isinstance(declared, list):
        declared = next((t for t in declared if t != "null"), None)
    if isinstance(declared, str) and declared in _TYPE_VALUES:
        return _TYPE_VALUES[declared]
    return ""


def _is_writable(definition: Any) -> bool:
    return not (isinstance(definition, dict) and definition.get("readOnly"))


def _merge_all_of(
    schema: dict[str, Any], context: _Context, depth: int, seen: frozenset[tuple[str, str]]
) -> dict[str, Any]:
    """Flatten `allOf` into a single object schema."""
    merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    for part in schema.get("allOf") or []:
        resolved = _deref(part, context, depth, seen)
        if not isinstance(resolved, dict):
            continue
        merged["properties"].update(resolved.get("properties") or {})
        required = resolved.get("required")
        if isinstance(required, list):
            merged["required"].extend(r for r in required if isinstance(r, str))
    for key, value in schema.items():
        if key in ("allOf", "properties", "required"):
            continue
        merged.setdefault(key, value)
    merged["properties"].update(schema.get("properties") or {})
    own_required = schema.get("required")
    if isinstance(own_required, list):
        merged["required"].extend(r for r in own_required if isinstance(r, str))
    if not merged["required"]:
        merged.pop("required")
    return merged


def _deref(
    node: Any, context: _Context, depth: int, seen: frozenset[tuple[str, str]]
) -> Any:
    """Resolve a `$ref` one hop, without generating an example."""
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node
    parsed = parse_ref(ref)
    if parsed is None:
        return {}
    section, name = parsed
    if (section, name) in seen or depth >= context.max_depth:
        return {}
    target = context.source.component(section, name)
    return target if isinstance(target, dict) else {}


def _build(
    schema: Any, context: _Context, depth: int, seen: frozenset[tuple[str, str]]
) -> Any:
    if not isinstance(schema, dict):
        return None
    if not context.take():
        return "<truncated: node budget>"

    ref = schema.get("$ref")
    if isinstance(ref, str):
        parsed = parse_ref(ref)
        if parsed is None:
            return "<unresolved external reference>"
        section, name = parsed

        if (section, name) in seen:
            context.markers += 1
            return f"<recursive: {name}>"
        if depth >= context.max_depth:
            context.markers += 1
            return f"<truncated: {name}>"

        target = context.source.component(section, name)
        if not isinstance(target, dict):
            context.markers += 1
            return f"<unknown schema: {name}>"

        context.pool.register(name, target)
        key = fingerprint(target)
        remaining = context.max_depth - depth
        cached = context.pool.get(key, remaining)
        if cached is not None:
            return cached

        markers_before = context.markers
        value = _build(target, context, depth + 1, seen | {(section, name)})
        # Only context-independent results are safe to reuse: anything that hit a
        # cycle depended on which refs were already open on this branch.
        if context.markers == markers_before:
            context.pool.put(key, remaining, value)
        return value

    if schema.get("allOf"):
        return _build(_merge_all_of(schema, context, depth, seen), context, depth, seen)

    for combinator in ("oneOf", "anyOf"):
        options = schema.get(combinator)
        if isinstance(options, list) and options:
            return _build(options[0], context, depth, seen)

    declared = schema.get("type")
    if declared == "array" or ("items" in schema and declared is None):
        item = _build(schema.get("items") or {}, context, depth, seen)
        return [item] if item is not None else []

    properties = schema.get("properties")
    if isinstance(properties, dict) or declared == "object":
        return _object(schema, properties or {}, context, depth, seen)

    return _scalar(schema)


def _object(
    schema: dict[str, Any],
    properties: dict[str, Any],
    context: _Context,
    depth: int,
    seen: frozenset[tuple[str, str]],
) -> Any:
    required = [r for r in (schema.get("required") or []) if isinstance(r, str)]
    candidates: list[tuple[str, Any]]

    if required:
        # Required properties are exactly what the caller must supply. A name in
        # `required` with no matching property definition still gets an entry, so
        # the body does not silently omit a mandatory field.
        candidates = [(name, properties.get(name, {})) for name in required]
    else:
        writable = [
            (name, definition)
            for name, definition in properties.items()
            if name not in IGNORED_PROPERTIES and _is_writable(definition)
        ]
        candidates = writable[: context.max_properties]
        if len(writable) > context.max_properties:
            context.truncated = True

    body: dict[str, Any] = {}
    for name, definition in candidates:
        if name in IGNORED_PROPERTIES:
            continue
        value = _build(definition, context, depth, seen)
        body[name] = value

    if not body and isinstance(schema.get("additionalProperties"), dict):
        body["propertyName"] = _build(schema["additionalProperties"], context, depth, seen)
    return body


def build_example(
    schema: Any,
    source: Any,
    *,
    pool: SchemaPool | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_properties: int = DEFAULT_MAX_PROPERTIES,
) -> tuple[Any, bool]:
    """Build an example value for `schema`.

    Returns the example and whether anything was truncated — a caller can use the
    flag to note in the request description that the body is a starting point
    rather than the full payload. Always terminates.
    """
    context = _Context(
        source=source,
        pool=pool if pool is not None else SchemaPool(),
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_properties=max_properties,
    )
    value = _build(schema, context, 0, frozenset())
    return value, context.truncated or bool(context.markers)


def request_body_example(
    operation: dict[str, Any], source: Any, *, pool: SchemaPool | None = None, **kwargs: Any
) -> tuple[str | None, Any, bool]:
    """Pick a content type for an operation's request body and build its example.

    Returns `(content_type, example, truncated)`, or `(None, None, False)` when the
    operation takes no body. `application/json` wins when the operation offers it;
    otherwise the spec's first declared type is used, which is how Oracle's
    action endpoints keep their required
    `application/vnd.oracle.adf.action+json`.
    """
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return None, None, False

    if "$ref" in body:
        parsed = parse_ref(str(body["$ref"]))
        if parsed:
            resolved = source.component(*parsed)
            if isinstance(resolved, dict):
                body = resolved

    content = body.get("content")
    if not isinstance(content, dict) or not content:
        return None, None, False

    content_type = (
        "application/json" if "application/json" in content else next(iter(content))
    )
    schema = (content.get(content_type) or {}).get("schema")
    if not isinstance(schema, dict):
        return content_type, {}, False

    example, truncated = build_example(schema, source, pool=pool, **kwargs)
    return content_type, example, truncated
