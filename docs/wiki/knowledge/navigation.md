---
title: "Navigate and read documents"
summary: "List collections, walk a collection's tree, read a document and filter by metadata."
order: 4
tools: [collections__collections_list, collections__collection_tree, collections__read_document, system__list_documents, system__get_document, system__get_document_content, system__list_collection_documents, system__find_documents, system__find_collection_documents_by_meta]
related: [knowledge/search, knowledge/documents]
---
## What it is
These tools find and read documents: the `collections` tools walk a collection's tree by path, and the `system` reads address documents by id or path. Locate a collection, discover what it holds, read a body and filter by metadata. Search by meaning or keyword is at [search a collection](knowledge/search).

## Concepts
- Two families of reads. The `collections` tools are path-addressed: `collections__collections_list` names every collection and the best search it offers (`semantic` or `fulltext`), `collections__collection_tree` walks a parent's subtree, and `collections__read_document` returns one document's body and its children. The `system` tools are row-level: `system__list_documents`, `system__get_document`, `system__get_document_content`, `system__list_collection_documents`, `system__find_documents` and `system__find_collection_documents_by_meta`.
- `collections__collection_tree` walks from a parent path (`""` is the root) to a `depth` of 1 to 10; `depth: 1` lists only the immediate children. The nodes carry `path` and `has_children`, so you can see where the tree branches before you read anything.
- `collections__read_document` returns the body and the document's children. A miss answers `type=not-found` and names the siblings it did find, so a wrong path tells you the right neighborhood.
- `system__list_documents` lists a collection's documents by path (paths, ids and sizes, no bodies), scoped by an optional `prefix`. `system__get_document` fetches one entity by id: its `meta`, `title`, `path` and timestamps, but never the body. `system__get_document_content` fetches the raw body by `(collection_id, path)` from the content store.
- `system__list_collection_documents` pages the entity rows of one collection (the server-side filter on `collection_id`). `system__find_documents` matches any field values with a predicate tree (operators `=`, `!=`, `~=`, `>`, `<`, `>=`, `<=`, `in`, `and`, `or`). `system__find_collection_documents_by_meta` is the flat shortcut: equality on `meta.<key>`, joined with AND.
- These reads are exact: they address, list or filter. For relevance, search instead.

## How to
Browse a collection and read a document by path:
1. Find the collection: `collections__collections_list` names each one and its best search.
2. Walk the tree: `collections__collection_tree` from the root at a small depth, deeper as you narrow in.
3. Read the page: `collections__read_document` by path (body plus children), or `system__get_document_content` when you only need the raw body.
4. Filter by metadata: `system__find_collection_documents_by_meta` with the keys you stored in `meta`.

## Tool calls
### List the collections and their best search
`collections__collections_list` names every collection and the best search it offers.

```json
{"toolset_id": "collections", "tool_name": "collections_list", "arguments": {}}
```

Reference: [`collections__collections_list`](reference/tools/collections/collections_list)

### Walk a collection's tree
`collections__collection_tree` lists the nodes under a parent path to a depth of 1 to 10.

```json
{"toolset_id": "collections", "tool_name": "collection_tree", "arguments": {"collection": "kb", "parent": "", "depth": 2}}
```

Reference: [`collections__collection_tree`](reference/tools/collections/collection_tree)

### Read a document's body and children
`collections__read_document` returns the body and the children; a miss names the siblings it did find.

```json
{"toolset_id": "collections", "tool_name": "read_document", "arguments": {"collection": "kb", "path": "guides/intro"}}
```

Reference: [`collections__read_document`](reference/tools/collections/read_document)

### List documents by path
`system__list_documents` lists paths, ids and sizes without loading any body.

```json
{"toolset_id": "system", "tool_name": "list_documents", "arguments": {"collection_id": "kb-1", "prefix": "concepts/"}}
```

Reference: [`system__list_documents`](reference/tools/system/list_documents)

### Fetch one document entity
`system__get_document` fetches the entity by id: its `meta`, `title`, `path` and timestamps.

```json
{"toolset_id": "system", "tool_name": "get_document", "arguments": {"id": "document-0123456789ab"}}
```

Reference: [`system__get_document`](reference/tools/system/get_document)

### Fetch a document's body by path
`system__get_document_content` reads the raw body from the content store at `(collection_id, path)`.

```json
{"toolset_id": "system", "tool_name": "get_document_content", "arguments": {"collection_id": "kb-1", "path": "concepts/slo.md"}}
```

Reference: [`system__get_document_content`](reference/tools/system/get_document_content)

### Page a collection's document rows
`system__list_collection_documents` pages the entity rows that belong to one collection.

```json
{"toolset_id": "system", "tool_name": "list_collection_documents", "arguments": {"collection_id": "kb-1", "limit": 20}}
```

Reference: [`system__list_collection_documents`](reference/tools/system/list_collection_documents)

### Find documents by field values
`system__find_documents` matches rows by a predicate tree; a `predicate` of null matches all rows.

```json
{"toolset_id": "system", "tool_name": "find_documents", "arguments": {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "id"}, "op": "=", "right": {"kind": "value", "value": "document-0123456789ab"}}}}
```

Reference: [`system__find_documents`](reference/tools/system/find_documents)

### Find documents by metadata equality
`system__find_collection_documents_by_meta` matches `meta.<key>` values, joined with AND.

```json
{"toolset_id": "system", "tool_name": "find_collection_documents_by_meta", "arguments": {"collection_id": "kb-1", "meta_filter": {"source": "web"}}}
```

Reference: [`system__find_collection_documents_by_meta`](reference/tools/system/find_collection_documents_by_meta)

## Gotchas
- The body is not on the entity: `system__get_document` and `system__list_documents` never return it. Read bodies with `collections__read_document` or `system__get_document_content`.
- A read miss is information: `collections__read_document` names the siblings it did find, so fix the path from the miss instead of guessing.
- A predicate that sorts or filters on a secret-bearing field is refused before it queries; keep predicates on plain fields and `meta`.
- For relevance (meaning or keywords) use the [search](knowledge/search) tools; these reads address, list and filter only.

## Related
[Search a collection](knowledge/search), [Write documents](knowledge/documents).
