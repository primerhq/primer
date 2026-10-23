# MCP OAuth

## 1. Purpose

MCP OAuth lets an HTTP or SSE MCP toolset authenticate to its upstream server.
Primer keeps one shared sign-in per toolset. Every agent and API caller that uses
the toolset acts through that same upstream identity, so only an admin can
connect, reconnect, or disconnect it. The provider and agent integration is
described in [provider-pattern](../architecture/provider-pattern.md) and
[agents](agents.md).

## 2. Conceptual model

The toolset row describes how to authenticate. A browser grant uses
`authorization_code`; a machine-to-machine grant uses `client_credentials`.
The sign-in state is separate from the toolset row and is shared by every API
and worker process. Its public status has four values:

- `connected`: a usable shared token exists.
- `needs_sign_in`: no usable token exists and an admin must connect again.
- `error`: the last OAuth operation failed and the reason is safe to show.
- `not_applicable`: the toolset does not use OAuth.

A caller identity never selects an OAuth token. The `principal` passed through a
toolset provider remains dispatch context only.

## 3. Architecture patterns implemented

The subsystem follows the [provider pattern](../architecture/provider-pattern.md):
`McpToolsetProvider` asks one `ToolsetOAuthService` for the shared token and
`McpBearerAuth` attaches it to upstream requests. It uses the
[storage](../architecture/storage.md) abstraction for tokens, dynamically
registered clients, pending browser grants, and refresh leases. The
[REST API](../architecture/rest-api.md) exposes RFC 7807 errors and role-gated
routes. Cross-process refresh uses a storage lease plus compare-and-set writes so
one pod refreshes while its peers wait for the winning token row.

## 4. Code layout

- `primer/toolset/oauth/discovery.py` validates protected-resource and
  authorization-server metadata.
- `primer/toolset/oauth/client.py` performs authorization-code, client-credentials,
  refresh, revocation, and dynamic-registration requests.
- `primer/toolset/oauth/store.py` encrypts and persists OAuth state.
- `primer/toolset/oauth/service.py` owns the shared sign-in lifecycle and status.
- `primer/toolset/oauth/bearer.py` attaches bearer tokens and retries once after a
  401.
- `primer/toolset/mcp.py` opens the upstream MCP transport only after the OAuth
  preflight succeeds.
- `primer/api/routers/mcp_oauth.py` exposes the sign-in routes.
- `primer/model/mcp_oauth.py` defines stored rows, public status, and the typed
  sign-in-required error.

## 5. Data model

`McpOAuthTokenSet` is keyed by toolset id and stores encrypted access and refresh
tokens plus expiry and scope metadata. `McpOAuthClient` stores dynamic client
registration metadata; only a client secret issued by dynamic registration is
encrypted in this row. A static client secret typed into the toolset configuration
stays in the toolset row and follows its existing masking and origin-binding rules.
`McpOAuthPendingSignIn` is keyed by a hash of the browser state value and stores an
encrypted PKCE verifier until the callback consumes it. The public
`ToolsetAuthStatus` never includes a token or client secret.

## 6. Lifecycle

For a browser grant, an admin starts sign-in, Primer discovers the upstream
metadata, creates or reuses a client, records one short-lived state and PKCE
verifier, and returns the authorization URL. `PRIMER_PUBLIC_URL` supplies the
stable callback origin in deployments where request-derived URLs are not safe.
The start answer also sets the `primer_mcp_oauth` cookie: a fresh nonce,
HttpOnly, `SameSite=Lax`, scoped to the callback path, living as long as the
pending sign-in (600 s), and `Secure` when the callback is https. The pending row
keeps only the nonce's SHA-256. The public callback takes the state back only with
that cookie, then exchanges the code, stores the shared token, clears the cookie
and returns to the console. A callback without the cookie, or with another
browser's, is `invalid_state` and does not spend the sign-in, so an authorize URL
that leaks (a browser history, a shared link, a provider-side log) and is
completed with another account at the provider connects nothing (audit G4-01). A
replay, late callback, denial, or edited state writes no token either.

