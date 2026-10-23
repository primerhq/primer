---
slug: mcp-toolset-sign-in
title: MCP toolset sign-in
summary: How the shared OAuth sign-in for an MCP toolset is connected, inspected, refreshed, and disconnected.
mcp_tools: [system::get_toolset_auth_status]
---

# MCP toolset sign-in

## Overview

An HTTP or SSE MCP toolset can authenticate to its upstream server with an
OAuth browser grant or client credentials. Primer keeps one shared sign-in per
toolset. Every agent that calls that toolset uses the same upstream identity.
Only an admin can connect, reconnect, or disconnect it; any user may read its
status.

The browser flow needs a stable externally reachable callback origin. Set
`PRIMER_PUBLIC_URL` to the deployment's public HTTP or HTTPS URL when Primer
cannot safely derive it from the incoming request.

## Mental model

Treat sign-in as operator-managed infrastructure attached to the toolset, not
as a login for the person running an agent. A model cannot select another user,
token, or OAuth principal. Editing the URL or any OAuth setting is admin-only,
and an accepted change signs the toolset out so credentials cannot silently
follow a changed identity or endpoint.

The public states are:

- `connected`: a usable shared sign-in exists.
- `needs_sign_in`: an admin must connect or reconnect the toolset.
- `error`: the last OAuth operation failed; read `reason` before retrying.
- `not_applicable`: the toolset uses no OAuth sign-in.

Status responses never include an access token, refresh token, or client
secret.

## Lifecycle

An admin connects a browser-grant toolset from the console. Primer discovers
the upstream endpoints, creates a PKCE authorization request, and opens the
provider. The callback is public so the upstream redirect does not need a
Primer session cookie. Browser callbacks receive a 303 back to the console;
API clients receive JSON. Replayed, expired, denied, or edited callbacks write
no token.

A client-credentials toolset connects without a browser. Primer sends the
configured client credentials to the discovered or configured token endpoint.
For both grants, the shared access token is attached to every MCP request. A
401 causes one refresh and one retry. Refresh runs under a lease shared across
pods.

Disconnect removes the shared sign-in for every caller and ends any sign-in
still pending, so a Reconnect left open on the provider's page cannot connect the
toolset again afterwards. Deleting the toolset
does the same, whichever way it is deleted: the console or the REST API, the
`system::delete_toolset` tool, or a harness uninstall or sync that drops it. A
toolset created again under the same id needs an admin to connect it. A changed encryption key also requires every connected toolset
to reconnect. This includes setting `PRIMER_ENCRYPTION_KEY` after the deployment
previously used the session-secret fallback.

## MCP tools

`system::get_toolset_auth_status` reads one toolset's shared sign-in status. It
is read-only and requires the user role. Only an admin can take the connect or
disconnect actions in the console.

If an MCP call cannot use its sign-in, the tool result says
`the MCP server <id> needs an operator to reconnect it`. If the server cannot
list its tools, Primer leaves that toolset out of the current turn and tells the
model which toolset was unavailable.

## Workflows

### Check whether a toolset is connected

Call `system::get_toolset_auth_status`:

```json
{"toolset_id": "internal-kb"}
```

Response when it needs an admin:

```json
{
  "toolset_id": "internal-kb",
  "mode": "oauth_browser",
  "state": "needs_sign_in",
  "reason": "Not connected.",
  "expires_at": null,
  "connected_at": null,
  "refreshed_at": null,
  "scope": null,
  "redirect_uri": null
}
```

Tell the user that an admin must open the toolset in the console and choose
Connect. Do not retry the failing MCP call until the status changes.

### Confirm a successful sign-in

Call `system::get_toolset_auth_status` again after the admin completes the
browser flow:

```json
{"toolset_id": "internal-kb"}
```

An abridged connected response looks like:

```json
{
  "toolset_id": "internal-kb",
  "mode": "oauth_browser",
  "state": "connected",
  "reason": null,
  "expires_at": "2026-10-22T18:00:00Z",
  "scope": "search"
}
```

The toolset is ready for every caller once the shared status is `connected`.

## Gotchas

- A shared sign-in means every agent acts as the identity the admin connected.
  Use separate toolsets when upstream identities need different privileges.
- Changing only extra HTTP headers does not sign out an OAuth toolset. Changing
  the URL or any OAuth setting does.
- Tokens, pending PKCE verifiers, and dynamically registered client secrets are
  encrypted. A static client secret typed into the toolset remains in the
  toolset row under the normal masking and origin-binding rules.
- `PRIMER_ENCRYPTION_KEY` must be the same random value on every API and worker
  process. Generate it with `secrets.token_urlsafe(48)`. HKDF does not stretch a
  weak password.
- Without `PRIMER_ENCRYPTION_KEY`, Primer warns and derives the encryption key
  from the session secret. Changing either key material makes old token rows
  unreadable and requires reconnecting.

## Related

- [MCP exposure](mcp-exposure.md) explains Primer's inbound `/v1/mcp` server,
  which is separate from the outbound MCP toolsets covered here.
- [Auth and tokens](auth-and-tokens.md) covers authentication to Primer itself.
- [Internal tool via MCP](cookbook/internal-tool-via-mcp.md) shows how to
  register and expose an MCP-backed toolset.
