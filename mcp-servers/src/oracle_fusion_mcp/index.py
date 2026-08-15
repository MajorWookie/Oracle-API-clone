"""Read-side access to a compiled spec index.

Opens the SQLite database produced by `build_index` and serves the catalog
queries behind the search/describe tools. Connections are read-only and the
database is immutable at runtime, so queries are cheap and startup is instant.
"""

from __future__ import annotations

import json
import re
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Bare FTS5 operators would otherwise be parsed as syntax when a user's search
#: text happens to contain them.
_FTS_SPECIALS = re.compile(r'["*():^\-]')


@dataclass(frozen=True)
class Operation:
    """A single API operation as stored in the index."""

    op_id: str
    method: str
    path: str
    kind: str
    category: str
    tag: str
    resource: str
    summary: str

    def brief(self) -> dict[str, Any]:
        """The compact form returned by search and browse tools."""
        return {
            "operation_id": self.op_id,
            "method": self.method,
            "path": self.path,
            "kind": self.kind,
            "summary": self.summary,
            "tag": self.tag,
        }


class IndexNotBuilt(RuntimeError):
    """Raised when the SQLite index is missing, with instructions to build it."""


def escape_fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Every term is quoted, which neutralizes FTS5 operator characters, and a
    trailing prefix wildcard is added to the final term so that partial words
    ("shipm") still match. Terms are ANDed.
    """
    terms = [t for t in _FTS_SPECIALS.sub(" ", text).split() if t]
    if not terms:
        return ""
    quoted = [f'"{t}"' for t in terms[:-1]]
    quoted.append(f'"{terms[-1]}"*')
    return " AND ".join(quoted)


class SpecIndex:
    """Query interface over one compiled spec index."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        if not db_path.exists():
            raise IndexNotBuilt(
                f"Index not found at {db_path}. Build it with:\n"
                f"    uv run oracle-fusion-build-index"
            )
        # Read-only URI so a bug can never mutate the compiled catalog.
        self._connection = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self.meta = {
            row["key"]: row["value"] for row in self._connection.execute("SELECT key, value FROM meta")
        }

    # -- catalog metadata -------------------------------------------------

    @property
    def title(self) -> str:
        return self.meta.get("spec_title", "Oracle Fusion REST API")

    @property
    def operation_count(self) -> int:
        return int(self.meta.get("operation_count", 0))

    def counts(self) -> dict[str, int]:
        return {
            "total": self.operation_count,
            "read": int(self.meta.get("read_count", 0)),
            "write": int(self.meta.get("write_count", 0)),
            "delete": int(self.meta.get("delete_count", 0)),
        }

    # -- queries ----------------------------------------------------------

    def _row_to_operation(self, row: sqlite3.Row) -> Operation:
        return Operation(
            op_id=row["op_id"],
            method=row["method"],
            path=row["path"],
            kind=row["kind"],
            category=row["category"] or "",
            tag=row["tag"] or "",
            resource=row["resource"] or "",
            summary=row["summary"] or "",
        )

    _COLUMNS = "op_id, method, path, kind, category, tag, resource, summary"
    _SELECT = f"SELECT {_COLUMNS} FROM operations"

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        kind: str | None = None,
        method: str | None = None,
        category: str | None = None,
    ) -> tuple[list[Operation], int]:
        """Full-text search the catalog.

        Returns matching operations ranked by FTS5 relevance, plus the total
        number of matches so callers can tell the model what was truncated.
        """
        match = escape_fts_query(query)
        if not match:
            return [], 0

        where = ["operations_fts MATCH ?"]
        params: list[Any] = [match]
        if kind:
            where.append("o.kind = ?")
            params.append(kind)
        if method:
            where.append("o.method = ?")
            params.append(method.upper())
        if category:
            where.append("o.category = ?")
            params.append(category)
        clause = " AND ".join(where)

        total = self._connection.execute(
            f"SELECT COUNT(*) FROM operations_fts "
            f"JOIN operations o ON o.op_id = operations_fts.op_id WHERE {clause}",
            params,
        ).fetchone()[0]

        rows = self._connection.execute(
            f"SELECT o.op_id, o.method, o.path, o.kind, o.category, o.tag, o.resource, o.summary "
            f"FROM operations_fts JOIN operations o ON o.op_id = operations_fts.op_id "
            f"WHERE {clause} ORDER BY bm25(operations_fts, 0, 4.0, 8.0, 2.0, 4.0, 4.0) "
            f"LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [self._row_to_operation(r) for r in rows], total

    def get(self, op_id: str) -> tuple[Operation, dict[str, Any]] | None:
        """Fetch one operation with its full detail body."""
        row = self._connection.execute(
            f"SELECT {self._COLUMNS}, detail FROM operations WHERE op_id = ?", (op_id,)
        ).fetchone()
        if row is None:
            return None
        detail = json.loads(zlib.decompress(row["detail"]))
        return self._row_to_operation(row), detail

    def find_similar(self, op_id: str, limit: int = 5) -> list[Operation]:
        """Operations whose ids resemble `op_id`, for 'did you mean' errors."""
        rows = self._connection.execute(
            f"{self._SELECT} WHERE op_id LIKE ? LIMIT ?", (f"%{op_id}%", limit)
        ).fetchall()
        return [self._row_to_operation(r) for r in rows]

    def list_categories(self) -> list[tuple[str, int]]:
        """Top-level tag categories with operation counts."""
        rows = self._connection.execute(
            "SELECT category, COUNT(*) AS n FROM operations "
            "WHERE category <> '' GROUP BY category ORDER BY category"
        ).fetchall()
        return [(r["category"], r["n"]) for r in rows]

    def list_resources(
        self, category: str | None = None, *, limit: int = 200, offset: int = 0
    ) -> tuple[list[tuple[str, str, int]], int]:
        """Resources (tags) available, optionally filtered to one category.

        Returns (tag, category, operation_count) triples plus the total.
        """
        where = "WHERE tag <> ''"
        params: list[Any] = []
        if category:
            where += " AND category = ?"
            params.append(category)

        total = self._connection.execute(
            f"SELECT COUNT(*) FROM (SELECT tag FROM operations {where} GROUP BY tag)", params
        ).fetchone()[0]
        rows = self._connection.execute(
            f"SELECT tag, category, COUNT(*) AS n FROM operations {where} "
            f"GROUP BY tag ORDER BY tag LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [(r["tag"], r["category"], r["n"]) for r in rows], total

    def operations_for_tag(
        self, tag: str, *, kind: str | None = None, limit: int = 100
    ) -> tuple[list[Operation], int]:
        """Every operation belonging to one resource tag."""
        where = "WHERE tag = ?"
        params: list[Any] = [tag]
        if kind:
            where += " AND kind = ?"
            params.append(kind)
        total = self._connection.execute(
            f"SELECT COUNT(*) FROM operations {where}", params
        ).fetchone()[0]
        rows = self._connection.execute(
            f"{self._SELECT} {where} ORDER BY path, method LIMIT ?", [*params, limit]
        ).fetchall()
        return [self._row_to_operation(r) for r in rows], total

    def component(self, section: str, name: str) -> Any | None:
        """Look up one entry from the spec's `components` section."""
        row = self._connection.execute(
            "SELECT body FROM components WHERE section = ? AND name = ?", (section, name)
        ).fetchone()
        if row is None:
            return None
        return json.loads(zlib.decompress(row["body"]))

    def close(self) -> None:
        self._connection.close()
