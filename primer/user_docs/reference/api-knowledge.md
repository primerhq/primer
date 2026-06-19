---
slug: api-knowledge
title: REST API - knowledge
summary: Developer reference for collections, documents, semantic search, semantic-search providers, embedding providers, and the internal-collections subsystem.
section: reference
---

Collections and documents form the knowledge surface under `/v1`. Semantic search providers (SSP) back the vector store. The internal-collections subsystem enables semantic search over agents, graphs, and collections via a CDC-driven index.

```ref:toolsets/toolsets-system
Toolsets and tools: how agents access knowledge collections via the search toolset.
```

```ref:embedding/collections-and-documents
Collections and documents: creating collections, ingesting documents, and searching from the console.
```

## Endpoints

| Method | Path | What it does |
|---|---|---|
| POST | `/v1/ssp` | Create a semantic-search provider |
| GET | `/v1/ssp` | List providers |
| GET | `/v1/ssp/{id}` | Fetch one provider |
| PUT | `/v1/ssp/{id}` | Replace a provider |
| DELETE | `/v1/ssp/{id}` | Delete (blocked if collections reference it) |
| POST | `/v1/ssp/find` | Predicate-based search |
| POST | `/v1/ssp/{id}/invalidate` | Invalidate cached connections |
| POST | `/v1/embedding_providers` | Register an embedding provider |
| GET | `/v1/embedding_providers` | List providers |
| GET | `/v1/embedding_providers/{id}` | Fetch one provider |
| PUT | `/v1/embedding_providers/{id}` | Replace a provider |
| DELETE | `/v1/embedding_providers/{id}` | Delete |
| POST | `/v1/collections` | Create a collection |
| GET | `/v1/collections` | List collections |
| GET | `/v1/collections/{id}` | Fetch one collection |
| PUT | `/v1/collections/{id}` | Replace a collection |
| DELETE | `/v1/collections/{id}` | Delete |
| POST | `/v1/collections/find` | Predicate-based search |
| POST | `/v1/collections/{id}/search` | Semantic search within a collection |
| GET | `/v1/collections/{id}/documents` | List documents (no `path`) or read one by `?path=` |
| PUT | `/v1/collections/{id}/documents?path=<p>` | Create or replace a document at a path |
| DELETE | `/v1/collections/{id}/documents?path=<p>` | Delete a document by path |
| POST | `/v1/collections/{id}/documents/move` | Move a document from one path to another |
| GET | `/v1/collections/{id}/indexed_documents` | List indexed chunks |
| POST | `/v1/documents` | Create a document (entity CRUD; `path` required) |
| GET | `/v1/documents/{id}` | Fetch one document by id |
| PUT | `/v1/documents/{id}` | Replace and re-index by id |
| DELETE | `/v1/documents/{id}` | Delete and remove chunks by id |
| POST | `/v1/documents/_convert_file` | Convert a file to indexable text |
| PUT | `/v1/internal_collections/config` | Activate the internal-collections subsystem |
| GET | `/v1/internal_collections/config` | Get current config |
| DELETE | `/v1/internal_collections/config` | Deactivate the subsystem |
| POST | `/v1/internal_collections/bootstrap` | Trigger async bootstrap (returns 202) |
| GET | `/v1/internal_collections/bootstrap/status` | Poll bootstrap status |
| POST | `/v1/agents/search` | Semantic search over agents |
| POST | `/v1/collections/search` | Semantic search over collections |
| POST | `/v1/graphs/search` | Semantic search over graphs |
| POST | `/v1/tools/search` | Semantic search over tools |

---

## POST /v1/ssp

Create a semantic-search provider. Supported backends: `pgvector`, `pgvectorscale`, `lance`. The `id` is optional: supply one to use it verbatim, or omit it and the server assigns a type-prefixed id (e.g. `semantic-search-provider-3f9a1c8d`). The id is immutable after creation.

