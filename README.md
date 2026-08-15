# Oracle API clone

OpenAPI 3.0 specifications for the Oracle Fusion Cloud REST APIs.

## Specs

| Spec | Stored as | Paths |
| --- | --- | --- |
| REST API for Oracle Fusion Cloud SCM | `.json.gz` (9.0 MB) | 6,174 |
| REST API for Sales and Fusion Service in Oracle Fusion Cloud Customer Experience | `.json.gz` (6.9 MB) | 5,719 |
| REST API for Common Features in Oracle Fusion Cloud Applications | `.json` (4.3 MB) | — |
| REST API Services for Oracle CPQ | `.json` (4.1 MB) | — |

The SCM and CX specs are 274 MB and 180 MB as pretty-printed JSON, well past
GitHub's 100 MB per-file limit. They are committed minified and gzipped
instead — a ~30x reduction — and the raw `.json` files are gitignored. The two
smaller specs are stored as plain JSON so they stay browsable on GitHub.

## MCP servers

[`mcp-servers/`](mcp-servers/) wraps three of these specs — SCM, CX and Common
Features — as MCP servers for Claude, using a search + execute tool pattern so
that ~19,700 operations cost ~2,800 tokens of context instead of millions. The
CPQ spec is not wrapped.

```sh
cd mcp-servers
uv run oracle-fusion-build-index   # compile the specs into SQLite indexes
uv run pytest
```

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
