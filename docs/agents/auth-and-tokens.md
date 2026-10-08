---
slug: auth-and-tokens
title: Auth and API tokens
summary: How agents authenticate to primer - bearer tokens, scopes, and the mcp scope.
related: [mcp-exposure]
mcp_tools: []
---

# Auth and API tokens

## Overview

Primer accepts two authentication mechanisms. **Cookie sessions** are
what the operator console uses - they're established by an interactive
login and carry the full set of privileges. **Bearer tokens** (API
tokens) are what programmatic clients use, including any agent
connecting to primer's MCP endpoint. Cookies and tokens reach the same
auth middleware, populate the same `scope.state.principal`, and from
the perspective of downstream code, look identical apart from one
thing: bearer tokens carry an explicit **scope list**, while cookies
implicitly hold every scope.

A scope is a named permission. The vocabulary in v1 is small - the only
defined scope is `mcp`, which gates the `/v1/mcp` MCP endpoint. Adding
new scopes is forward-compatible: the auth middleware accepts unknown
scope strings (logged at WARN) so a future API can introduce its own
scope without breaking existing clients.

An agent that wants to call primer's MCP endpoint needs two things: an
operator has minted an API token for them and granted it the `mcp`
scope, and an operator has enabled MCP exposure on the singleton
config and added at least one tool to the allowlist. The first thing
is per-client; the second is per-deployment.

## Mental model

An **API token** is a row in the `ApiToken` storage table. It carries
a SHA-256 hash of the token plaintext, a short prefix (for display in
listings - never the full secret), the user who owns it, an optional
expiry, an immutable scopes list, and a soft-delete `revoked_at`. The
plaintext leaves the server **exactly once**, in the response to the
POST that created the token. Listing, GETting, or otherwise touching
the row afterward returns the hash + prefix only. If a user loses a
token, the recovery procedure is to mint a new one and revoke the
old; there's no recovery from the hash.

Token names are unique per user but not unique globally - two
different users can have a token named "claude-desktop". The
uniqueness check is scoped to the owning `user_id`.

On every request, the auth middleware tries the session cookie first,
then falls back to a `Authorization: Bearer <token>` header. If the
bearer path matches, the middleware loads the `ApiToken` row, checks
expiry, checks `revoked_at`, sets `scope.state.api_token` to the row,
and proceeds. A `last_used_at` write is fire-and-forget - it doesn't
block the request, and a failed write only logs.

