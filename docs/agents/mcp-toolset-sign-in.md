---
slug: mcp-toolset-sign-in
title: MCP toolset sign-in
summary: How each person signs in to an OAuth MCP toolset for themselves (or uses its shared client-credentials sign-in), and how that sign-in is inspected, refreshed and ended.
mcp_tools: [system::get_toolset_auth_status]
---

# MCP toolset sign-in

## Overview

An HTTP or SSE MCP toolset can authenticate to its upstream server with an
OAuth browser grant or client credentials.

- A **browser sign-in** belongs to one person. Each person who may use the
  toolset (it is shared with them, or one of its tools is public) signs in for
  themselves, and a run sends only the token of the person it acts for: never
  another person's, and never a fallback when that person has not signed in.
- A **client-credentials sign-in** is one shared sign-in per toolset, used by
  every caller. Someone with full access on the toolset connects it.

Anyone who may use the toolset may read its status: their own sign-in for a
browser grant, the shared one for client credentials.

The browser flow needs a stable externally reachable callback origin. Set
`PRIMER_PUBLIC_URL` to the deployment's public HTTP or HTTPS URL when Primer
cannot safely derive it from the incoming request.

## Mental model

A browser sign-in is the upstream account of the person a run acts for: the
person who sent the turn, an API token's owner, a trigger's owner, the parent
run's person for a sub-agent. A run that acts for nobody (an unattributed run)
gets no browser token. A model cannot select another user, token, or OAuth
principal. Editing the URL or any OAuth setting is admin-only, and an accepted
change forgets every sign-in of the toolset (every person's and the shared one)
so credentials cannot silently follow a changed identity or endpoint.

The public states, for the person asking:

- `connected`: they have a usable sign-in (the shared one for client
  credentials).
- `needs_sign_in`: they must connect it themselves (client credentials: someone
  with full access on the toolset must).
- `error`: the last OAuth operation failed; read `reason` before retrying.
- `not_applicable`: the toolset uses no OAuth sign-in.

`per_user` is `true` for a browser sign-in (this status is the caller's own) and
`false` for client credentials.

Status responses never include an access token, refresh token, or client
secret.

## Lifecycle

A person connects their own browser sign-in from the console (Toolsets, the
toolset, Connect; the panel says "Your sign-in"). Primer discovers
the upstream endpoints, creates a PKCE authorization request, and opens the
provider. The callback is public so the upstream redirect does not need a
Primer session cookie, but it must return to the browser that started the
sign-in: the start answer sets a short-lived `primer_mcp_oauth` cookie that the
callback requires. An authorize URL completed in any other browser is refused
as `invalid_state` and connects nothing. The callback writes the sign-in of the
person who started it, and only when that person still exists, is enabled, may
still use the toolset, and is not someone else signed in to Primer in that
browser; otherwise it is `invalid_state` too. Browser callbacks receive a 303 back to
the console; API clients receive JSON and must carry the cookie from the start
answer to the callback themselves. Replayed, expired, denied, or edited
callbacks write no token.

A client-credentials toolset connects without a browser (the panel says "Shared
sign-in"; only people with full access on the toolset can connect it). Primer
sends the configured client credentials to the discovered or configured token
endpoint. The call's token (its person's, or the shared one) is attached to every
MCP request. A 401 causes one refresh and one retry. If the server rejects the
refreshed token as well, that browser sign-in is signed out (`needs_sign_in`,
reason `The MCP server rejected the access token.`) and its person connects
again; a client-credentials toolset shows `error` with that reason and tries a
fresh exchange on its next call. Refresh runs under a lease on that one sign-in,
shared across pods, so two people refreshing at once never touch each other's.

Disconnect ends only the caller's own browser sign-in (other people's stay) and
any sign-in of theirs still pending, so a Reconnect left open on the provider's
page cannot connect them again afterwards; for client credentials it ends the
shared sign-in and needs full access. There is no action that ends every
person's sign-in: deleting the toolset, or changing its URL or OAuth settings,
does, whichever way it is done: the console or the REST API, the
`system::delete_toolset` tool, or a harness uninstall or sync that drops it. A
toolset created again under the same id needs everyone to connect again. A changed
encryption key also requires every connected sign-in to reconnect. This includes setting `PRIMER_ENCRYPTION_KEY` after the deployment
previously used the session-secret fallback.

## MCP tools

`system::get_toolset_auth_status` reads one toolset's sign-in status for the
person the run acts for. It is read-only and requires the user role; a run whose
person may not use the toolset gets the same `not-found` answer as for a missing
toolset. Connecting and disconnecting happen in the console.

If an MCP call cannot use a sign-in, the tool result says, for a browser sign-in,
`the MCP server <id> needs the person this run acts for to sign in (Toolsets > <id> > Connect in the console)`,
and for client credentials `the MCP server <id> needs an operator to reconnect it`.
The error is in the transcript of whoever sent the turn. If the server cannot
list its tools, Primer leaves that toolset out of the current turn and tells the
model which toolset was unavailable and why, in a fixed word (needs sign-in,
unreachable, timed out or failed).

## Workflows

### Check whether a toolset is connected

Call `system::get_toolset_auth_status`:

```json
{"toolset_id": "internal-kb"}
```

Response when the person this run acts for has not signed in:

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
  "redirect_uri": null,
  "per_user": true
}
```

Tell the user to open the toolset in the console and choose Connect: the
sign-in is theirs to make (for `per_user: false`, someone with full access on
the toolset connects it). Do not retry the failing MCP call until the status
changes.

### Confirm a successful sign-in

Call `system::get_toolset_auth_status` again after the person completes the
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
  "scope": "search",
  "per_user": true
}
```

