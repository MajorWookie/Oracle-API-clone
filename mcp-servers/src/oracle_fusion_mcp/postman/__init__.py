"""Postman collection generation from the compiled Oracle spec indexes.

`build_postman` is the entry point; the modules beneath it are:

  * `dedupe`     — pool schemas by content fingerprint, memoize their examples
  * `examples`   — schema to example request body, cycle-safe and depth-capped
  * `collection` — Postman v2.1.0 primitives
  * `emit`       — folder tree, size-based splitting, skipped-operation manifest
"""

from __future__ import annotations

__all__ = ["collection", "dedupe", "emit", "examples"]
