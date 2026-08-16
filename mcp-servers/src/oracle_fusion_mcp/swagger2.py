"""Upconvert a Swagger 2.0 document to the OpenAPI 3 shape the rest of this package reads.

The Oracle CPQ spec is Swagger 2.0 while SCM, CX and Common Features are OpenAPI
3.0. Rather than teach every consumer two dialects, CPQ is converted once during
indexing so that `build_index`, `refs` and the Postman generator only ever see
OpenAPI 3.

The conversion covers the subset the Oracle specs actually use:

  * `definitions`            -> `components.schemas` (with every `$ref` rewritten)
  * `in: body` parameter     -> `requestBody`, content types from `consumes`
  * `in: formData` params    -> a `requestBody` object schema
  * bare `type` on a param   -> `schema: {type: ...}`
  * response `schema`        -> `content: {<produces>: {schema: ...}}`

Anything outside that subset (`securityDefinitions`, `host`, `basePath`) is
absent from the CPQ spec and is intentionally not handled; base paths are
recorded in `specs.py` instead.
"""

from __future__ import annotations

from typing import Any

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

#: JSON Schema facets Swagger 2.0 places directly on a parameter, which OpenAPI 3
#: moves inside `schema`.
_SCHEMA_FACETS = (
    "type",
    "format",
    "items",
    "enum",
    "default",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "multipleOf",
)

#: Parameter keys that stay at the top level of an OpenAPI 3 parameter object.
_PARAMETER_KEYS = ("name", "in", "description", "required", "deprecated", "example")

_DEFAULT_CONTENT_TYPE = "application/json"


def is_swagger2(spec: dict[str, Any]) -> bool:
    """True if this document declares Swagger 2.0 rather than OpenAPI 3."""
    return str(spec.get("swagger", "")).startswith("2") and "openapi" not in spec


def rewrite_refs(node: Any) -> Any:
    """Rewrite every `#/definitions/X` pointer to `#/components/schemas/X`."""
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/definitions/"):
                result[key] = value.replace("#/definitions/", "#/components/schemas/", 1)
            else:
                result[key] = rewrite_refs(value)
        return result
    if isinstance(node, list):
        return [rewrite_refs(item) for item in node]
    return node


def _schema_from_facets(source: dict[str, Any]) -> dict[str, Any]:
    """Pull the JSON Schema facets off a 2.0 parameter into a `schema` object."""
    schema = {key: source[key] for key in _SCHEMA_FACETS if key in source}
    # Swagger 2.0 spells file uploads as `type: file`; OpenAPI 3 uses a binary string.
    if schema.get("type") == "file":
        schema["type"] = "string"
        schema["format"] = "binary"
    return schema or {"type": "string"}


def convert_parameter(parameter: dict[str, Any]) -> dict[str, Any]:
    """Convert one non-body 2.0 parameter to OpenAPI 3 form."""
    if "$ref" in parameter or "schema" in parameter:
        return dict(parameter)
    converted = {key: parameter[key] for key in _PARAMETER_KEYS if key in parameter}
    converted["schema"] = _schema_from_facets(parameter)
    return converted


def _content(schema: dict[str, Any], media_types: list[str]) -> dict[str, Any]:
    return {media_type: {"schema": schema} for media_type in media_types}


def _request_body_from_body_param(
    parameter: dict[str, Any], consumes: list[str]
) -> dict[str, Any]:
    body: dict[str, Any] = {"content": _content(parameter.get("schema") or {}, consumes)}
    if parameter.get("required"):
        body["required"] = True
    if parameter.get("description"):
        body["description"] = parameter["description"]
    return body