### pgvector

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/ssp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "pg-main",
    "provider": "pgvector",
    "config": {
      "hostname": "localhost",
      "port": 5432,
      "database": "primer_prod",
      "username": "primer",
      "password": "secret",
      "db_schema": "public",
      "distance_metric": "cosine"
    }
  }'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/ssp",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "pg-main",
        "provider": "pgvector",
        "config": {
            "hostname": "localhost",
            "port": 5432,
            "database": "primer_prod",
            "username": "primer",
            "password": "secret",
            "db_schema": "public",
            "distance_metric": "cosine",
        },
    },
)
r.raise_for_status()
--- javascript
await fetch("/v1/ssp", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    id: "pg-main",
    provider: "pgvector",
    config: {
      hostname: "localhost",
      port: 5432,
      database: "primer_prod",
      username: "primer",
      password: "secret",
      db_schema: "public",
      distance_metric: "cosine",
    },
  }),
});
```

Response `201`:

```json
{
  "id": "pg-main",
  "provider": "pgvector",
  "config": { "hostname": "localhost", "port": 5432, "database": "primer_prod", "username": "primer", "db_schema": "public", "distance_metric": "cosine" }
}
```

Deleting an SSP that is still referenced by one or more collections returns `409` with `/errors/conflict`.

---

## POST /v1/embedding_providers

Register an embedding provider. Supported backends: `openai` (compatible with LM Studio, Azure, OpenRouter, etc.), `huggingface`, and `gemini`. The `id` is optional: supply one to use it verbatim, or omit it and the server assigns a type-prefixed id (e.g. `embedding-provider-3f9a1c8d`). The id is immutable after creation.

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/embedding_providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "lmstudio-embed",
    "provider": "openai",
    "models": [{ "name": "nomic-embed-text-v1.5" }],
    "config": {
      "url": "http://localhost:1234/v1",
      "api_key": "lm-studio",
      "flavor": "lmstudio"
    },
    "limits": { "max_concurrency": 2 }
  }'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/embedding_providers",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "lmstudio-embed",
        "provider": "openai",
        "models": [{"name": "nomic-embed-text-v1.5"}],
        "config": {
            "url": "http://localhost:1234/v1",
            "api_key": "lm-studio",
            "flavor": "lmstudio",
        },
        "limits": {"max_concurrency": 2},
    },
)
r.raise_for_status()
--- javascript
await fetch("/v1/embedding_providers", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    id: "lmstudio-embed",
    provider: "openai",
    models: [{ name: "nomic-embed-text-v1.5" }],
    config: { url: "http://localhost:1234/v1", api_key: "lm-studio", flavor: "lmstudio" },
    limits: { max_concurrency: 2 },
  }),
});
```

---

## POST /v1/collections

Create a collection. Both `embedder.provider_id` and `search_provider_id` must reference existing rows. Passing an unknown `search_provider_id` returns `404`. The `id` is optional: supply one to use it verbatim, or omit it and the server assigns a type-prefixed id (e.g. `collection-3f9a1c8d`). The id is immutable after creation.

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/collections \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "company-docs",
    "description": "Internal company documentation",
    "embedder": {
      "provider_id": "lmstudio-embed",
      "model": "nomic-embed-text-v1.5"
    },
    "search_provider_id": "pg-main",
    "system": false
  }'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/collections",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "company-docs",
        "description": "Internal company documentation",
        "embedder": {
            "provider_id": "lmstudio-embed",
            "model": "nomic-embed-text-v1.5",
        },
        "search_provider_id": "pg-main",
        "system": False,
    },
)
r.raise_for_status()
--- javascript
await fetch("/v1/collections", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    id: "company-docs",
    description: "Internal company documentation",
    embedder: { provider_id: "lmstudio-embed", model: "nomic-embed-text-v1.5" },
    search_provider_id: "pg-main",
    system: false,
  }),
});
```

Response `201`:

```json
{
  "id": "company-docs",
  "description": "Internal company documentation",
  "embedder": { "provider_id": "lmstudio-embed", "model": "nomic-embed-text-v1.5" },
  "search_provider_id": "pg-main",
  "system": false,
  "harness_id": null,
  "search": null
}
```

---

## POST /v1/documents

Create a document via the id-addressed entity CRUD path. A document now carries a required `path` (a POSIX-like address, unique per collection, e.g. `concepts/slo.md`); creating one without `path` returns `422`. The body to index is read from `meta.text` (or `meta.content`) on this CRUD path. Indexing is best-effort: when the embedder is reachable the document's chunks become searchable, and on embedder failure the row is persisted without chunks and indexing is retried later. The `id` is optional: supply one to use it verbatim, or omit it and the server assigns a type-prefixed id (e.g. `document-3f9a1c8d`). The id is immutable after creation. To address a document by its path instead, use the path-addressed routes under `/v1/collections/{id}/documents` (below).

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/documents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "doc-postmortem-001",
    "collection_id": "company-docs",
    "name": "Q1 post-mortem",
    "path": "postmortems/q1.md",
    "meta": { "text": "We experienced a 30-minute outage on March 3..." }
  }'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/documents",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "doc-postmortem-001",
        "collection_id": "company-docs",
        "name": "Q1 post-mortem",
        "path": "postmortems/q1.md",
        "meta": {"text": "We experienced a 30-minute outage on March 3..."},
    },
)
r.raise_for_status()
--- javascript
await fetch("/v1/documents", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    id: "doc-postmortem-001",
    collection_id: "company-docs",
    name: "Q1 post-mortem",
    path: "postmortems/q1.md",
    meta: { text: "We experienced a 30-minute outage on March 3..." },
  }),
});
```

