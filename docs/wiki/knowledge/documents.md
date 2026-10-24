---
title: "Write documents"
summary: "Create, replace, move and delete documents by path, with the collections tools or the system document tools."
order: 2
tools: [collections__create_document, collections__update_document, collections__move_document, collections__delete_document, system__put_document, system__create_document, system__update_document, system__delete_document, system__move_document]
related: [knowledge/navigation, knowledge/collections]
---
## What it is
A document is a node in a collection's tree, addressed by its slug path within the collection. Two tool families write documents: the `collections` tools, the path-addressed way in, and the older `system` document tools, the row-level way. Create, replace, move and delete nodes with either; read them back at [navigate and read documents](knowledge/navigation).

## Concepts
- The `collections` tools are path-addressed. A path is a chain of slugs (`guides/intro`); slugs match `[a-z0-9-]`. The parent must already exist, so a new branch starts by creating its first node. `collections__update_document` changes the body or the title in place, `collections__move_document` moves a node and its subtree to a new parent or slug (document ids survive the move), and `collections__delete_document` takes `recursive` to remove a whole branch.
- The `system` tools are the older, row-level family. `system__put_document` upserts a body at a path; `system__create_document` makes an empty node (put the text in afterwards with `put_document`, or create it with its body in one call using `put_document` alone); `system__update_document` changes only `title` and `meta` by id; `system__move_document` takes `from` and `to` paths; `system__delete_document` removes one node by id.
- The body lives in the content store, keyed by the document, not in `meta`. Do not store content in `meta`, and do not expect a read to find it there. A document with no content row genuinely has no body.
- A write indexes the document when the collection has search on: best effort, after the body is stored. A store failure does not fail the write; the document stays stored but unsearchable until it is indexed. A dimension mismatch is the exception, and it is refused (see [collections](knowledge/collections)).
- A harness-managed document (its `harness_id` set) and any document of a system collection cannot be written: the writes answer `type=conflict` (the harness manages it; change it through the harness) or `type=forbidden` (the collection is read-only).

## How to
Write a note under a new branch:
1. Create the branch node: `collections__create_document` at the root (or under an existing node) with a short body.
2. Create the page under it: `collections__create_document` with the branch's path as `parent`.
3. Revise it in place: `collections__update_document` with the page's path.
4. Relocate it: `collections__move_document` to a new parent or slug.

## Tool calls
### Create a document under a parent
`collections__create_document` adds a page to a collection; the parent must exist and the slug must match `[a-z0-9-]`.

```json
{"toolset_id": "collections", "tool_name": "create_document", "arguments": {"collection": "kb", "parent": "guides", "slug": "intro", "body": "# Intro"}}
```

Reference: [`collections__create_document`](reference/tools/collections/create_document)

### Replace a document's body or title
`collections__update_document` revises a page in place; omit `body` to retitle without touching the text.

```json
{"toolset_id": "collections", "tool_name": "update_document", "arguments": {"collection": "kb", "path": "guides/intro", "body": "# New intro"}}
```

Reference: [`collections__update_document`](reference/tools/collections/update_document)

### Move a document and its subtree
`collections__move_document` reorganises the tree; moving a node into its own subtree is rejected.

```json
{"toolset_id": "collections", "tool_name": "move_document", "arguments": {"collection": "kb", "path": "intro", "new_parent": "guides"}}
```

Reference: [`collections__move_document`](reference/tools/collections/move_document)

### Delete a document
`collections__delete_document` removes a page; pass `recursive` for a whole branch, or a node with children is a conflict.

```json
{"toolset_id": "collections", "tool_name": "delete_document", "arguments": {"collection": "kb", "path": "old", "recursive": true}}
```

Reference: [`collections__delete_document`](reference/tools/collections/delete_document)

### Upsert a body at a path
`system__put_document` stores raw text at a path in one call; repeated puts on the same path update the same entity.

```json
{"toolset_id": "system", "tool_name": "put_document", "arguments": {"collection_id": "kb-1", "path": "onboarding.md", "content": "Welcome to the team.", "title": "Onboarding Guide"}}
```

Reference: [`system__put_document`](reference/tools/system/put_document)

### Create an empty node
`system__create_document` makes a readable, listable node with no body; any `id` you send is ignored, so read the id from the result.

```json
{"toolset_id": "system", "tool_name": "create_document", "arguments": {"entity": {"collection_id": "kb-1", "slug": "notes", "path": "notes", "title": "Notes"}}}
```

Reference: [`system__create_document`](reference/tools/system/create_document)

### Change a document's title and meta
`system__update_document` relabels a document by id; send the full entity you read with `system__get_document`, and expect `type=bad-request` if it moves the node.

```json
{"toolset_id": "system", "tool_name": "update_document", "arguments": {"id": "document-0123456789ab", "entity": {"id": "document-0123456789ab", "collection_id": "kb-1", "slug": "notes", "path": "notes", "title": "Meeting notes", "meta": {"owner": "ops"}}}}
```

Reference: [`system__update_document`](reference/tools/system/update_document)

### Delete a document by id
`system__delete_document` removes the entity, its body and its search index entries; a document with children is refused.

```json
{"toolset_id": "system", "tool_name": "delete_document", "arguments": {"id": "document-0123456789ab"}}
```

Reference: [`system__delete_document`](reference/tools/system/delete_document)

### Move a document between paths
`system__move_document` relocates one path to another within the collection; the same entity keeps its id and body.

```json
{"toolset_id": "system", "tool_name": "move_document", "arguments": {"collection_id": "kb-1", "from": "draft.md", "to": "concepts/final.md"}}
```

Reference: [`system__move_document`](reference/tools/system/move_document)

## Gotchas
- `system__create_document` ignores an `id` you send; the service assigns it. `path` must equal the parent's path plus the slug (or just the slug at the root), and `harness_id` cannot be set through the tool.
- Deleting a node with children needs `recursive` (the `collections` tool) or the children deleted first (the `system` tool refuses the node).
- Writing to an existing path replaces that document: the path is the document's identity and a natural idempotency key for `system__put_document`.
- A harness-managed document answers `type=conflict` on every write, and a system collection answers `type=forbidden`; there is no way around it but the harness or the platform.
- The body never goes in `meta`; `meta` is for filtering and display only.

## Related
[Navigate and read documents](knowledge/navigation), [Collections](knowledge/collections).