def _request_body_from_form_params(
    parameters: list[dict[str, Any]], consumes: list[str]
) -> dict[str, Any]:
    """Fold `in: formData` parameters into a single object schema."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in parameters:
        name = parameter.get("name")
        if not name:
            continue
        schema = _schema_from_facets(parameter)
        if parameter.get("description"):
            schema["description"] = parameter["description"]
        properties[name] = schema
        if parameter.get("required"):
            required.append(name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required

    # A file upload has to travel as multipart regardless of what `consumes` says.
    if any(parameter.get("type") == "file" for parameter in parameters):
        media_types = ["multipart/form-data"]
    else:
        media_types = [
            media_type
            for media_type in consumes
            if media_type not in (_DEFAULT_CONTENT_TYPE,)
        ] or ["application/x-www-form-urlencoded"]

    body: dict[str, Any] = {"content": _content(schema, media_types)}
    if required:
        body["required"] = True
    return body


def convert_responses(
    responses: dict[str, Any], produces: list[str]
) -> dict[str, Any]:
    """Move each response's `schema` under `content`, and convert header types."""
    converted: dict[str, Any] = {}
    for status, response in responses.items():
        if not isinstance(response, dict):
            converted[status] = response
            continue
        entry = {k: v for k, v in response.items() if k not in ("schema", "examples", "headers")}
        if isinstance(response.get("schema"), dict):
            entry["content"] = _content(response["schema"], produces)
        if isinstance(response.get("examples"), dict):
            # 2.0 keys examples by media type; keep them on the matching content entry.
            for media_type, example in response["examples"].items():
                slot = entry.setdefault("content", {}).setdefault(media_type, {})
                slot["example"] = example
        headers = response.get("headers")
        if isinstance(headers, dict):
            entry["headers"] = {
                name: (
                    {
                        **{
                            k: v
                            for k, v in header.items()
                            if k not in _SCHEMA_FACETS
                        },
                        "schema": _schema_from_facets(header),
                    }
                    if isinstance(header, dict)
                    else header
                )
                for name, header in headers.items()
            }
        converted[status] = entry
    return converted


def convert_operation(
    operation: dict[str, Any], default_consumes: list[str], default_produces: list[str]
) -> dict[str, Any]:
    """Convert one 2.0 operation object to OpenAPI 3 form."""
    consumes = operation.get("consumes") or default_consumes or [_DEFAULT_CONTENT_TYPE]
    produces = operation.get("produces") or default_produces or [_DEFAULT_CONTENT_TYPE]

    converted = {
        key: value
        for key, value in operation.items()
        if key not in ("parameters", "responses", "consumes", "produces")
    }

    parameters = [p for p in (operation.get("parameters") or []) if isinstance(p, dict)]
    body_params = [p for p in parameters if p.get("in") == "body"]
    form_params = [p for p in parameters if p.get("in") == "formData"]
    others = [p for p in parameters if p.get("in") not in ("body", "formData")]

    if others:
        converted["parameters"] = [convert_parameter(p) for p in others]
    if body_params:
        # 2.0 allows at most one body parameter.
        converted["requestBody"] = _request_body_from_body_param(body_params[0], consumes)
    elif form_params:
        converted["requestBody"] = _request_body_from_form_params(form_params, consumes)

    if isinstance(operation.get("responses"), dict):
        converted["responses"] = convert_responses(operation["responses"], produces)

    return converted


def upconvert(spec: dict[str, Any]) -> dict[str, Any]:
    """Return `spec` as an OpenAPI 3 document. Already-3.0 documents pass through."""
    if not is_swagger2(spec):
        return spec

    spec = rewrite_refs(spec)
    default_consumes = [c for c in (spec.get("consumes") or []) if isinstance(c, str)]
    default_produces = [p for p in (spec.get("produces") or []) if isinstance(p, str)]

    paths: dict[str, Any] = {}
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        converted_item = {
            key: value
            for key, value in path_item.items()
            if key not in HTTP_METHODS and key != "parameters"
        }
        if isinstance(path_item.get("parameters"), list):
            converted_item["parameters"] = [
                convert_parameter(p) for p in path_item["parameters"] if isinstance(p, dict)
            ]
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if isinstance(operation, dict):
                converted_item[method] = convert_operation(
                    operation, default_consumes, default_produces
                )
        paths[path] = converted_item

    components: dict[str, Any] = dict(spec.get("components") or {})
    if spec.get("definitions"):
        components["schemas"] = {**(components.get("schemas") or {}), **spec["definitions"]}
    if spec.get("parameters"):
        components["parameters"] = {
            name: convert_parameter(p) if isinstance(p, dict) else p
            for name, p in spec["parameters"].items()
        }
    if spec.get("responses"):
        components["responses"] = convert_responses(spec["responses"], default_produces)

    converted: dict[str, Any] = {
        key: value
        for key, value in spec.items()
        if key
        not in (
            "swagger",
            "paths",
            "definitions",
            "parameters",
            "responses",
            "consumes",
            "produces",
            "host",
            "basePath",
            "schemes",
        )
    }
    converted["openapi"] = "3.0.0"
    converted["paths"] = paths
    if components:
        converted["components"] = components
    return converted