---

## PUT /v1/documents/{id}

Replace a document by id and re-index it. The `path` field is required here too. Old chunks are removed before new chunks are written; no stale vectors linger.

```code-tabs:curl,python,javascript
--- curl
curl -s -X PUT https://primer.example/v1/documents/doc-postmortem-001 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "doc-postmortem-001",
    "collection_id": "company-docs",
    "name": "Q1 post-mortem (revised)",
    "path": "postmortems/q1.md",
    "meta": { "text": "Updated analysis: root cause was a misconfigured timeout..." }
  }'
--- python
import httpx
r = httpx.put(
    "https://primer.example/v1/documents/doc-postmortem-001",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "id": "doc-postmortem-001",
        "collection_id": "company-docs",
        "name": "Q1 post-mortem (revised)",
        "path": "postmortems/q1.md",
        "meta": {"text": "Updated analysis: root cause was a misconfigured timeout..."},
    },
)
r.raise_for_status()
--- javascript
await fetch("/v1/documents/doc-postmortem-001", {
  method: "PUT",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    id: "doc-postmortem-001",
    collection_id: "company-docs",
    name: "Q1 post-mortem (revised)",
    path: "postmortems/q1.md",
    meta: { text: "Updated analysis: root cause was a misconfigured timeout..." },
  }),
});
```

---

## Path-addressed documents

Documents are also addressable by their `path` within a collection. These routes are the agent-facing surface: the body lives in a content store keyed by `(collection_id, path)`, and the `?path=` query form (rather than a slash-bearing path segment) avoids segment-routing issues with nested paths.

### GET /v1/collections/{id}/documents

With no `path`, list documents in the collection. The listing is sourced from the content store unioned with entity rows that carry a `path`, is scoped to the collection, and is NOT offset-paginated. Bodies are not loaded; each entry carries `document_id`, `path`, and `size` (body length in characters). Pass `?prefix=<p>` to scope the listing to a path prefix.

```code-tabs:curl,python,javascript
--- curl
curl -s "https://primer.example/v1/collections/company-docs/documents" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/collections/company-docs/documents",
    headers={"Authorization": f"Bearer {token}"},
)
docs = r.json()["documents"]
--- javascript
const r = await fetch("/v1/collections/company-docs/documents", {
  headers: { "Authorization": `Bearer ${token}` },
});
const { documents } = await r.json();
```

Response `200`:

```json
{
  "documents": [
    { "document_id": "doc-postmortem-001", "path": "postmortems/q1.md", "size": 48 }
  ]
}
```

With `?path=<p>`, return the single document at that path (body + metadata), or `404`:

