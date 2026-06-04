---
slug: semantic-search
title: Semantic search providers
section: features
summary: Configure and bind the embedding models that index every knowledge collection and internal collection.
---

## What an SSP is

A semantic search provider (SSP) is primer's binding to one
embedding model. Each provider row carries the model identifier,
the vector dimension, and the credentials needed to call the
embedding API. Collections bind to one SSP at create time; the
SSP decides what model embeds every document landing in that
collection.

## The provider list

The console Semantic Search Providers page lists every configured
SSP. Exactly one provider is marked active; that one is the
default for newly-created collections.

```mockup:ssp-list
{ "activeId": "voyage-3-large" }
```

Adding a provider is a one-shot form (kind, model, credentials);
the row appears immediately and accepts collection bindings.

## Adding a provider

The REST equivalent:

```code-tabs:python,curl
--- python
ssp = client.semantic_search.create_provider(
    id="voyage-3-large",
    kind="voyage",
    model="voyage-3-large",
    api_key="<token>",
)
client.semantic_search.set_active_provider(ssp.id)
--- curl
curl -X POST https://primer.example/v1/ssp/providers \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "id":"voyage-3-large",
    "kind":"voyage",
    "model":"voyage-3-large",
    "api_key":"<token>"
  }'

curl -X PUT https://primer.example/v1/ssp/active \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"provider_id":"voyage-3-large"}'
```

## Active vs configured

Configured providers can serve specific collections via explicit
binding. The active provider is the one a freshly-created
collection picks up if no explicit binding is named. Flipping
the active provider does NOT migrate existing collections; they
continue to use the SSP they were created with.

```callout:info
The active provider is per-instance, not per-collection. There
is no concept of 'default for this team'. If your operators need
different defaults, give them separate primer instances or rely
on explicit SSP id at collection-create time.
```

## Local vs remote

Three SSP kinds ship in primer:

| Kind | Where it runs | Best for |
|---|---|---|
| `voyage` | Remote API call | Production quality, no GPU |
| `openai` | Remote API call | Existing OpenAI account |
| `huggingface` | In-process (CPU or GPU) | Air-gapped or cost-sensitive |

The huggingface kind loads the model into the primer process at
SSP create time; the first embedding call is slow as the model
warms.
