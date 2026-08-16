# Oracle API clone

OpenAPI 3.0 specifications for the Oracle Fusion Cloud REST APIs.

## Specs

| Spec | Stored as | Paths |
| --- | --- | --- |
| REST API for Oracle Fusion Cloud SCM | `.json.gz` (9.0 MB) | 6,174 |
| REST API for Sales and Fusion Service in Oracle Fusion Cloud Customer Experience | `.json.gz` (6.9 MB) | 5,719 |
| REST API for Common Features in Oracle Fusion Cloud Applications | `.json` (4.3 MB) | 386 |
| REST API Services for Oracle CPQ | `.json` (4.1 MB) | 668 |

CPQ is the odd one out: it is a Swagger 2.0 document, while the other three are
OpenAPI 3.0. It is upconverted on load rather than consumed as a second dialect.

The SCM and CX specs are 274 MB and 180 MB as pretty-printed JSON, well past
GitHub's 100 MB per-file limit. They are committed minified and gzipped
instead — a ~30x reduction — and the raw `.json` files are gitignored. The two
smaller specs are stored as plain JSON so they stay browsable on GitHub.

## MCP servers

[`mcp-servers/`](mcp-servers/) wraps three of these specs — SCM, CX and Common
Features — as MCP servers for Claude, using a search + execute tool pattern so
that ~19,700 operations cost ~2,800 tokens of context instead of millions. CPQ is
compiled to an index for Postman export but is not served over MCP.

```sh
cd mcp-servers
uv run oracle-fusion-build-index   # compile all four specs into SQLite indexes
uv run pytest
```

## Postman collections

[`postman/`](postman/) holds ready-to-import Postman collections generated from
all four specs — 18,507 requests across 12 collections.

| Spec | Collections | Requests |
| --- | --- | --- |
| SCM | 9, one per functional area | 8,956 |
| CX | 1 | 8,092 |
| Common Features | 1 | 485 |
| CPQ | 1 | 974 |

Import a collection plus `Oracle Fusion.postman_environment.json`, set `host` to
your pod and fill in `username` / `password`. Every request URL is built as
`{{baseUrl}}/{{basePath}}/...`, so following an Oracle resource-version bump is a
one-variable edit.

```sh
cd mcp-servers
uv run oracle-fusion-build-index      # required first — the generator reads the indexes
uv run oracle-fusion-build-postman
```

Generation is deterministic, so regenerating an unchanged spec produces no diff.

Three things are flattened out of the OpenAPI structure on the way in:

- **Duplicate schemas.** 30% of SCM's 22,405 schemas are byte-identical to
  another schema under a different name (26% of CX's). They are pooled by content
  fingerprint, so identical schemas yield identical bodies and are expanded once.
- **Recursion.** Oracle's parent/child schemas are self-referential, so `$ref`
  expansion cuts any reference already open on the current branch and leaves a
  `<recursive: Name>` marker. Depth is capped at 3.
- **Child nesting.** SCM nests `/child/` segments up to seven deep. Operations
  more than two levels deep are omitted and listed in
  `postman/skipped-operations.tsv` — 2,148 of the 20,655 operations. Raise the
  cap with `--max-child-depth`.

What is *not* flattened is the endpoint count. Roughly 19,000 of the 20,655
operations are genuinely distinct URLs: grouping by method plus child-suffix, the
most repeated child endpoint recurs only 36 times, so there is nothing to
collapse without losing coverage.

## Reading a spec

Read the compressed specs directly; there is no need to decompress to disk.

```python
import gzip, json

spec = json.load(gzip.open("REST API for Oracle Fusion Cloud SCM.json.gz"))
print(len(spec["paths"]))
```

```javascript
const { gunzipSync } = require("node:zlib");
const { readFileSync } = require("node:fs");

const spec = JSON.parse(
  gunzipSync(readFileSync("REST API for Oracle Fusion Cloud SCM.json.gz")),
);
```

Tools that only accept a file path need the expanded form:

```sh
python3 scripts/unpack_specs.py    # writes the full-size .json files (gitignored)
```

## Refreshing a spec

After downloading a newer spec from Oracle, drop the `.json` in the repo root
and re-pack it:

```sh
python3 scripts/pack_specs.py      # minifies + gzips any .json over 50 MB
```

Packing is deterministic, so re-packing an unchanged spec produces no diff.
