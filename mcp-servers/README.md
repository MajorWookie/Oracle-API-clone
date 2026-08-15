# Oracle Fusion MCP servers

Three MCP servers, one per OpenAPI spec in this repo:

| Server | Spec | Operations | Base path |
| --- | --- | --- | --- |
| `oracle-fusion-scm-mcp` | Oracle Fusion Cloud SCM | 10,335 | `/fscmRestApi/resources/11.13.18.05` |
| `oracle-fusion-cx-mcp` | Sales and Fusion Service (CX) | 8,861 | `/crmRestApi/resources/11.13.18.05` |
| `oracle-fusion-common-mcp` | Common Features | 485 | mixed, see below |

The Oracle CPQ spec is intentionally not wrapped over MCP — it is a different
product with its own authentication model. It is still compiled to an index
(`indexes/cpq.db`), because the Postman generator below covers all four specs.

## Why search + execute, not one tool per endpoint

Every tool schema a server exposes is spent from the context window on *every*
turn. At 10,335 operations, one tool per endpoint would cost millions of tokens
before the conversation started.

These servers instead keep the whole catalog in a prebuilt SQLite index and
expose nine fixed tools. The measured tool-list overhead is **~2,800 tokens**,
constant regardless of spec size.

```
search_operations   find operations by keyword           readOnly
list_categories     top-level functional areas           readOnly
list_resources      resources within a category          readOnly
list_operations     every operation on one resource      readOnly
describe_operation  parameters, request body, paths      readOnly
describe_schema     expand a named schema                readOnly
invoke_read         run a GET                            readOnly
invoke_write        run a POST / PUT / PATCH             destructiveHint: false
invoke_delete       run a DELETE                         destructiveHint: true
```

Reads, writes and deletes are separate tools. No single tool can both read and
modify Fusion data, so a host can auto-approve retrieval while still prompting
on mutation. Each execute tool rejects operations of the wrong kind and names
the correct tool in the error.

## Setup

### 1. Build the indexes

The specs are far too large to parse at server start — SCM alone is 287 MB of
JSON. Compile them once:

```sh
cd mcp-servers
uv run oracle-fusion-build-index
```

Takes about 5 seconds total and writes `indexes/{scm,cx,common,cpq}.db`
(72 MB, 50 MB, 1.7 MB, 2.1 MB). The indexes are gitignored; rebuild after
refreshing a spec. Operation bodies are stored zlib-compressed, which is what
keeps the SCM index roughly 4x smaller than its source.

CPQ is a Swagger 2.0 document; [`swagger2.py`](src/oracle_fusion_mcp/swagger2.py)
upconverts it on load so that nothing downstream has to read two dialects.

### 2. Configure credentials

Every setting takes a shared form or a per-server override. The per-server form
wins:

```sh
ORACLE_FUSION_HOST=your-pod.fa.us2.oraclecloud.com   # shared by all three
ORACLE_FUSION_SCM_HOST=...                           # overrides, SCM only
```

| Variable | Required | Notes |
| --- | --- | --- |
| `..._HOST` | yes | Pod hostname. `https://` is assumed if no scheme is given. |
| `..._USERNAME` / `..._PASSWORD` | one of | HTTP Basic, what most Fusion pods accept. |
| `..._TOKEN` | one of | OAuth bearer token. Takes precedence over Basic if both are set. |
| `..._BASE_PATH` | no | Override when Oracle bumps the `11.13.18.05` resource version. |
| `..._TIMEOUT` | no | Seconds, default 60. |
| `..._MAX_RESPONSE_CHARS` | no | Response truncation limit, default 40,000. |
| `..._INSECURE_SKIP_TLS_VERIFY` | no | Only for a sandbox with a self-signed certificate. |
| `ORACLE_FUSION_INDEX_DIR` | no | Where to find the compiled indexes. |

A server with no credentials still starts and its catalog tools still work —
only the `invoke_*` tools fail, naming exactly which variables are missing. That
makes the servers useful for exploring the API surface with no pod at all.

### 3. Register with Claude

`claude_desktop_config.json`, or `.mcp.json` for Claude Code:

