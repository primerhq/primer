---
slug: rest-api-overview
title: REST API overview
section: reference
summary: Authentication, base URL, response shape, and a quick hello-world for the /v1 API.
---

## Base URL

In production the API mounts at `/v1/`. In development the same
routes are reachable via `http://localhost:8000/v1/`. The OpenAPI
schema is served at `/v1/openapi.json` and the Swagger UI at
`/v1/docs`.

```callout:tip
Generating client code? Point your OpenAPI generator at the live
`/v1/openapi.json` rather than copying a snapshot; the schema
includes every Pydantic v2 constraint and is refreshed on every
release.
```

## Hello world

Hitting the version endpoint confirms auth + reachability in one
shot.

```code-tabs:python,curl,javascript
--- python
import primer
client = primer.Client(token="...")
print(client.version())
--- curl
curl -H "Authorization: Bearer $TOKEN" \
  https://primer.example/v1/version
--- javascript
const r = await fetch("/v1/version", {
  headers: { "Authorization": `Bearer ${token}` },
});
console.log(await r.json());
```

## Auth header

Every authenticated request carries either a session cookie (set by
the console login flow) or a bearer token (issued via the API
Tokens page).

```code-tabs:python,curl
--- python
# The SDK reads $PRIMER_TOKEN if the keyword arg is omitted.
client = primer.Client()
--- curl
# Bearer tokens are validated against the same secret as session
# cookies; no separate verification endpoint is needed.
curl -H "Authorization: Bearer $PRIMER_TOKEN" \
  https://primer.example/v1/agents
```

## Errors

Every error response uses the same envelope:

```
{ "detail": { "error": "<kind>", "message": "<human>", ... } }
```

The HTTP status is the canonical signal; the `error` discriminator
gives the SDK an exception kind to raise. Status 422 maps to
Pydantic validation failures and includes the path that failed.