```code-tabs:curl,python,javascript
--- curl
curl -s "https://primer.example/v1/collections/company-docs/documents?path=postmortems/q1.md" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/collections/company-docs/documents",
    params={"path": "postmortems/q1.md"},
    headers={"Authorization": f"Bearer {token}"},
)
result = r.json()
--- javascript
const r = await fetch(
  "/v1/collections/company-docs/documents?path=postmortems/q1.md",
  { headers: { "Authorization": `Bearer ${token}` } },
);
const result = await r.json();
```

Response `200`:

```json
{
  "document": {
    "id": "doc-postmortem-001",
    "collection_id": "company-docs",
    "name": "Q1 post-mortem",
    "path": "postmortems/q1.md",
    "title": null,
    "meta": {}
  },
  "content": "We experienced a 30-minute outage on March 3..."
}
```

### PUT /v1/collections/{id}/documents (path query)

Create or replace (upsert) the document at `(collection_id, path)` named by the `?path=` query parameter. The body carries `content` (required), and optional `title` and `meta`. Returns the stored document metadata under `document`.

```code-tabs:curl,python,javascript
--- curl
curl -s -X PUT "https://primer.example/v1/collections/company-docs/documents?path=postmortems/q1.md" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"content": "We experienced a 30-minute outage on March 3...", "title": "Q1 post-mortem"}'
--- python
import httpx
r = httpx.put(
    "https://primer.example/v1/collections/company-docs/documents",
    params={"path": "postmortems/q1.md"},
    headers={"Authorization": f"Bearer {token}"},
    json={"content": "We experienced a 30-minute outage on March 3...", "title": "Q1 post-mortem"},
)
r.raise_for_status()
--- javascript
await fetch("/v1/collections/company-docs/documents?path=postmortems/q1.md", {
  method: "PUT",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ content: "We experienced a 30-minute outage on March 3...", title: "Q1 post-mortem" }),
});
```

### DELETE /v1/collections/{id}/documents (path query)

Delete the document at `(collection_id, path)` named by the `?path=` query parameter. Returns `204`; a missing path returns `404`.

```code-tabs:curl,python,javascript
--- curl
curl -s -X DELETE "https://primer.example/v1/collections/company-docs/documents?path=postmortems/q1.md" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.delete(
    "https://primer.example/v1/collections/company-docs/documents",
    params={"path": "postmortems/q1.md"},
    headers={"Authorization": f"Bearer {token}"},
)
r.raise_for_status()  # 204
--- javascript
await fetch("/v1/collections/company-docs/documents?path=postmortems/q1.md", {
  method: "DELETE",
  headers: { "Authorization": `Bearer ${token}` },
});
```

### POST /v1/collections/{id}/documents/move

Move a document from one path to another within the collection. The body uses `from` and `to`. Returns `204`; a missing source returns `404`, and an already-occupied destination returns `409`.

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST "https://primer.example/v1/collections/company-docs/documents/move" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"from": "postmortems/q1.md", "to": "archive/postmortems/q1.md"}'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/collections/company-docs/documents/move",
    headers={"Authorization": f"Bearer {token}"},
    json={"from": "postmortems/q1.md", "to": "archive/postmortems/q1.md"},
)
r.raise_for_status()  # 204
--- javascript
await fetch("/v1/collections/company-docs/documents/move", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ from: "postmortems/q1.md", to: "archive/postmortems/q1.md" }),
});
```

---

## POST /v1/collections/{id}/search

Semantic search within a single collection. Returns a ranked list of matching chunks.

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/collections/company-docs/search \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "what caused the outage", "top_k": 5}'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/collections/company-docs/search",
    headers={"Authorization": f"Bearer {token}"},
    json={"query": "what caused the outage", "top_k": 5},
)
hits = r.json()["hits"]
--- javascript
const r = await fetch("/v1/collections/company-docs/search", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ query: "what caused the outage", top_k: 5 }),
});
const { hits } = await r.json();
```

Response `200`:

