# Postman collections for the Oracle Fusion REST APIs

18,507 requests across 12 collections, generated from the four OpenAPI specs in
this repo. Regenerate with `cd mcp-servers && uv run oracle-fusion-build-postman`.

| Collection | Requests |
| --- | --- |
| `REST API for Oracle Fusion Cloud SCM — <area>` (9 files) | 8,956 |
| `REST API for Sales and Fusion Service … Customer Experience` | 8,092 |
| `REST API for Common Features in Oracle Fusion Cloud Applications` | 485 |
| `REST API Services for Oracle CPQ` | 974 |

## Setup

1. Import the collection you want, plus `Oracle Fusion.postman_environment.json`.
2. Select the **Oracle Fusion** environment and set `host` to your pod, e.g.
   `your-pod.fa.us2.oraclecloud.com`.
3. Fill in `username` and `password`. Collections authenticate with HTTP Basic at
   the collection level, which is what most Fusion pods accept. For OAuth, switch
   the collection's auth type to Bearer Token and use `{{token}}`.

Every URL is `{{baseUrl}}/{{basePath}}/...`. `basePath` is a per-collection
variable holding Oracle's resource version (`fscmRestApi/resources/11.13.18.05`,
`rest/v19` for CPQ), so following a version bump is a single edit. Endpoints that
carry their own API root — Common Features' `/ess`, `/bpm` and `/api` services —
are absolute and unaffected.

Optional query parameters and headers ship **disabled**, so a request runs as-is;
enable the ones you need. `REST-Framework-Version`, `Metadata-Context` and
`Upsert-Mode` are there on the operations that accept them.

## Framework query parameters

Oracle's collection resources share a set of query parameters, present but
disabled on every applicable request:

| Parameter | Use |
| --- | --- |
| `q` | Filter expression, e.g. `WorkOrderNumber='WO-1001'` |
| `fields` | Comma-separated attributes to return |
| `limit` / `offset` | Page size and starting row |
| `orderBy` | Sort, e.g. `CreationDate:desc` |
| `expand` | Include named child resources inline |
| `finder` | Named finder, e.g. `PrimaryKey;OrderId=300100` |
| `totalResults` | `true` to include the total row count |
| `onlyData` | `true` to omit `links` from the payload |

## Request bodies

Postman has no `$ref`, so bodies are materialized from the schemas. A body
carries the schema's `required` fields where it declares any, otherwise a capped
sample of writable fields. Values are placeholders derived from type, format and
enum — `""`, `0`, `2026-01-01T00:00:00+00:00`, the first enum member.

Expansion is bounded, because Oracle's schemas are both enormous and
self-referential:

- `$ref` following stops at depth 3; deeper references appear as
  `<truncated: SchemaName>`.
- A reference already open on the current branch becomes
  `<recursive: SchemaName>` rather than being followed.
- Objects declaring no `required` list are capped at 12 properties.

Treat a body as a starting point, not a complete payload. For the full schema,
use the `describe_operation` and `describe_schema` tools on the MCP servers, or
read the spec directly.

## What is not here

`skipped-operations.tsv` lists 2,148 of the 20,655 operations that are omitted
because they nest more than two `/child/` levels deep — SCM goes seven deep. To
include them, regenerate with a higher cap:

```sh
uv run oracle-fusion-build-postman --max-child-depth 7
```

## Caveats

- **Base paths are inferred, not declared.** No spec ships a `servers` block, so
  each base path was derived from the URLs the spec embeds in its own
  `components` section. They match Oracle's documented conventions; override
  `basePath` if your pod differs.
- **No live-pod verification.** Nothing here has issued a request to Oracle. The
  generator's tests run against compiled indexes and a mock, not a pod.