Session cookies can be revoked. Each user carries a session epoch
(`User.session_epoch`), and every cookie is signed with the epoch it
was minted under; the middleware rejects a cookie whose epoch is not
the user's current one. A password change (`POST
/v1/auth/change-password`, which hands the caller a fresh cookie), an
admin password reset, and `POST /v1/auth/logout-all` ("Sign out
everywhere" in the profile menu) bump the epoch, so every cookie of
that account stops working on its next request. A plain `POST
/v1/auth/logout` only clears the caller's own cookie. None of this
touches bearer tokens: revoke those through the API-token routes.
A connection that is already open (the workspace tap's SSE, a terminal
WebSocket, an MCP GET stream) is re-checked against its account every
`auth.revalidate_interval_s` (5 s by default, 0 turns it off) and cut when
the account is disabled or deleted, its epoch moved (cookie sessions), its
role was weakened (a promotion cuts nothing), its cookie's own lifetime
(`session_ttl_days` from when it was signed) ran out, or its API token was
revoked or expired. A WebSocket closes with code 4401 `auth_revoked`; an HTTP
stream is completed, and the reconnect meets the ordinary 401. An MCP tool
call that was already started by a POST keeps running to its end (only the
response is cut). A storage outage cuts WebSockets and GET streams after
three failed checks and leaves a POST, PUT, PATCH or DELETE in flight alone.

Route-level scope enforcement is via the `require_scope("mcp")`
dependency. Cookie sessions bypass scope checks (they have implicit
all-scopes); bearer tokens must have the named scope in their `scopes`
list. This is why the MCP endpoint accepts cookie auth from the
operator console without an explicit token grant.

### WebSocket authentication

The live session stream is not a WebSocket: it is the workspace tap, a read-only SSE
stream (`GET /v1/workspaces/{wid}/tap`), authenticated by the same middleware as every
HTTP route (no valid credential is an HTTP 401). The one WebSocket left in the API is the
workspace terminal (`/v1/workspaces/{wid}/terminal`). Its handshakes carry auth the same way HTTP requests do - the
signed session cookie, or an `Authorization: Bearer <token>` header -
and the same middleware populates the connection's principal before
the handler runs. A handshake that carries no valid cookie or bearer
is **rejected**: the server accepts the upgrade and immediately closes
the socket with WebSocket close code **4401** (`auth_required`). This
is the WebSocket analogue of an HTTP 401 (the standard close-code
convention adds 4000 to the HTTP status). Clients should treat a 4401
close as "authenticate, then reconnect", not as a transient blip to
retry blindly (an OPEN socket whose account stopped being valid is closed with
the same code, reason `auth_revoked`). When auth is disabled on the deployment, the handshake
connects without credentials (a synthetic operator identity is
injected), exactly like the HTTP routes.

## Lifecycle and states

An `ApiToken` is in one of three observable states:

- **active** - `revoked_at IS NULL` and (`expires_at IS NULL` OR
  `expires_at > now()`). The token can be used to authenticate.
- **expired** - `expires_at <= now()`. The middleware rejects with
  401 `token_expired`. The row stays in storage for audit; an operator
  can see it in the list view.
- **revoked** - `revoked_at IS NOT NULL`. The middleware rejects with
  401 `token_revoked`. Revocation is permanent; the row is never
  un-revoked. Audit retention keeps the row indefinitely.

Transitions:

- `active → expired` happens implicitly as wall-clock crosses
  `expires_at`. No write is needed.
- `active → revoked` happens when an operator deletes the token (a
  soft-delete that sets `revoked_at`). The DELETE returns 204 and the
  next request from that token gets 401.

There is no `revoked → active` transition. Mint a new token instead.

## MCP tools

Auth doesn't surface as an MCP tool - agents authenticate before they
can call any tool, and they don't need to manage their own tokens
from inside an agent session. Token CRUD is operator-facing only
(REST routes under `/v1/auth/tokens` + the console UI), not
agent-facing.

If an operator does need a programmatic mint flow, the REST endpoint
takes a JSON body and returns the plaintext token in the response.
Example:

```bash
curl -X POST https://primer.example/v1/auth/tokens \
  -H "Cookie: session=..." \
  -H "Content-Type: application/json" \
  -d '{"name": "claude-desktop", "scopes": ["mcp"], "expires_at": null}'
