#!/usr/bin/env python3
"""Compress oversized OpenAPI specs into committable .json.gz files.

GitHub rejects any file over 100MB. The Oracle Fusion SCM and CX specs are
274MB and 180MB as pretty-printed JSON, but ~9MB and ~7MB once minified and
gzipped, so they are stored compressed and the raw .json files are gitignored.

Run after downloading or refreshing a spec from Oracle:

    python3 scripts/pack_specs.py
"""

import gzip
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

# Specs above this size are stored compressed; smaller ones stay as plain JSON
# so they remain browsable on GitHub.
THRESHOLD_BYTES = 50 * 1024 * 1024


def pack(source: pathlib.Path) -> pathlib.Path:
    target = source.with_suffix(".json.gz")
    spec = json.loads(source.read_text(encoding="utf-8"))
    minified = json.dumps(spec, separators=(",", ":")).encode("utf-8")
    # mtime=0 keeps the output byte-identical across runs, so re-packing an
    # unchanged spec produces no git diff.
    with gzip.GzipFile(target, "wb", compresslevel=9, mtime=0) as fh:
        fh.write(minified)
    print(
        f"{source.name}\n"
        f"  {source.stat().st_size / 1048576:7.1f} MB raw"
        f" -> {target.stat().st_size / 1048576:6.1f} MB gzipped"
    )
    return target


def main() -> int:
    sources = [
        p
        for p in sorted(REPO.glob("*.json"))
        if p.stat().st_size > THRESHOLD_BYTES
    ]
    if not sources:
        print(f"No .json files over {THRESHOLD_BYTES / 1048576:.0f} MB found.")
        return 0
    for source in sources:
        pack(source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
