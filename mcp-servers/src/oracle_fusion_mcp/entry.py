"""Console-script entry points — one MCP server per spec.

Each runs over stdio, which is what Claude Desktop and Claude Code launch.
Configuration errors are reported on stderr and exit non-zero, so a
misconfigured server surfaces in the host's MCP log instead of hanging.
"""

from __future__ import annotations

import sys

from .config import ConfigError
from .server import create_server
from .specs import COMMON, CX, SCM, SpecDef


def _run(definition: SpecDef) -> int:
    try:
        server = create_server(definition)
    except ConfigError as error:
        print(f"{definition.server_name}: configuration error: {error}", file=sys.stderr)
        return 2
    server.run()  # stdio transport by default
    return 0


def run_scm() -> int:
    """Oracle Fusion Cloud SCM MCP server."""
    return _run(SCM)


def run_cx() -> int:
    """Oracle Fusion Cloud Customer Experience MCP server."""
    return _run(CX)


def run_common() -> int:
    """Oracle Fusion Cloud Common Features MCP server."""
    return _run(COMMON)


if __name__ == "__main__":
    raise SystemExit(run_scm())
