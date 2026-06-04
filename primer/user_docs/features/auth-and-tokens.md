---
slug: auth-and-tokens
title: Auth and API tokens
section: features
summary: The session-cookie + bearer-token model, the API tokens page, scope picking, rotation.
---

## Two transports, one secret

Primer's auth model is single-user in v1. Every authenticated
request carries either:

- A session cookie set by the console login flow.
- A bearer token minted on the API Tokens page.

Both are validated against the same HMAC secret
(`PRIMER_SESSION_SECRET`). Rotating the secret rotates every
session and every token; rotating just one token leaves the
secret intact.

## The topbar shows the auth state

A successful login produces the username initial in the
operator's avatar bubble in the top right:

```mockup:topbar
{ "workers": "8/8", "showThemeToggle": true, "username": "alex" }
```

A logged-out client sees the login page; bearer-token clients
that miss auth get a clean 401.

## Minting a token

The API Tokens page is the only place a bearer token is created.
Pick a label, an expiry, and a scope set:

```mockup:api-token-create
{ "phase": "form" }
```

Save. The token value is shown exactly once:

```mockup:api-token-create
{ "phase": "reveal" }
```

Copy it; primer never shows the value again. The token id (the
short row id on the list) is retrievable; the secret is not.

## Three transports for using it

```code-tabs:python,curl,javascript
--- python
import primer
client = primer.Client(token="primer_at_...")
# The SDK reads $PRIMER_TOKEN if the keyword arg is omitted.
--- curl
curl -H "Authorization: Bearer $PRIMER_TOKEN" \
  https://primer.example/v1/agents
--- javascript
fetch("/v1/agents", {
  headers: { "Authorization": `Bearer ${token}` },
});
```

## Scopes

Each token carries a scope set. Scopes are coarse
(noun-level) and additive: `agents:read`, `agents:write`,
`sessions:read`, etc. A call to a scoped endpoint returns 403
if the token does not carry the matching scope.

```callout:warning
Mint short-lived tokens for short-lived purposes. A 90-day token
in a CI variable is harder to rotate than a 7-day token that
gets refreshed by the CI's secret manager. The pain of frequent
rotation is real but smaller than the pain of a leak you cannot
quickly remediate.
```

## Rotation

Two rotation flows:

- **Revoke individual token**: hit the trash icon on the API
  Tokens row. Future calls with that token return 401.
- **Rotate the session secret**: set a new
  `PRIMER_SESSION_SECRET` and restart. Every session cookie and
  bearer token in existence stops working; operators log in
  again, tokens are reminted.

The session-secret rotation is the nuclear option; reserve it
for actual compromise.
