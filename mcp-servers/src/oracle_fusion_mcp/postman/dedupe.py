"""Content-addressed pooling of schemas.

30% of the SCM spec's 22,405 schemas are byte-for-byte identical to another
schema under a different name (26% of CX's 14,173). Postman collections have no
`$ref`, so that duplication cannot be represented in the output — but it can be
removed from the *work*: identical schemas are fingerprinted to the same key, so
their example body is generated once and reused, and identical schemas are
guaranteed to produce identical bodies.

The pool also carries the statistics reported at the end of a build.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def fingerprint(schema: Any) -> str:
    """A stable content hash for a schema, insensitive to key order."""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


class SchemaPool:
    """Fingerprint-keyed cache of generated examples.

    Cache keys include the remaining depth budget, because the same schema
    expanded with more depth available yields a deeper example. Subtrees whose
    expansion depended on the enclosing `$ref` chain — anything that emitted a
    recursion marker — are deliberately not cached, since their result is only
    correct for the branch that produced it.
    """

    def __init__(self) -> None:
        self._examples: dict[tuple[str, int], Any] = {}
        self._seen_fingerprints: dict[str, str] = {}
        self.registered = 0
        self.hits = 0

    # -- statistics -------------------------------------------------------

    def register(self, name: str, schema: Any) -> str:
        """Record a schema under its fingerprint; return the first name seen for it."""
        self.registered += 1
        key = fingerprint(schema)
        return self._seen_fingerprints.setdefault(key, name)

    @property
    def distinct(self) -> int:
        """Number of distinct schema bodies registered."""
        return len(self._seen_fingerprints)

    @property
    def duplicates(self) -> int:
        """How many registered schemas were copies of one already seen."""
        return self.registered - self.distinct

    # -- example memoization ---------------------------------------------

    def get(self, key: str, depth_remaining: int) -> Any | None:
        cached = self._examples.get((key, depth_remaining))
        if cached is not None:
            self.hits += 1
        return cached

    def put(self, key: str, depth_remaining: int, value: Any) -> None:
        self._examples[(key, depth_remaining)] = value

    def stats(self) -> dict[str, int]:
        return {
            "schemas_registered": self.registered,
            "distinct_schemas": self.distinct,
            "duplicate_schemas": self.duplicates,
            "example_cache_hits": self.hits,
            "example_cache_size": len(self._examples),
        }
