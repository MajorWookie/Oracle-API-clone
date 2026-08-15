"""CLI: compile the spec indexes into Postman collections.

Usage:
    uv run oracle-fusion-build-postman              # all four specs
    uv run oracle-fusion-build-postman scm cpq      # a subset
    uv run oracle-fusion-build-postman --max-child-depth 3

Reads the SQLite indexes rather than the specs themselves, so
`oracle-fusion-build-index` has to have run first.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from ..config import index_dir
from ..index import IndexNotBuilt, SpecIndex
from ..specs import ALL_SPECS, SPECS_BY_KEY, SpecDef
from . import collection as pc
from . import emit

ENVIRONMENT_NAME = "Oracle Fusion"


def default_out_dir() -> Path:
    """`postman/` at the repo root, alongside the spec files."""
    return Path(__file__).resolve().parents[4] / "postman"


def write_environment(out_dir: Path) -> Path:
    """One environment shared by every collection.

    Holds only what is pod-specific rather than spec-specific. `basePath` is a
    collection variable instead, since each spec has its own.
    """
    document = pc.environment(
        ENVIRONMENT_NAME,
        [
            ("host", "your-pod.fa.us2.oraclecloud.com"),
            ("username", ""),
            ("password", ""),
            ("token", ""),
        ],
    )
    destination = out_dir / f"{ENVIRONMENT_NAME}.postman_environment.json"
    destination.write_text(
        json.dumps(document, separators=(",", ":"), ensure_ascii=False), encoding="utf-8"
    )
    return destination


def build_one(
    definition: SpecDef,
    out_dir: Path,
    indexes: Path,
    *,
    max_child_depth: int,
    max_bytes: int,
) -> tuple[emit.BuildStats, list[tuple[str, object, str]]]:
    index = SpecIndex(indexes / definition.index_filename)
    started = time.monotonic()
    stats, skipped = emit.build_collections(
        definition,
        index,
        out_dir,
        max_child_depth=max_child_depth,
        max_bytes=max_bytes,
    )
    index.close()

    elapsed = time.monotonic() - started
    pool = stats.pool
    print(
        f"[{definition.key}] {stats.emitted:,} requests in {len(stats.files)} "
        f"collection(s), {stats.total_bytes / 1_048_576:.1f} MB "
        f"({stats.bodies:,} bodies, {stats.truncated_bodies:,} abbreviated; "
        f"{pool.get('duplicate_schemas', 0):,} duplicate schemas pooled, "
        f"{pool.get('example_cache_hits', 0):,} example cache hits; "
        f"{stats.skipped_depth:,} operations skipped) in {elapsed:.1f}s",
        flush=True,
    )
    return stats, [(definition.key, operation, reason) for operation, reason in skipped]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="oracle-fusion-build-postman",
        description="Generate Postman collections from the compiled Oracle spec indexes.",
    )
    parser.add_argument(
        "specs",
        nargs="*",
        choices=[s.key for s in ALL_SPECS],
        help="Which specs to convert. Defaults to all four.",
    )
    parser.add_argument(
        "--out", type=Path, default=default_out_dir(), help="Directory to write collections into."
    )
    parser.add_argument(
        "--index-dir", type=Path, default=None, help="Where the compiled .db indexes live."
    )
    parser.add_argument(
        "--max-child-depth",
        type=int,
        default=emit.DEFAULT_MAX_CHILD_DEPTH,
        help="Deepest `/child/` nesting to include. Deeper operations go to the manifest.",
    )
    parser.add_argument(
        "--max-mb",
        type=float,
        default=emit.DEFAULT_MAX_BYTES / 1_048_576,
        help="Split a collection once it exceeds this size in megabytes.",
    )
    args = parser.parse_args(argv)

    selected = [SPECS_BY_KEY[k] for k in args.specs] if args.specs else list(ALL_SPECS)
    indexes = args.index_dir or index_dir()
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    all_skipped: list[tuple[str, object, str]] = []
    failures = 0
    for definition in selected:
        try:
            _, skipped = build_one(
                definition,
                out_dir,
                indexes,
                max_child_depth=args.max_child_depth,
                max_bytes=int(args.max_mb * 1_048_576),
            )
            all_skipped.extend(skipped)
        except IndexNotBuilt as error:
            print(f"[{definition.key}] SKIPPED: {error}", file=sys.stderr)
            failures += 1

    if all_skipped:
        manifest = out_dir / "skipped-operations.tsv"
        emit.write_skipped_manifest(manifest, all_skipped)  # type: ignore[arg-type]
        print(f"{len(all_skipped):,} skipped operations recorded in {manifest.name}")

    environment = write_environment(out_dir)
    print(f"environment -> {environment.name}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