In the console the cookie arrives because the flow stays in one browser: the
Connect button's `fetch` of the start route stores the cookie for the console's
origin, the popup it opens goes to the provider, and the provider's redirect
back to the callback is a top-level GET navigation, which carries a `Lax` cookie
across sites. The console must therefore be opened on the host the callback
uses: with `PRIMER_PUBLIC_URL` set, open the console at that URL, or the cookie
is set for one host and the callback lands on another.

For client credentials, an admin starts sign-in and Primer obtains the shared token
immediately. During normal MCP traffic, `McpBearerAuth` sends that token on every
request. A 401 triggers at most one refresh and one retry. When the server
rejects the refreshed token too, the toolset is signed out with the reason
`The MCP server rejected the access token.` (`ToolsetOAuthService.sign_out_rejected`,
fenced on the version that holds that token, so a reconnect or a newer token
stored meanwhile is left alone): its status stops saying `connected`, and later
calls fail at once instead of spending a refresh token each. Refresh is coordinated
under a lease across pods, and a compare-and-set write prevents a late refresher
from replacing newer credentials. Disconnect first ends the toolset's pending
sign-ins (a Reconnect left open on the provider's page would otherwise connect the
toolset again when completed), attempts revocation and then removes the local
sign-in. Every path that deletes a toolset forgets its sign-in (its
tokens, its registered client and its pending sign-ins): the REST delete and the
system `delete_toolset` tool through `ToolsetOAuthService.forget`, and a harness
uninstall or a sync that drops the toolset through the store's keyless
`forget_sign_in`, which the service's `forget` is built on. A toolset created
again under the same id is never connected on the deleted one's sign-in.

Changing an OAuth toolset's URL or any OAuth setting requires an admin. An accepted
change signs the toolset out, even when the typed secret makes the change safe from
the separate masked-secret repoint guard. An unchanged save and a refused save keep
the existing sign-in.

## 7. Persistence

Tokens, pending PKCE verifiers, and dynamically registered client secrets are
encrypted before storage. Set `PRIMER_ENCRYPTION_KEY` to the same random value on
every API and worker process. Generate suitable material with
`secrets.token_urlsafe(48)`; the HKDF step derives a key but does not stretch a weak
password.

When `PRIMER_ENCRYPTION_KEY` is unset, Primer logs a warning and derives the key
from the persisted session secret. Changing the active key makes existing rows
unreadable, so every connected toolset must reconnect. Setting
`PRIMER_ENCRYPTION_KEY` after connecting under the fallback is also a key change.

A process whose key differs from the one that sealed a toolset's rows (a worker
started without the key or with another one, a rolling deploy that introduces it)
fails only its own calls: they answer `needs_sign_in` with the `KEY_CHANGED`
reason, and the process logs an ERROR that names the toolset and says the
encryption key differs. It never clears the shared tokens, so the processes that
can open them keep using them; only an admin Disconnect or Reconnect replaces the
row (lead ruling on audit G4-10). The status a process serves is its own view, so
a misconfigured process reports `needs_sign_in` while the others report
`connected`.
The token, client, pending-sign-in, and lease rows use the same storage provider as
the rest of the deployment, so API and worker pods observe one shared state.

## 8. Public surfaces

The REST surface is:

- `POST /v1/toolsets/{id}/oauth/start`, admin only, starts or restarts sign-in.
- `GET /v1/toolsets/{id}/oauth/callback`, public, consumes an authorization-code
  callback without requiring the browser to hold a Primer session; it requires the
  `primer_mcp_oauth` cookie the start answer set in the same browser.
- `GET /v1/toolsets/{id}/oauth/status`, user tier, returns
  `ToolsetAuthStatus`.
- `POST /v1/toolsets/{id}/oauth/disconnect`, admin only, ends the pending
  sign-ins, revokes when possible and deletes the stored sign-in.