```json
{
  "hits": [
    {
      "document_id": "doc-postmortem-001",
      "chunk_id": "doc-postmortem-001:0",
      "score": 0.91,
      "text": "We experienced a 30-minute outage on March 3...",
      "meta": {}
    }
  ]
}
```

`top_k` is capped at 100. An empty collection returns `{"hits": []}` rather than an error.

---

## GET /v1/collections/{id}/indexed_documents

List chunks that have been embedded into the vector store. Filter by `?document_id=<id>` to inspect one document's chunks.

```code-tabs:curl,python,javascript
--- curl
curl -s "https://primer.example/v1/collections/company-docs/indexed_documents?document_id=doc-postmortem-001" \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.get(
    "https://primer.example/v1/collections/company-docs/indexed_documents",
    params={"document_id": "doc-postmortem-001"},
    headers={"Authorization": f"Bearer {token}"},
)
result = r.json()
--- javascript
const r = await fetch(
  "/v1/collections/company-docs/indexed_documents?document_id=doc-postmortem-001",
  { headers: { "Authorization": `Bearer ${token}` } },
);
const result = await r.json();
```

Response `200`:

```json
{
  "total": 1,
  "items": [
    {
      "document_id": "doc-postmortem-001",
      "chunk_id": "doc-postmortem-001:0",
      "text": "We experienced a 30-minute outage on March 3..."
    }
  ]
}
```

---

## POST /v1/documents/_convert_file

Convert an uploaded file (markdown, PDF, etc.) to plain text suitable for ingestion. Uses multipart form data.

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/documents/_convert_file \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@note.md;type=text/markdown"
--- python
import httpx
with open("note.md", "rb") as f:
    r = httpx.post(
        "https://primer.example/v1/documents/_convert_file",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("note.md", f, "text/markdown")},
    )
text = r.json()["text"]
--- javascript
const fd = new FormData();
fd.append("file", fileBlob, "note.md");
const r = await fetch("/v1/documents/_convert_file", {
  method: "POST",
  headers: { "Authorization": `Bearer ${token}` },
  body: fd,
});
const { text } = await r.json();
```

Returns `200` with a JSON object carrying the extracted text plus upload metadata. The endpoint is non-destructive: it does not persist a Document row.

```json
{
  "filename": "note.md",
  "content_type": "text/markdown",
  "bytes_loaded": 1234,
  "text": "# Note\n\n..."
}
```

The upload is capped at 32 MB; a larger or empty file returns `400`.

---

## Internal collections (SSP-backed semantic search for agents/graphs/collections/tools)

The internal-collections subsystem indexes agents, graphs, collections, and tools into a shared vector store so they can be found by semantic search. Activation requires a configured embedding provider and SSP. The config stores `embedding_provider_id`, `embedding_model`, and `search_provider_id`. Once activated, vector-space-defining fields are frozen; changing `embedding_provider_id` after the first bootstrap returns `409` with `frozen_fields`.

### PUT /v1/internal_collections/config

Activate or update the subsystem config. Acts as an upsert before the first bootstrap. After bootstrap, changes to frozen fields return `409`.

```code-tabs:curl,python,javascript
--- curl
curl -s -X PUT https://primer.example/v1/internal_collections/config \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "embedding_provider_id": "lmstudio-embed",
    "embedding_model": "nomic-embed-text-v1.5",
    "search_provider_id": "pg-main"
  }'