```

The response includes `{"id": "...", "token": "primer_pk_XXXXXX...", ...}`.
After the response is consumed, the plaintext is unrecoverable.

## Workflows

### Workflow 1 - operator mints an MCP token for an agent

**Goal.** An operator wants Claude Desktop to drive primer via MCP.
They mint a token, hand the plaintext to the desktop client config,
and never see it again.

1. Operator opens the console, navigates to the API Tokens page, clicks
   "New token", names it `claude-desktop`, picks the `mcp` scope, and
   submits. The page shows the plaintext token in a one-time modal
   with a "Copy" button.
2. Operator pastes the token into the desktop's MCP server config
   (`{"url": "https://primer.example/v1/mcp", "headers": {"Authorization": "Bearer <token>"}}`)
   and closes the modal.
3. Desktop connects to `/v1/mcp`. The auth middleware sees the bearer
   header, resolves the token, sets the scope on the request, and the
   MCP handshake proceeds.
4. If the operator later revokes the token from the console, the next
   tool call from the desktop fails with 401 `token_revoked`. The
   desktop reconnects (gets the same 401), then prompts the user for a
   new token.

### Workflow 2 - agent encounters 401 mid-session

**Goal.** An agent's token was revoked mid-session. Recover cleanly.

The agent sees the 401 on its next tool call. The MCP transport
surfaces this as a `tools/call` error result. Concrete response shape:

```json
{
  "is_error": true,
  "output": "{\"type\":\"auth-error\",\"message\":\"token_revoked\"}"
}
```

The agent should:
1. Stop attempting further tool calls (subsequent ones will also 401).
2. Surface a clear status to the user - "I lost my primer access mid-
   session; the operator may have revoked the token."
3. Wait for the user to provide a replacement token (or reconnect with
   a fresh one if the MCP client supports it).

The agent should **not** invent retry logic - there's nothing to retry,
and aggressive reconnection just generates audit-log noise on the
server side.

## Gotchas

- **Plaintext is one-shot.** Mint, copy, configure - in that order. If
  you close the modal without copying the token, you've lost it. The
  hash on the server can't be reversed.
- **Cookie auth bypasses scope checks** for routes guarded by
  `require_scope(...)`. This is intentional - the console UI is
  operator-trusted - but means tests that assert "X cannot reach
  route Y without scope Z" must use bearer auth, not cookies.
- **`scope.state.api_token` is `None` under cookie auth**, set only
  under bearer auth. Code that wants to detect "is this a programmatic
  caller" checks `api_token is not None`, not `principal`.
- **Token name uniqueness is per-user**, not global. Two different
  users can mint tokens with identical names; the constraint applies
  only within one user's token set.
- **Expiry is checked on every request**, not cached. There's no race
  where a recently-expired token is briefly accepted; the wall-clock
  comparison runs at request time.
- **Revocation does not invalidate in-flight MCP sessions** at the
  transport layer - it invalidates the next request from that token.
  If the agent is mid-streaming-tool-result, the current response
  completes; the *next* call gets 401.
- **WebSocket auth failures close with 4401, not HTTP 401.** A bad or
  missing cookie/bearer on a terminal WS handshake does not get a
  401 response body - the socket is accepted then closed with code
  4401. Distinguish this in client code from the other documented WS
  close codes (4403 forbidden role, 4404 workspace not found). The
  session stream is SSE and answers an HTTP 401 instead.
- **Most of `/v1/workers` is a public probe surface, but the mutations
  are not.** `GET /v1/workers` stays reachable without auth so
  liveness/readiness probes work pre-login; `POST /v1/workers/{id}/drain`,
  `POST /v1/workers/purge_dead` and `DELETE /v1/workers/{id}` require the
  admin role (401 without auth, 403 below admin).
- **Password sign-in is throttled; bearer tokens are not.** After five
  attempts in a row for one username from one client address,
  `POST /v1/auth/login` answers `429` (`extensions.error` is
  `too_many_attempts`) with a `Retry-After` header, and keeps doing so
  for 2, 4, 8, ... seconds (at most 15 minutes) even if the password is
  right. Wait the `Retry-After` seconds; hammering does not lengthen the
  wait but gets nowhere. An automation that signs in with a cookie
  should use an API token instead. The counters live in one API process.
  The client address is the peer address the server sees: a deployment
  behind a reverse proxy must set `FORWARDED_ALLOW_IPS` to the proxy
  (uvicorn then reads the real client from the forwarded header), or every
  client shares the proxy's address and the throttle key degrades to the
  username alone.
- **Approval-required tools are not callable over MCP.** Even with the
  `mcp` scope and the tool allowlisted, a tool whose effective approval
  policy is `required` is refused at `tools/call` - MCP has no surface
  to collect the approval. See [mcp-exposure](mcp-exposure.md).

## Related

- [mcp-exposure](mcp-exposure.md) - what scopes gate, plus the
  per-tool allowlist that also gates MCP access.
