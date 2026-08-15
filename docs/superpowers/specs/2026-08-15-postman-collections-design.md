# Postman collections from the Oracle Fusion specs

Date: 2026-08-15
Status: approved, implementing

## Goal

Convert the four Oracle OpenAPI specs in this repo into Postman collections that
a person can import and run against a Fusion pod, with schema duplication and
`$ref` recursion removed from the generated output.

## What the specs actually contain

Measured from the compiled indexes, plus a direct parse of the CPQ spec:

| | SCM | CX | Common | CPQ |
| --- | --- | --- | --- | --- |
| Operations | 10,335 | 8,861 | 485 | 974 |
| Schemas | 22,405 | 14,173 | 635 | 1,016 (`definitions`) |
| Byte-identical duplicate schemas | 6,767 (30%) | 3,658 (26%) | 36 (6%) | — |
| Schemas on a `$ref` cycle | 11 | 91 | 0 | — |
| Deepest `/child/` nesting | 7 | 4 | 2 | flat |
| Tag categories | 9 | 369 | 60 | 112 |

20,655 operations in total.

### Redundancy is not where it first appears

Three separate things look like redundancy, and only two of them are:

1. **Duplicate schema bodies are real.** 30% of SCM's schemas are byte-for-byte
   identical to another schema under a different name.
2. **Recursion is real but rare.** 11 SCM and 91 CX schemas sit on a `$ref`
   cycle. Rare enough to be easy to miss, fatal if example generation does not
   cut them.
3. **Repeated child endpoints are mostly an illusion.** `smartActions` appears
   under 1,944 distinct CX paths, but grouped by method plus child-suffix the
   worst repeat is 36 — the paths differ because the parents differ. Capping
   `/child/` depth at 2 still retains 87–91% of operations, and collapsing
   child subtrees parametrically retains 75–83%.

So roughly 19k of the 20,655 endpoints are genuinely distinct URLs. Endpoint
count is close to irreducible without deleting coverage. Compaction has to come
from bytes per request and shared schema work, and the output has to be split
across multiple collections regardless.

## Approach

Generate from the existing SQLite indexes rather than re-parsing the specs.
`refs.py` already does bounded, cycle-safe `$ref` resolution and `paths.py`
already normalizes the Common Features path dialects; both are tested. The
alternative — a standalone script under `scripts/` — would duplicate that logic
and pay a 274 MB JSON parse per run. An off-the-shelf converter
(`openapi-to-postman`) was rejected: it gives up control over precisely the two
transformations this task is about, and adds a Node toolchain.

CPQ is Swagger 2.0 and is upconverted to OpenAPI 3 shape before indexing, so all
four specs flow through one code path.

## Components

New subpackage `mcp-servers/src/oracle_fusion_mcp/postman/`:

| Module | Responsibility |
| --- | --- |
| `dedupe.py` | Fingerprint schemas by SHA-1 of canonical JSON; pool identical bodies; memoize generated examples per fingerprint. |
| `examples.py` | Schema to example request body. Cycle-safe, depth-capped, required properties first, `readOnly` excluded. |
| `collection.py` | Postman v2.1.0 primitives: collection, folder, item, url, auth, variable. |
| `emit.py` | Folder tree assembly, size-based splitting, skipped-operations manifest. |
| `build_postman.py` | CLI entry `oracle-fusion-build-postman`. |

Plus `swagger2.py` at package top level (it serves indexing, not just Postman)
and a `CPQ` entry in `specs.py`.

### Redundancy removal, concretely

- **Schema duplication** — one pooled entry per fingerprint. Identical schemas
  therefore produce identical example bodies and are computed once. This is a
  generation-cost and consistency win; Postman has no `$ref`, so it does not
  shrink the emitted JSON directly.
- **Recursion** — reuse the `(section, name)` seen-set from `refs.resolve`. A
  cycle emits the string `"<recursive: SchemaName>"` instead of looping.
  Depth cap 3.
- **Endpoints** — `/child/` depth capped at 2 (`--max-child-depth`, default 2).
  Every omitted operation is written to `skipped-operations.tsv` with its method,
  path and reason. Nothing disappears silently.
- **Bytes** — Oracle's framework query parameters (`q`, `fields`, `limit`,
  `offset`, `expand`, `onlyData`, `links`, `totalResults`, `orderBy`, `finder`)
  become collection variables rather than repeating their full descriptions
  across ~19k requests. Per-request descriptions are trimmed to one line.

### Requests

- Collection-level HTTP Basic auth on `{{username}}` / `{{password}}`, matching
  `config.py`'s precedence, with the bearer alternative documented in the
  collection description.
- `{{baseUrl}}` = `https://{{host}}{{basePath}}`; `basePath` defaults to the
  spec's `default_base_path`.
- Spec `{OrderId}` becomes Postman `:OrderId` with a matching path-variable entry.
- Bodies carry required fields only, values derived from `type` / `format` /
  `enum`. `readOnly` properties are excluded.
- Query parameters are present but `disabled` unless required.
- No test scripts.

### Folders and splitting

Folders mirror the spec's own tags: category then resource. SCM (9 categories),
Common (60) and CPQ (112 tags, already `Category/Subcategory`) nest cleanly. CX
has 369 categories; they are emitted alphabetically sorted rather than bucketed
under a grouping Oracle does not define.

A collection is split per top-level category when it would exceed
`--max-bytes` (default 15 MB). One shared environment file is emitted alongside.

### Output

`postman/` at the repo root, minified JSON, committed:

```
postman/Oracle Fusion SCM.postman_collection.json
postman/Oracle Fusion CX.postman_collection.json
postman/Oracle Fusion Common Features.postman_collection.json
postman/Oracle CPQ.postman_collection.json
postman/Oracle Fusion.postman_environment.json
postman/skipped-operations.tsv
```

## Testing

Extends the existing suite:

- A cyclic schema must terminate and produce the recursion marker. The existing
  `MINI_SPEC` fixture already contains a `PurchaseOrder` to `OrderLine` cycle.
- Fingerprint pooling: two identical schemas under different names collapse to
  one entry and yield the same example.
- Path to Postman URL conversion, including the Common Features `<servername>`
  and absolute-URL forms.
- Swagger 2.0 upconversion: `definitions`, `body` parameters, bare `type`.
- No `$ref` or `$ref_unexpanded` key survives into any emitted request body.
- A real-index test that skips cleanly when indexes have not been built,
  asserting every emitted request has a resolvable URL.

## Known limitations

Carried forward from the MCP servers, and stated in the generated collection
descriptions:

- Base paths are inferred, not declared by the specs. Override via the
  environment file.
- No live-pod verification. No emitted request has been issued against Oracle.
- Deeply nested endpoints beyond `--max-child-depth` are absent from the
  collections by design; consult `skipped-operations.tsv`.
