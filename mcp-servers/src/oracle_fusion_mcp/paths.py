"""Normalization of the raw path keys found in the Oracle specs.

The SCM and CX specs use clean relative paths (`/assetSystemOptions`), but the
Common Features spec is inconsistent — of its 386 path keys, 120 start with
`https:`, 59 with `<servername>`, 54 with `http:` and 20 with a bare `servername`
token. Some already carry an API root (`/hcmRestApi/scim/Users`), others do not
(`/announcements`).

`normalize_path` reduces every variant to a single absolute, host-free path.
"""

from __future__ import annotations

import re

from .specs import KNOWN_API_ROOTS

_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
# A leading segment is a hostname if it is a `<servername>`-style placeholder or
# looks like a domain (contains a dot, optionally a port). Resource names never do.
_HOSTLIKE = re.compile(r"^(<[^>]*>|[^/]*servername[^/]*|[^/]+\.[^/]+|[^/]+:\d+)$", re.IGNORECASE)


def strip_host(raw: str) -> str:
    """Remove any scheme and hostname, returning a path with a single leading slash."""
    path = raw.strip()
    path = _SCHEME.sub("", path)

    # After scheme removal the first segment may still be a host.
    if not path.startswith("/"):
        head, slash, tail = path.partition("/")
        if slash and _HOSTLIKE.match(head):
            path = tail
        elif slash and head in KNOWN_API_ROOTS:
            # e.g. "ess/rest/scheduler/v1/..." — a root that lost its leading slash.
            path = f"{head}/{tail}"

    if not path.startswith("/"):
        path = "/" + path
    # Collapse duplicate slashes introduced by the placeholder forms.
    return re.sub(r"/{2,}", "/", path).rstrip("/") or "/"


# Oracle names most API roots `<something>Api` (fscmRestApi, fndSetupApi,
# applcoreApi) or `<something>UI` (fscmUI). Matching the pattern rather than an
# enumerated list keeps unfamiliar roots from being double-prefixed.
_API_ROOT_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]*(Api|UI)$")


def has_api_root(path: str) -> bool:
    """True if the path already begins with a recognized Oracle API root."""
    segments = path.strip("/").split("/")
    if not segments or not segments[0]:
        return False
    head = segments[0]
    return head in KNOWN_API_ROOTS or bool(_API_ROOT_PATTERN.match(head))


def normalize_path(raw: str, default_base_path: str, *, enabled: bool = True) -> str:
    """Return the absolute, host-free request path for a spec path key.

    `default_base_path` is prepended when the path carries no API root of its own.
    When `enabled` is False the path is only cleaned up, never re-rooted with the
    host stripper — used for the SCM and CX specs, whose paths are already clean
    relative resource paths.
    """
    if enabled:
        path = strip_host(raw)
    else:
        path = raw.strip()
        if not path.startswith("/"):
            path = "/" + path
        path = re.sub(r"/{2,}", "/", path).rstrip("/") or "/"

    if has_api_root(path):
        return path
    return f"{default_base_path.rstrip('/')}{path}"


def fill_path_params(path: str, values: dict[str, object]) -> tuple[str, set[str]]:
    """Substitute `{name}` placeholders in `path`.

    Returns the filled path and the set of placeholder names that had no value.
    Values are percent-encoded so that keys containing `/` or spaces stay in the
    correct path segment.
    """
    from urllib.parse import quote

    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values or values[name] is None:
            missing.add(name)
            return match.group(0)
        return quote(str(values[name]), safe="")

    return re.sub(r"\{([^}]+)\}", replace, path), missing


def path_placeholders(path: str) -> list[str]:
    """Names of the `{...}` placeholders in a path, in order."""
    return re.findall(r"\{([^}]+)\}", path)