The toolset is ready for that person's runs once their status is `connected`;
another person's runs still need that person's own sign-in.

## Gotchas

- A client-credentials sign-in is shared: every caller acts as that one client.
  Use a browser grant when each person should act as their own upstream account.
- Changing only extra HTTP headers does not sign out an OAuth toolset. Changing
  the URL or any OAuth setting does, whichever way it is changed: the console or
  the REST API, the `system::update_toolset` tool, or a harness sync.
- Primer does not call an OAuth endpoint the MCP server names on another host
  when it is on a private, loopback or link-local address (the start answers 400
  naming the URL). An operator whose authorization server is on a private
  network turns on `PRIMER_WORKSPACE_ALLOW_PRIVATE_URL_SOURCES`.
- `oauth.resource_uri` must be the MCP server's URL or a parent of it on the same
  origin (scheme, host and port), such as the origin itself; a create or update
  that names another one (another origin, or a sibling path) is refused as a
  validation error naming `config.config.oauth.resource_uri`. A sign-in also refuses a server
  whose protected-resource metadata describes another server.
- Tokens, pending PKCE verifiers, and dynamically registered client secrets are
  encrypted. A static client secret typed into the toolset remains in the
  toolset row under the normal masking and origin-binding rules.
- `PRIMER_ENCRYPTION_KEY` must be the same random value on every API and worker
  process. Generate it with `secrets.token_urlsafe(48)`. HKDF does not stretch a
  weak password.
- Without `PRIMER_ENCRYPTION_KEY`, Primer warns and derives the encryption key
  from the session secret. Changing either key material makes old token rows
  unreadable and requires reconnecting.
- A single process whose key differs from the others' (a misconfigured worker)
  fails only its own calls as `needs_sign_in` and logs an ERROR naming the
  toolset; it leaves the sign-in in place for the processes that can read it. Fix that process's `PRIMER_ENCRYPTION_KEY` rather than reconnecting.

## Related

- [MCP exposure](mcp-exposure.md) explains Primer's inbound `/v1/mcp` server,
  which is separate from the outbound MCP toolsets covered here.
- [Auth and tokens](auth-and-tokens.md) covers authentication to Primer itself.
- [Internal tool via MCP](cookbook/internal-tool-via-mcp.md) shows how to
  register and expose an MCP-backed toolset.