--- python
import httpx
r = httpx.put(
    "https://primer.example/v1/internal_collections/config",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "embedding_provider_id": "lmstudio-embed",
        "embedding_model": "nomic-embed-text-v1.5",
        "search_provider_id": "pg-main",
    },
)
r.raise_for_status()
--- javascript
await fetch("/v1/internal_collections/config", {
  method: "PUT",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    embedding_provider_id: "lmstudio-embed",
    embedding_model: "nomic-embed-text-v1.5",
    search_provider_id: "pg-main",
  }),
});
```

### POST /v1/internal_collections/bootstrap

Trigger an async bootstrap that creates vector tables and indexes existing entities. Returns `202` immediately with the freshly created status row (its `status` is `"running"`); poll `GET /v1/internal_collections/bootstrap/status` until `status == "succeeded"`. A second call while the first is running returns `409`.

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/internal_collections/bootstrap \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx, time
r = httpx.post(
    "https://primer.example/v1/internal_collections/bootstrap",
    headers={"Authorization": f"Bearer {token}"},
)
assert r.status_code == 202

# Poll until done
while True:
    s = httpx.get(
        "https://primer.example/v1/internal_collections/bootstrap/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    if s.json()["status"] == "succeeded":
        break
    time.sleep(1)
--- javascript
const boot = await fetch("/v1/internal_collections/bootstrap", {
  method: "POST",
  headers: { "Authorization": `Bearer ${token}` },
});
// boot.status === 202; poll status
```

Bootstrap `202` response body (the status row at launch):

```json
{
  "status": "running",
  "phase": null,
  "phase_done": 0,
  "phase_total": null,
  "counts": { "agents": 0, "graphs": 0, "collections": 0, "tools": 0 },
  "started_at": "2026-06-08T12:00:00Z",
  "finished_at": null,
  "error": null
}
```

Status poll response `200` (same shape; `status` is one of `idle`, `running`, `succeeded`, `failed`):

```json
{
  "status": "succeeded",
  "phase": null,
  "phase_done": 0,
  "phase_total": null,
  "counts": { "agents": 4, "graphs": 2, "collections": 1, "tools": 5 },
  "started_at": "2026-06-08T12:00:00Z",
  "finished_at": "2026-06-08T12:00:12Z",
  "error": null
}
```

### DELETE /v1/internal_collections/config

Deactivate the subsystem. After deletion, search routes (`/v1/agents/search`, etc.) return `503` with `/errors/subsystem-inactive`.

```code-tabs:curl,python,javascript
--- curl
curl -s -X DELETE https://primer.example/v1/internal_collections/config \
  -H "Authorization: Bearer $TOKEN"
--- python
import httpx
r = httpx.delete(
    "https://primer.example/v1/internal_collections/config",
    headers={"Authorization": f"Bearer {token}"},
)
r.raise_for_status()  # 204
--- javascript
await fetch("/v1/internal_collections/config", {
  method: "DELETE",
  headers: { "Authorization": `Bearer ${token}` },
});
```

---

## POST /v1/agents/search (and /collections/search, /graphs/search, /tools/search)

Semantic search over the internal collections. Requires a successful bootstrap. Returns the same `SearchResponse` shape for all four routes.

```code-tabs:curl,python,javascript
--- curl
curl -s -X POST https://primer.example/v1/agents/search \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "data science automation", "top_k": 5}'
--- python
import httpx
r = httpx.post(
    "https://primer.example/v1/agents/search",
    headers={"Authorization": f"Bearer {token}"},
    json={"query": "data science automation", "top_k": 5},
)
hits = r.json()["hits"]
--- javascript
const r = await fetch("/v1/agents/search", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ query: "data science automation", top_k: 5 }),
});
const { hits } = await r.json();
```

Response `200`:

```json
{
  "hits": [
    {
      "document_id": "agent-data-pipeline",
      "chunk_id": "agent-data-pipeline:0",
      "score": 0.88,
      "text": "Automates data pipelines and runs ML experiments",
      "meta": null
    }
  ]
}
```

`query` must be non-empty (min length 1). `top_k` defaults to 10, max 100. If the subsystem is not bootstrapped, the route returns `503`:

```json
{
  "type": "/errors/subsystem-inactive",
  "title": "Subsystem Inactive",
  "status": 503,
  "detail": "internal collections subsystem is not active"
}
```

---

## Error responses

All error responses follow RFC 7807:

```json
{
  "type": "/errors/not-found",
  "title": "Not Found",
  "status": 404,
  "detail": "collection company-docs not found",
  "instance": "/v1/collections/company-docs"
}
```

Common status codes: `404` resource not found, `409` conflict (SSP in use, frozen field, or duplicate), `422` validation error, `503` subsystem inactive.
