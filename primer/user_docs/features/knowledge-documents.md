---
slug: knowledge-documents
title: Knowledge documents
section: features
summary: Add and manage documents within a collection in the console.
---

## Overview

Documents are the unit of content inside a collection. Each
document has an ID, a name, optional free-form metadata, and a
text payload stored in `meta.text`. After ingestion the text is
split into chunks and embedded; those chunks are what agents
retrieve during search.

The Documents page lives under Knowledge / Documents. The table
shows ID, collection, name, and metadata keys. A collection
filter dropdown lets you scope the view to one collection.

## Adding a document

1. Open Knowledge / Documents in the left navigation.
2. Use the collection filter dropdown to select the target
   collection. The **Ingest document** button only appears for
   user-managed collections; system collections are read-only.
3. Click **Ingest document**.
4. In the modal, choose how to supply the content:
   - **Upload** (default) -- drag a file onto the drop zone or
     click to pick one. Supported formats include PDF, Word,
     Markdown, plain text, and others. The file is converted to
     plain text automatically; the textarea then shows the
     converted content so you can review or edit it before saving.
   - **Paste** -- switch to paste mode and type or paste text
     directly into the textarea.
5. Set the **Name** field (the filename is pre-filled from an
   uploaded file).
6. Optionally supply extra metadata as JSON in the **Meta** field.
   This is free-form; common keys are `source`, `tags`, and
   `created_at`.
7. Click **Create**.

```callout:tip
To ingest several files at once, select multiple files in the
file picker or drag them all onto the drop zone together. The
modal switches to a batch progress view and creates one document
per file without requiring review between files. Pick the
collection before dropping the files.
```

## Viewing indexed chunks

Click any document ID in the table to open the chunks modal. It
lists every vector chunk indexed for that document, including the
chunk text and chunk ID. A document that exists but has no chunks
has not yet been vectorised; the modal says so explicitly.

## Editing a document

Click the edit icon on a document row (available for user-managed,
non-indexed rows). The same modal opens in edit mode. The
collection and ID are locked; you can update the name, text, and
metadata.

## Deleting a document

Click the trash icon on a document row, then confirm in the
dialog. This removes the document storage row. Any vector chunks
already indexed for the document are not pruned automatically;
they become orphaned until the vector store is re-indexed.

```callout:warning
Deleting a document row does not immediately remove its chunks
from the vector index. If you need the chunks gone immediately,
re-index the collection after deletion. The chunks become stale
but are not returned in searches once their source document row
is absent.
```

## Pagination

The table loads 50 rows at a time. Use the **Prev** and **Next**
buttons at the bottom to page through large collections. The
counter shows the current window (`1-50 of 312`) and updates as
you page.

## Automate this

```ref:reference/api-knowledge
Full REST reference for document ingest, retrieval, update, and
deletion endpoints.
```
