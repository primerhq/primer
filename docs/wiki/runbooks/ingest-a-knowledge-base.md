---
title: Ingest a knowledge base
summary: Create an owned collection, persist text and ZIP entries, verify bodies and explicitly prove semantic search.
audience: admin
tools: [system__create_collection, system__get_collection, system__list_collections, system__create_document, system__put_document, system__get_document_content, system__list_documents, system__describe_tool, collections_api__import_collection_zip, collections__collection_tree, collections__search]
related: [knowledge/collections, knowledge/documents, knowledge/navigation, runbooks/enable-collection-search]
---
## Goal

Ingest a small text knowledge base into a collection, reread its durable bodies and prove that real text can be retrieved.

## Preconditions

- Run as a known authenticated `user`. New collections are stamped with the creator as owner; document writes and ZIP imports require full access on an existing collection, while reads/search need read. Search setup separately requires owner access.
- Inspect the visible inventory with [`system__list_collections`](reference/tools/system/list_collections) and [`system__get_collection`](reference/tools/system/get_collection). Page the list, choose `team-knowledge` only when unused, and reuse an existing row only when you have full access. A hidden row can still cause a create conflict; stop or choose another id, do not probe its contents.

```json
{"toolset_id": "system", "tool_name": "list_collections", "arguments": {"limit": 200}}
```

```json
{"toolset_id": "system", "tool_name": "get_collection", "arguments": {"id": "team-knowledge"}}
```

- Prepare UTF-8 text and read [enable collection search](runbooks/enable-collection-search) before writing when semantic retrieval is required: an admin must confirm usable embedding/vector provider ids and a model. With no embedder the collection still supports text storage and lexical search, but do not claim semantic completion.
- Use [`system__describe_tool`](reference/tools/system/describe_tool) to read the live policies before writing. The three search/import generated mutators ship required gates; reads stay ungated. Create and put ship no gate, but an operator can add one; inspect the current policy on each exact write you will perform.

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "create_collection"}}
```

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "system", "tool_name": "put_document"}}
```

```json
{"toolset_id": "system", "tool_name": "describe_tool", "arguments": {"toolset_id": "collections_api", "tool_name": "import_collection_zip"}}
```

## Roles and gates

Runner: `user`; new row owner, or full on the existing collection. A reader can verify/search but cannot import. A non-owner with full may import; full alone still cannot enable or disable search.

| write | shipped gate | who decides and what to check |
|---|---|---|
| [`system__create_collection`](reference/tools/system/create_collection) | none | creator owns the new row; inspect any live operator-added policy |
| [`system__put_document`](reference/tools/system/put_document) | none | needs full; inspect any live operator-added policy |
| [`collections_api__import_collection_zip`](reference/tools/collections_api/import_collection_zip) | required | stamped live approver or admin; check collection, parent, conflict policy and archive size before approval |

For an import, the worker turn parks until approval; do not retry while parked. A rejected or unapproved call has no requested import effect. The handler still checks full access after approval. The generated tool translates base64 bytes into its multipart file; it is not a JSON document-body endpoint.

## Steps

1. Create a grep-only collection with [`system__create_collection`](reference/tools/system/create_collection). Expect a durable id, `access.owners` naming you and `search=null`. Skip this creation for an already verified writable collection.

```json
{"toolset_id": "system", "tool_name": "create_collection", "arguments": {"entity": {"id": "team-knowledge", "description": "Cedar team operating knowledge"}}}
```

2. Store actual text with [`system__put_document`](reference/tools/system/put_document). Bodies live in the content store, not in `Document.meta`. [`system__create_document`](reference/tools/system/create_document) is the lower-level metadata-row operation; creating metadata alone is not proof that a body exists. Prefer the path-addressed put for ingestion; a repeated put updates that path.

```json
{"toolset_id": "system", "tool_name": "put_document", "arguments": {"collection_id": "team-knowledge", "path": "overview", "title": "Cedar overview", "content": "# Cedar\nCedar on-call escalation uses the blue pager."}}
```

3. Reread the body with [`system__get_document_content`](reference/tools/system/get_document_content) and list paths with [`system__list_documents`](reference/tools/system/list_documents). Require the exact stored text, not just a metadata id. A listing contains paths and sizes, not bodies, and pages at 1000 entries: `next_after_path` in the answer is the cursor to pass back as `after_path` for the next page (null on the last one), so a large collection is read in pages rather than lost.

```json
{"toolset_id": "system", "tool_name": "get_document_content", "arguments": {"collection_id": "team-knowledge", "path": "overview"}}
```

```json
{"toolset_id": "system", "tool_name": "list_documents", "arguments": {"collection_id": "team-knowledge"}}
```