```json
{
  "mcpServers": {
    "oracle-fusion-scm": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/Oracle API clone/mcp-servers", "oracle-fusion-scm-mcp"],
      "env": {
        "ORACLE_FUSION_HOST": "your-pod.fa.us2.oraclecloud.com",
        "ORACLE_FUSION_USERNAME": "your.user",
        "ORACLE_FUSION_PASSWORD": "your-password"
      }
    },
    "oracle-fusion-cx": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/Oracle API clone/mcp-servers", "oracle-fusion-cx-mcp"],
      "env": {
        "ORACLE_FUSION_HOST": "your-pod.fa.us2.oraclecloud.com",
        "ORACLE_FUSION_USERNAME": "your.user",
        "ORACLE_FUSION_PASSWORD": "your-password"
      }
    },
    "oracle-fusion-common": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/Oracle API clone/mcp-servers", "oracle-fusion-common-mcp"],
      "env": {
        "ORACLE_FUSION_HOST": "your-pod.fa.us2.oraclecloud.com",
        "ORACLE_FUSION_USERNAME": "your.user",
        "ORACLE_FUSION_PASSWORD": "your-password"
      }
    }
  }
}
```

Register only the servers you need — each one costs ~2,800 tokens of tool
schemas, so three is ~8,400.

## Typical flow

```
search_operations  {"query": "work order", "kind": "write"}
  -> create_workOrders, POST /fscmRestApi/resources/11.13.18.05/workOrders

describe_operation {"operation_id": "create_workOrders"}
  -> required: InventoryItemId, PlannedStartQuantity, WorkOrderType

invoke_write       {"operation_id": "create_workOrders",
                    "body": {"InventoryItemId": 300100, ...}}
```

`invoke_read` accepts Oracle's framework query parameters directly:

```json
{"operation_id": "getall_workOrders",
 "query": {"q": "WorkOrderNumber='WO-1001'", "fields": "WorkOrderId,StatusCode",
           "limit": 10, "totalResults": true}}
```

## Base paths

No spec declares a `servers` block, so the base path for each is recorded in
[`specs.py`](src/oracle_fusion_mcp/specs.py). The values were derived from the
`/<root>RestApi/resources/<version>` URLs the specs embed in their own
`components` sections — `fscmRestApi` appears 6,144 times in SCM, `crmRestApi`
4,819 times in CX.

The Common Features spec is the awkward one: of its 386 path keys, 120 start
with `https:`, 59 with `<servername>`, 54 with `http:` and 20 with a bare
`servername` token, while some already carry an API root (`/hcmRestApi/scim/Users`)
and others do not (`/announcements`). [`paths.py`](src/oracle_fusion_mcp/paths.py)
reduces all of them to one absolute, host-free form, prefixing the default base
path only where no API root is present. Roots are recognized by pattern
(`*Api`, `*UI`) plus a small explicit list, so unfamiliar ones like `fndSetupApi`
are not double-prefixed.

## Postman collections

The same indexes back a Postman exporter, which writes to
[`../postman/`](../postman/):

```sh
uv run oracle-fusion-build-postman              # all four specs
uv run oracle-fusion-build-postman scm cpq      # a subset
uv run oracle-fusion-build-postman --max-child-depth 3 --max-mb 8
```

Folders mirror each spec's own taxonomy — tag category, then root resource.
Request bodies are materialized from the schemas, since Postman has no `$ref`:
identical schemas are pooled by content fingerprint, `$ref` cycles are cut with a
`<recursive: Name>` marker, and expansion stops at depth 3. Collections over
`--max-mb` are split per category; SCM produces nine, one per functional area.

Operations nested deeper than `--max-child-depth` are omitted and recorded in
`postman/skipped-operations.tsv`, so the omission is auditable rather than
silent. Output is deterministic — ids come from `uuid5` of the collection name,
not a random generator — so regenerating an unchanged spec produces no diff.

See [`postman/README.md`](../postman/README.md) for import instructions.

## Tests

```sh
uv run pytest
```

169 tests. Those touching the real indexes skip cleanly if you have not built
them yet.

## Known limitations

- **Base paths are inferred, not declared.** They match Oracle's documented
  conventions and the specs' own embedded URLs, but have not been exercised
  against a live pod. Override with `..._BASE_PATH` if your pod differs.
- **`/persons` in Common Features** is a bare resource with no API root in the
  spec; it is assumed to be under `fscmRestApi`. If your pod serves it from
  `hcmRestApi`, that one operation will 404.
- **Deeply nested schemas are truncated** at depth 3 and 60 properties. Required
  properties always survive; the rest are reachable via `describe_schema`.
- **No live-pod verification.** Every HTTP path in the test suite runs against a
  mock transport. Nothing here has issued a real request to Oracle.
