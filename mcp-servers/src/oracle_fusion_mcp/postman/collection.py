"""Postman collection format v2.1.0 primitives.

Only the parts of the format these collections need: collection, folder, request
item, url, auth and variables. Ids are derived with `uuid5` from stable names
rather than generated randomly, so regenerating an unchanged spec produces no
diff — the same property the repo's `pack_specs.py` maintains for the specs.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Iterable

SCHEMA_URL = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"

#: Namespace for deterministic collection and environment ids.
_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "oracle-fusion-postman")

#: Oracle's REST framework query parameters. They appear on thousands of
#: operations with several hundred characters of identical description text
#: apiece; the collection description documents them once instead.
FRAMEWORK_QUERY_PARAMS = frozenset(
    {
        "q",
        "fields",
        "limit",
        "offset",
        "expand",
        "onlyData",
        "links",
        "totalResults",
        "orderBy",
        "finder",
    }
)

FRAMEWORK_PARAM_NOTE = "Oracle REST framework parameter — see the collection description."

_WHITESPACE = re.compile(r"\s+")
_WHOLE_PLACEHOLDER = re.compile(r"^\{([^{}]+)\}$")
_ANY_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def stable_id(*parts: str) -> str:
    """A deterministic UUID for a collection or environment."""
    return str(uuid.uuid5(_ID_NAMESPACE, "|".join(parts)))


def trim(text: str | None, limit: int = 200) -> str:
    """Collapse a spec description to one line, truncated at `limit` characters."""
    if not text:
        return ""
    flat = _WHITESPACE.sub(" ", str(text)).strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def to_url(
    path: str, base_variable: str = "{{baseUrl}}", base_path: str = ""
) -> dict[str, Any]:
    """Convert a spec path into a Postman url object.

    A whole-segment `{OrderId}` becomes Postman's `:OrderId` path variable. A
    placeholder embedded in a larger segment (`adminCustom{tableName}`, and 42
    others in CPQ) cannot be expressed that way, so it becomes a `{{tableName}}`
    variable reference, which Postman substitutes anywhere in the URL.

    Indexed paths already carry the spec's base path. When `base_path` matches the
    start of the path, those segments collapse to a single `{{basePath}}`
    reference so that following an Oracle resource-version bump is a one-variable
    edit — the same substitution `client.build_url` performs at runtime. Paths
    rooted elsewhere (Common Features' `/ess` and `/bpm` endpoints, CPQ's four
    `/cpq/rest/v19` outliers) are left absolute, since the override does not
    apply to them.
    """
    segments: list[str] = []
    variables: list[dict[str, str]] = []
    inline: list[str] = []

    remainder = path.strip("/").split("/")
    base_segments = [s for s in base_path.strip("/").split("/") if s]
    if base_segments and remainder[: len(base_segments)] == base_segments:
        segments.append("{{basePath}}")
        remainder = remainder[len(base_segments) :]

    for segment in remainder:
        whole = _WHOLE_PLACEHOLDER.match(segment)
        if whole:
            name = whole.group(1)
            segments.append(f":{name}")
            variables.append({"key": name, "value": ""})
            continue
        if "{" in segment:
            names = _ANY_PLACEHOLDER.findall(segment)
            inline.extend(names)
            segments.append(_ANY_PLACEHOLDER.sub(lambda m: f"{{{{{m.group(1)}}}}}", segment))
            continue
        segments.append(segment)

    url: dict[str, Any] = {
        "raw": f"{base_variable}/{'/'.join(segments)}",
        "host": [base_variable],
        "path": segments,
    }
    if variables:
        url["variable"] = variables
    if inline:
        url["_inlineVariables"] = sorted(set(inline))
    return url


def _parameter_rows(
    parameters: Iterable[dict[str, Any]], location: str
) -> list[dict[str, Any]]:
    """Build query or header rows from an operation's merged parameter list."""
    rows: list[dict[str, Any]] = []
    for parameter in parameters:
        if not isinstance(parameter, dict) or parameter.get("in") != location:
            continue
        name = parameter.get("name")
        if not name:
            continue
        required = bool(parameter.get("required"))
        schema = parameter.get("schema") or {}
        default = schema.get("default")
        if name in FRAMEWORK_QUERY_PARAMS and location == "query":
            description = FRAMEWORK_PARAM_NOTE
        else:
            description = trim(parameter.get("description"), 160)

        row: dict[str, Any] = {
            "key": str(name),
            "value": "" if default is None else str(default),
        }
        if description:
            row["description"] = description
        # Everything optional ships disabled so a request runs as-is.
        if not required:
            row["disabled"] = True
        rows.append(row)
    return rows


def request_item(
    *,
    name: str,
    method: str,
    path: str,
    operation_id: str,
    parameters: Iterable[dict[str, Any]] = (),
    body: Any = None,
    content_type: str | None = None,
    description: str = "",
    base_path: str = "",
) -> dict[str, Any]:
    """One Postman request item."""
    parameters = list(parameters)
    url = to_url(path, base_path=base_path)
    inline = url.pop("_inlineVariables", None)

    query = _parameter_rows(parameters, "query")
    if query:
        url["query"] = query
        enabled = [row["key"] for row in query if not row.get("disabled")]
        if enabled:
            url["raw"] = f"{url['raw']}?" + "&".join(f"{key}=" for key in enabled)

    headers = _parameter_rows(parameters, "header")
    if content_type:
        headers.insert(0, {"key": "Content-Type", "value": content_type})
    headers.insert(0, {"key": "Accept", "value": "application/json"})

    notes = [f"`{operation_id}` — {method} {path}"]
    if description:
        notes.append(description)
    if inline:
        notes.append(
            "Set these collection variables before sending: "
            + ", ".join(f"`{n}`" for n in inline)
        )

    request: dict[str, Any] = {
        "method": method,
        "header": headers,
        "url": url,
        "description": "\n\n".join(notes),
    }
    if body is not None:
        request["body"] = {
            "mode": "raw",
            "raw": json.dumps(body, indent=2),
            "options": {"raw": {"language": "json"}},
        }

    return {"name": name, "request": request}


def folder(name: str, items: list[dict[str, Any]], description: str = "") -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "item": items}
    if description:
        entry["description"] = description
    return entry


def basic_auth() -> dict[str, Any]:
    """Collection-level HTTP Basic auth, matching `config.py`'s credential model."""
    return {
        "type": "basic",
        "basic": [
            {"key": "username", "value": "{{username}}", "type": "string"},
            {"key": "password", "value": "{{password}}", "type": "string"},
        ],
    }


def collection(
    *,
    name: str,
    description: str,
    items: list[dict[str, Any]],
    variables: list[dict[str, str]],
) -> dict[str, Any]:
    """Assemble a complete v2.1.0 collection document."""
    return {
        "info": {
            "_postman_id": stable_id("collection", name),
            "name": name,
            "description": description,
            "schema": SCHEMA_URL,
        },
        "auth": basic_auth(),
        "variable": [{"key": key, "value": value} for key, value in variables],
        "item": items,
    }


def environment(name: str, values: list[tuple[str, str]]) -> dict[str, Any]:
    """A Postman environment file holding the pod-specific settings."""
    return {
        "id": stable_id("environment", name),
        "name": name,
        "values": [
            {"key": key, "value": value, "type": "secret" if key == "password" else "default",
             "enabled": True}
            for key, value in values
        ],
        "_postman_variable_scope": "environment",
    }
