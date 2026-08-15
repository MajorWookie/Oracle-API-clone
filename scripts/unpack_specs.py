#!/usr/bin/env python3
"""Restore the pretty-printed .json specs from their committed .json.gz form.

Only needed if you want the full-size files on disk (to grep or open them in an
editor). Code should read the .gz directly instead:

    import gzip, json
    spec = json.load(gzip.open("REST API for Oracle Fusion Cloud SCM.json.gz"))

Usage:

    python3 scripts/unpack_specs.py
"""

import gzip
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent


def unpack(source: pathlib.Path) -> pathlib.Path:
    target = source.with_suffix("")  # drops the .gz, leaving .json
    with gzip.open(source, "rt", encoding="utf-8") as fh:
        spec = json.load(fh)
    with target.open("w", encoding="utf-8") as fh:
        json.dump(spec, fh, indent=4)
    print(
        f"{source.name}\n"
        f"  {source.stat().st_size / 1048576:6.1f} MB gzipped"
        f" -> {target.stat().st_size / 1048576:7.1f} MB raw"
    )
    return target


def main() -> int:
    sources = sorted(REPO.glob("*.json.gz"))
    if not sources:
        print("No .json.gz files found.")
        return 0
    for source in sources:
        unpack(source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