4. Import a ZIP using [`collections_api__import_collection_zip`](reference/tools/collections_api/import_collection_zip). The worked base64 value is a real small archive containing `Guides/Oncall.md` with Cedar on-call text. Supply your own archive bytes for your own ingestion; do not send a filename or URL as `body.file`. The turn parks here; do not retry until a person decides.

```json
{"toolset_id": "collections_api", "tool_name": "import_collection_zip", "arguments": {"collection_id": "team-knowledge", "parent": "", "conflict": "fail", "body": {"file": "UEsDBBQAAAAAAAAAIVxa612KNgAAADYAAAAQAAAAR3VpZGVzL09uY2FsbC5tZCMgQ2VkYXIgb24tY2FsbApDZWRhciBlc2NhbGF0aW9uIHVzZXMgdGhlIGJsdWUgcGFnZXIuClBLAQIUAxQAAAAAAAAAIVxa612KNgAAADYAAAAQAAAAAAAAAAAAAACAAQAAAABHdWlkZXMvT25jYWxsLm1kUEsFBgAAAAABAAEAPgAAAGQAAAAAAA=="}}}
```

   Inspect every report list: `created`, `skipped`, `overwritten`, `rejected`. This example creates `guides` and `guides/oncall`. The importer slugifies path segments and strips text/Markdown extensions. Binary or non-UTF-8 entries are reported as rejected. The archive may contain at most 10,000 entries, uploads are capped at 32 MiB and total expansion at 128 MiB; individual document body limits also apply.

   `conflict=fail` stops at the first occupied path with HTTP 409 and `extensions.report`; earlier writes can remain, so it is not an all-or-nothing import. `skip` retains existing content and `overwrite` deliberately replaces it. Read the report and existing bodies before choosing a new approved attempt; do not blindly retry a partial import.

5. Navigate the imported paths with [`collections__collection_tree`](reference/tools/collections/collection_tree), then reread the imported body through the content tool.

```json
{"toolset_id": "collections", "tool_name": "collection_tree", "arguments": {"collection": "team-knowledge", "parent": "", "depth": 3}}
```

```json
{"toolset_id": "system", "tool_name": "get_document_content", "arguments": {"collection_id": "team-knowledge", "path": "guides/oncall"}}
```

6. Follow [enable collection search](runbooks/enable-collection-search) as the owner, with the confirmed usable providers, and verify its required approval and inline backfill. Subsequent text writes are followed by best-effort indexing; a stored body does not guarantee its vectors were updated. Inspect search status and actual retrieval after writes.

## Verification

Reread both bodies and the collection ownership. Reread the live import policy with the same `system__describe_tool` call from Preconditions, checking the policy that actually governed this write. Inspect the import report for partial/rejected work. First prove the text path with [`collections__search`](reference/tools/collections/search); this needs no embedding provider.

```json
{"toolset_id": "collections", "tool_name": "search", "arguments": {"collection": "team-knowledge", "query": "Cedar", "mode": "literal", "limit": 5}}
```

When semantic setup is part of the goal, force semantic mode and require `mode_used=semantic` plus a hit for the imported `guides/oncall` text. `auto` can fall back to fulltext and is not semantic acceptance. Verify the actual indexed source, not an invented answer or only a successful upload response.

```json
{"toolset_id": "collections", "tool_name": "search", "arguments": {"collection": "team-knowledge", "query": "Cedar on-call escalation", "mode": "semantic", "path_prefix": "guides", "limit": 5}}
```

## Refusal and failure

| result | meaning and next move |
|---|---|
| `type=approval_required` / parked import | a person decides; do not retry while parked |
| native MCP `tool_unavailable` | opaque role/exposure/gate refusal; verify the live tool/gate and ask an authorized operator, do not bypass it |
| rejected approval | requested import not permitted; verify no requested new paths appeared |
| forbidden / HTTP 403 | full access is missing, or the collection is system-owned; approval does not override either check |
| not found / HTTP 404 | collection or parent path is absent/hidden; inspect the visible tree and ask its owner |
| conflict / HTTP 409 | id/path collision or partial `fail` import; inspect `extensions.report`, then reread before skip/overwrite |
| bad request / HTTP 400 | empty or invalid ZIP; fix the archive bytes |
| payload too large / HTTP 413 | upload, entry-count or expansion cap; split the input and review the report |
| rejected report entries | binary, non-UTF-8 or oversized document entries were not imported; inspect each reason and do not report complete ingestion until addressed |
| semantic mode unavailable / provider conflict | text can still exist; follow the search runbook and fix provider use/configuration |

## Related

- [knowledge/collections](knowledge/collections): inherited access.
- [knowledge/documents](knowledge/documents): body and metadata storage.
- [knowledge/navigation](knowledge/navigation): tree paths.
- [runbooks/enable-collection-search](runbooks/enable-collection-search): provider preconditions, approval and retrieval proof.
