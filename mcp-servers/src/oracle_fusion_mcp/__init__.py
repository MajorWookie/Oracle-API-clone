"""MCP servers exposing the Oracle Fusion Cloud REST APIs.

One server per OpenAPI spec — SCM, Customer Experience, and Common Features.
Each uses the search + execute tool pattern: the full operation catalog lives in
a prebuilt SQLite index rather than in Claude's context window.
"""

__version__ = "0.1.0"