The callback returns a 303 to the console for a browser request whose `Accept`
header includes `text/html`; other clients receive JSON or an RFC 7807 problem.
`system::get_toolset_auth_status` is a user-tier, read-only system tool that returns
the same status without credentials. A direct MCP call that needs a sign-in maps to
409 `/errors/mcp-sign-in-required`.

## 9. Internal contracts

The provider checks for a usable token before opening the upstream transport. If an
MCP call cannot authenticate, its tool error says exactly
`the MCP server <id> needs an operator to reconnect it`. A remote toolset that cannot
list is omitted from the current turn. The model receives the heading
`Toolsets unavailable this turn, so their tools are not offered:` followed by a
bullet naming the toolset and safe reason. The next turn tries to list it again.

OAuth callbacks reveal no toolset-existence oracle: an unknown toolset is handled
like invalid state. Pending state is short-lived and single-use. Tokens and secrets
must not appear in public status, tool results, errors, or logs. Origin binding
covers the toolset `url`, `resource_uri`, and `token_endpoint`; a stored masked
secret never follows a move to another origin.

Discovery uses a protected-resource document only when it describes this server
(RFC 9728 section 3.3, audit G4-07; `protocol.is_resource_of`): its `resource` is
the server's canonical URL, the configured `resource_uri`, or a parent of the
server's URL on the same origin (a document covering the whole origin, which many
servers publish and the MCP SDK's client accepts too). A document for another
host, scheme, port, sibling path or query, or with no `resource`, is refused with
a 400 that names both resources, so a 401 cannot steer the sign-in to another
server's authorization server. `oauth.resource_uri` itself must be on the MCP URL's
origin (scheme, host and port): every write refuses another one with a 422 naming
the field (`toolset_checks.check_oauth_resource_uri`, applied by the REST create and
update, where the problem's `extensions.errors[].loc` is
`body.config.config.oauth.resource_uri`, by the system tools as `validation-error`,
and by a harness install or sync), and a row stored before the rule cannot start a
sign-in or connect.

Every answer body the OAuth client reads (a discovery document, a token or a
registration answer) is streamed and read to 1 MiB (`protocol.MAX_BODY_BYTES`,
through `primer.common.bounded_read.read_capped`) before it is parsed, because the
servers that send them are named by the MCP server's metadata (audit G4-15). A
larger document is not used, and a larger token or registration answer is a
temporary server error; a revocation answer's body is never read.

## 10. Testing patterns

`tests/toolset/oauth/` covers discovery, client exchanges, encryption, leases,
refresh races, bearer retry, provider preflight, and retired-library imports.
`tests/api/test_mcp_oauth_routes.py` covers route tiers, browser and JSON callbacks,
state failures, status, and disconnect. The mock server in
`tests/_support/mock_oauth_mcp_fixtures.py` drives the real HTTP flow without an
external identity provider. The edit rules and the status system tool have focused
tests under `tests/api/` and `tests/toolset/`.

## 11. Historical decisions

- **The sign-in is shared by toolset, not selected per user.** The previous
  in-memory, per-user OAuth library was never wired end to end and could not share
  state across API and worker pods. Persisting one operator-owned identity makes
  the access model explicit and restart-safe.
- **Browser callbacks live on the Primer API.** The public callback can complete an
  upstream redirect without a Primer session, and the browser returns to the
  console. The state record alone was first taken as the binding ("public like
  SSO's"), but SSO binds its state to a signed cookie, and a state-only callback
  let anyone who obtained an admin's authorize URL attach their own provider
  account as the shared sign-in (audit G4-01). The `primer_mcp_oauth` cookie now
  binds the callback to the browser that started the sign-in.
- **Refresh is leased and fenced.** A process-local lock cannot protect several
  pods, so storage coordinates one refresher and compare-and-set keeps a late writer
  from overwriting a newer row.
