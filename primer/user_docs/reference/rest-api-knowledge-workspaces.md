---
slug: rest-api-knowledge-workspaces
title: REST API - knowledge, workspaces, SSP
section: reference
summary: Enumerated endpoints for the knowledge + workspaces + semantic-search surface.
---

## Knowledge collections

| Method | Path | Body |
|---|---|---|
| GET | `/v1/knowledge/collections` | - |
| POST | `/v1/knowledge/collections` | `{name, description, ssp_id}` |
| GET | `/v1/knowledge/collections/{id}` | - |
| PATCH | `/v1/knowledge/collections/{id}` | partial |
| DELETE | `/v1/knowledge/collections/{id}` | - |
| POST | `/v1/knowledge/collections/{id}/reindex` | re-embed all docs |

## Knowledge documents

| Method | Path | Body |
|---|---|---|
| GET | `/v1/knowledge/collections/{id}/documents` | - |
| POST | `/v1/knowledge/collections/{id}/documents` | multipart `file` + `metadata` |
| GET | `/v1/knowledge/documents/{id}` | - |
| GET | `/v1/knowledge/documents/{id}/content` | raw bytes |
| PATCH | `/v1/knowledge/documents/{id}` | partial (re-embed if content changes) |
| DELETE | `/v1/knowledge/documents/{id}` | - |
| POST | `/v1/knowledge/documents/find_by_meta` | metadata-filtered search |

```code-tabs:python,curl,javascript
--- python
doc = client.knowledge.put_document(
    collection_id="company-docs",
    file=open("post-mortem.md", "rb"),
    metadata={"source": "internal"},
)
--- curl
curl -X POST https://primer.example/v1/knowledge/collections/company-docs/documents \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@post-mortem.md" \
  -F 'metadata={"source":"internal"}'
--- javascript
const fd = new FormData();
fd.append("file", file);
fd.append("metadata", JSON.stringify({ source: "internal" }));
await fetch("/v1/knowledge/collections/company-docs/documents", {
  method: "POST",
  headers: { "Authorization": `Bearer ${token}` },
  body: fd,
});
```

## Workspaces

| Method | Path | Body |
|---|---|---|
| GET | `/v1/workspaces/providers` | - |
| POST | `/v1/workspaces/providers` | `{kind, name, config}` |
| GET | `/v1/workspaces/templates` | - |
| POST | `/v1/workspaces/templates` | `{provider_id, name, base_image, ttl_minutes, env, post_create_commands}` |
| GET | `/v1/workspaces` | List instances |
| POST | `/v1/workspaces` | `{template_id}` |
| GET | `/v1/workspaces/{id}/log` | `?lines=N` |
| GET | `/v1/workspaces/{id}/files/{path}` | read file |
| PUT | `/v1/workspaces/{id}/files/{path}` | write file |
| GET (ws) | `/v1/workspaces/{id}/watch` | stream file events |

## Semantic search providers

| Method | Path | Body |
|---|---|---|
| GET | `/v1/ssp/providers` | - |
| POST | `/v1/ssp/providers` | `{id, kind, model, ...kind-specific creds}` |
| GET | `/v1/ssp/active` | - |
| PUT | `/v1/ssp/active` | `{provider_id}` |

```callout:warning
A reindex on a collection with 10k+ documents is expensive and
slow. The endpoint returns 202 with a background job id; poll
`/v1/jobs/{id}` for progress.
```
