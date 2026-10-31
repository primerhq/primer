# MCP OAuth

## 1. Purpose

MCP OAuth lets an HTTP or SSE MCP toolset authenticate to its upstream server.
A browser (`authorization_code`) sign-in belongs to one person: its tokens are
keyed by (toolset, user), and a call or a listing sends only the token of the
person it acts for, never another person's (access-control spec 6.5). A
client-credentials sign-in is one shared row per toolset, used by every caller,
and so is any sign-in of the system person of an auth-disabled install. Any
full user who may use the toolset (a level on it, or a public tool of it) signs
in for themselves; connecting the shared client-credentials sign-in needs full
access on the toolset. The provider and agent integration is described in
[provider-pattern](../architecture/provider-pattern.md) and [agents](agents.md);
the person rules in [access-control](../architecture/access-control.md).

## 2. Conceptual model

The toolset row describes how to authenticate. A browser grant uses
`authorization_code`; a machine-to-machine grant uses `client_credentials`.
The sign-in state is separate from the toolset row and is shared by every API
and worker process. Its public status, read for one person (the shared row for
client credentials), has four values:

- `connected`: a usable token exists for that person (or the shared one).
- `needs_sign_in`: no usable token exists and that person (for client
  credentials, someone with full access on the toolset) must connect again.
- `error`: the last OAuth operation failed and the reason is safe to show.
- `not_applicable`: the toolset does not use OAuth.

The person of a call is `person_for_tool(ctx)`: the run's `initiated_by` when the
call carries a `ToolContext`, else the acting person
(`primer.access.acting.acting_person`). The auth middleware sets the acting
person for every authenticated request (REST, WebSocket, MCP; a bearer token's
owner); the tool manager sets it to the run's own person around every listing and
call of a run, whatever its task inherited; `dispatch_inner_tool` sets it from its
context (an approved re-run); and the MCP task bridge polls a parked task call as
that session's turn person, else its initiator. A call that acts for nobody (an
unattributed run, a graph ToolCall node's unattributed fallback) gets no browser
token: it answers sign-in required and never falls back to another row. The `principal` string
passed through a toolset provider remains dispatch context only and never selects
a token.

## 3. Architecture patterns implemented

The subsystem follows the [provider pattern](../architecture/provider-pattern.md):
`McpToolsetProvider` (cached per toolset and shared by every caller) builds an
`OAuthTarget` for the toolset and the call's person on every dispatch, asks one
`ToolsetOAuthService` for that target's token, and `McpBearerAuth` attaches it to
upstream requests. It uses the
[storage](../architecture/storage.md) abstraction for tokens, dynamically
registered clients, pending browser grants, and refresh leases. The
[REST API](../architecture/rest-api.md) exposes RFC 7807 errors and role-gated
routes. Cross-process refresh uses a storage lease plus compare-and-set writes so
one pod refreshes while its peers wait for the winning token row. Lease, fence
and the in-process lock are per token row, so two people refreshing the same
toolset never wait on or write each other's row.

## 4. Code layout

- `primer/toolset/oauth/protocol.py` is the pure-HTTP OAuth client: discovery
  (the protected-resource and authorization-server metadata, and their checks),
  PKCE, and the authorization-code, client-credentials, refresh, revocation and
  dynamic-registration requests.
- `primer/toolset/oauth/store.py` encrypts and persists OAuth state.
- `primer/toolset/oauth/egress.py` routes the OAuth client's requests: the MCP
  host directly, every other host through the private-destination guard.
- `primer/toolset/oauth/service.py` owns the sign-in lifecycle and status:
  `ToolsetOAuthService`, the public `ToolsetAuthStatus` and `SignInStart`
  models, and the refresh lease.
- `primer/toolset/oauth/bearer.py` attaches bearer tokens and retries once after a
  401.
- `primer/toolset/mcp.py` opens the upstream MCP transport only after the OAuth
  preflight succeeds.
- `primer/api/routers/mcp_oauth.py` exposes the sign-in routes; who may act on
  them is `primer.access.tools.may_use_toolset` (browser) or FULL on the toolset
  (client credentials).
- `ui/components/toolsets/mcp-auth.jsx` is the console panel (`TS_McpAuthPanel`)
  and the catalogue card's sign-in pill (`TS_cardAuthStatus`).
- `primer/model/mcp_oauth.py` defines the stored rows and the typed
  sign-in-required error.
- `primer/storage/migrations/m017_orphan_mcp_oauth_rows.py` removes the sign-ins
  that toolsets deleted before #852 left behind.
- The per-user OAuth migration (currently
  `primer/storage/migrations/m027_mcp_oauth_per_user.py`) turns the shared browser
  token sets of an upgraded install into per-person ones.

## 5. Data model

`McpOAuthTokenSet` stores encrypted access and refresh tokens plus expiry and scope
metadata. A person's browser sign-in is stored under
`token_key(toolset_id, user_id)` (`"u-"` and the SHA-256 of the two ids joined by a
NUL, so no pair collides with another or with a bare toolset id); the shared row
(client credentials, the system person) under the bare toolset id. Every row names
its `toolset_id` and `user_id` (`None` for the shared row), and
`McpOAuthStore.get_tokens` returns nothing for a row whose fields name another
pair. `connected_by` is the user id of the person who connected it (a row from
before per-person sign-ins holds a username). `McpOAuthClient` stores dynamic client
registration metadata; only a client secret issued by dynamic registration is
encrypted in this row. A static client secret typed into the toolset configuration
stays in the toolset row and follows its existing masking and origin-binding rules.
`McpOAuthPendingSignIn` is keyed by a hash of the browser state value and stores an
encrypted PKCE verifier until the callback consumes it, and the `user_id` of the
person whose row the callback writes. The registered client stays per toolset: it
is primer's registration, not a person's. The public
`ToolsetAuthStatus` never includes a token or client secret.

## 6. Lifecycle

For a browser grant, a person starts their own sign-in (a browser sign-in for
nobody is refused), Primer discovers the upstream metadata, creates or reuses the
toolset's client, records one short-lived state and PKCE verifier naming that
person, and returns the authorization URL. `PRIMER_PUBLIC_URL` supplies the
stable callback origin in deployments where request-derived URLs are not safe.
The start answer also sets the `primer_mcp_oauth` cookie: a fresh nonce,
HttpOnly, `SameSite=Lax`, scoped to the callback path, living as long as the
pending sign-in (600 s), and `Secure` when the callback is https. The pending row
keeps only the nonce's SHA-256. The public callback takes the state back only with
that cookie, then exchanges the code, stores the token as the sign-in of the person
the pending row names, clears the cookie and returns to the console. A callback without the cookie, or with another
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

For client credentials, someone with full access on the toolset starts sign-in and Primer obtains the shared token
immediately. During normal MCP traffic, `McpBearerAuth` sends the call's token (its
person's, or the shared one) on every request. A 401 triggers at most one refresh and one retry. When the server
rejects the refreshed token too, a browser sign-in is signed out with the reason
`The MCP server rejected the access token.` (`ToolsetOAuthService.sign_out_rejected`,
fenced on the version that holds that token, so a reconnect or a newer token
stored meanwhile is left alone): its status stops saying `connected`, and later
calls fail at once instead of spending a refresh token each. A client-credentials
toolset can recover without a person (the rule a refused client-credentials
refresh follows too): its row is marked `error` with that reason and only the
access token is dropped, the call fails as a temporary provider error, and the
next call exchanges the credentials again. Refresh is coordinated
under a lease across pods, and a compare-and-set write prevents a late refresher
from replacing newer credentials. Disconnect ends the caller's own sign-in (the
shared row for client credentials) and leaves every other person's alone: it first
ends that person's pending sign-ins (a Reconnect left open on the provider's page
would otherwise connect them again when completed), attempts revocation and then
removes their token row. Every path that deletes a toolset forgets all its
sign-ins (every person's tokens and the shared ones, its registered client and its
pending sign-ins): the REST delete and the
system `delete_toolset` tool through `ToolsetOAuthService.forget` (the tool forgets
before it deletes the row, and a forget that fails answers `storage-error` and
leaves the toolset in place), and a harness
uninstall or a sync that drops the toolset through the store's keyless
`forget_sign_in`, which the service's `forget` is built on. A toolset created
again under the same id is never connected on the deleted one's sign-in. The
system tool and the harness paths kept the sign-in before #852, so migration 17
(`primer/storage/migrations/m017_orphan_mcp_oauth_rows.py`, ticket 01a16a36)
deletes, on an upgraded install, every token row, registered client and pending
sign-in whose toolset no longer exists. It never touches a row whose toolset
exists (read through its own frozen view, so a stored toolset of an older shape
still counts) or a pending sign-in that names no toolset, and a second run
deletes nothing more. The keyless `forget_user_sign_ins(storage_provider, user_id)`
removes one person's token rows and pending sign-ins on every toolset (the shared
rows and registered clients stay), for removing a user.

The per-user OAuth migration (`M027McpOAuthPerUser` in
`primer/storage/migrations/m027_mcp_oauth_per_user.py` at the time of writing) makes
an upgraded install's sign-ins per person. A browser token set stored under the bare
toolset id moves, with its sealed tokens as stored, to the user its `connected_by`
names (by username, else by id), else to the first admin (the oldest admin); with
no users at all (the auth-disabled shape) it stays under the bare id. Not the
toolset's owner: the token is an upstream account, and handing it to someone who
never held it would be an escalation through a credential. Client-credentials rows
stay the shared row. Every token row is stamped with `toolset_id` and `user_id`,
every pending sign-in is dropped (ten-minute TTL), registered clients are left
alone, every row is read before any is written, and a second run changes nothing.
Every other user's runs then ask that user to sign in.

Changing an OAuth toolset's URL or any OAuth setting requires an admin. An accepted
change signs the toolset out, even when the typed secret makes the change safe from
the separate masked-secret repoint guard. An unchanged save and a refused save keep
the existing sign-in. Every update path applies the one predicate,
`toolset_checks.oauth_settings_changed`, against the stored row and forgets the
sign-in, every person's, before it writes the row: the REST update and the system `update_toolset`
tool in their pre-update hooks, and a harness sync for each toolset it changes
(`primer/harness/service.py::_forget_sign_in_on_oauth_change`, through the same
keyless `forget_sign_in`; ticket 01a16a36, L4 of the #852 review). A sync's forget
that fails leaves that toolset's row as it was and fails the sync
(`partial_apply_failure`, the snapshot not advanced), so the new settings are never
stored beside the old sign-in. Headers and the transport are outside the rule.

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
can open them keep using them; only a Disconnect or Reconnect of that sign-in replaces the
row (lead ruling on audit G4-10). The status a process serves is its own view, so
a misconfigured process reports `needs_sign_in` while the others report
`connected`.
The token, client, pending-sign-in, and lease rows use the same storage provider as
the rest of the deployment, so API and worker pods observe one shared state.

## 8. Public surfaces

The REST surface is:

- `POST /v1/toolsets/{id}/oauth/start` starts or restarts the caller's own
  browser sign-in (any full user who may use the toolset), or connects the shared
  client-credentials one (full access on the toolset).
- `GET /v1/toolsets/{id}/oauth/callback`, public, consumes an authorization-code
  callback without requiring the browser to hold a Primer session; it requires the
  `primer_mcp_oauth` cookie the start answer set in the same browser. It writes the
  row of the person who started the sign-in only when that person still exists, is
  enabled and a full user, and may still use the toolset, and when no other person
  is signed in on the callback's request; otherwise `invalid_state` and nothing is
  written.
- `GET /v1/toolsets/{id}/oauth/status`, for anyone who may use the toolset,
  returns the caller's `ToolsetAuthStatus` (`per_user` is true for a browser
  sign-in: the caller's own).
- `POST /v1/toolsets/{id}/oauth/disconnect` ends the caller's pending sign-ins,
  revokes when possible and deletes the caller's own browser sign-in (other
  people's stay), or the shared client-credentials one (full access on the
  toolset).

A toolset the caller may not use answers exactly like a missing one (404) on every
route; a restricted user gets the user tier's 403. There is no route that ends
every person's sign-in: changing the toolset's OAuth settings, or deleting it,
forgets them all.

The callback returns a 303 to the console for a browser request whose `Accept`
header includes `text/html`; other clients receive JSON or an RFC 7807 problem.
`system::get_toolset_auth_status` is a user-tier, read-only system tool that returns
the run's person's status without credentials, and the missing-toolset answer to a
run whose person may not use the toolset. A direct MCP call that needs a sign-in maps to
409 `/errors/mcp-sign-in-required`.

`GET /v1/tools` carries, per toolset the caller may use a tool of, an `auth` entry
with the caller's own status (`mode`, `state`, `reason`, `expires_at`, `per_user`).

The console panel (`TS_McpAuthPanel`, mounted on the toolset overview, its Tools
tab when a listing needs a sign-in, and the post-save result) reads the status
route. A browser sign-in is labelled "Your sign-in" and its Connect, Reconnect and
Disconnect work for whoever the route answered; the Disconnect confirm says "Your
runs lose access to this toolset until you connect again." A client-credentials
sign-in is labelled "Shared sign-in"; the panel reads `your_level` from
`GET /v1/toolsets/{id}/access` and enables its buttons for `full` or `owner`, else
says "Only people with full access can connect this toolset." The catalogue card
pill and the shared tool picker read the `auth` entry of `/v1/tools`: a browser
sign-in the caller has not made reads "Needs your sign-in". The phone shows a
toolset as a read-only fact sheet and never mounts the panel.

## 9. Internal contracts

The provider checks for a usable token before opening the upstream transport. If an
MCP call cannot authenticate, its tool error says exactly, for a browser sign-in,
`the MCP server <id> needs the person this run acts for to sign in (Toolsets > <id> > Connect in the console)`
(`McpSignInRequiredError(per_user=True)`), and for client credentials
`the MCP server <id> needs an operator to reconnect it`. The error is in the
transcript of whoever sent the turn. A remote toolset that cannot
list is omitted from the current turn. The model receives the heading
`Toolsets unavailable this turn, so their tools are not offered:` followed by a
bullet naming the toolset and its state in a fixed word (`needs sign-in`,
`unreachable`, `timed out` or `failed`): never the server's text, which a
`tools/list` error can be (audit G4-14). A call into the toolset answers the
fuller reason as a tool error. The next turn tries to list it again.

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
server's authorization server. `oauth.resource_uri` itself must be the MCP URL or
a parent of it on the same origin (scheme, host and port), the same rule
(`protocol.is_resource_of`): another origin, or another path of this one, is
another server's resource, and every write refuses it with a 422 naming
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

The OAuth client does not call private addresses an MCP server names (audit
G4-08). Its transport (`primer/toolset/oauth/egress.py`) routes each request by
host and port: the MCP URL's own host and port, and those of a token endpoint the
toolset configures, are reached directly (the MCP URL is chosen by the toolset's
author and is not guarded); any other host or port (another service on the MCP
server's machine, the protected-resource document a 401 points at, the
authorization server's metadata, token, registration and revocation endpoints)
passes `primer.common.ssrf`'s private-destination policy at connect time
(`ssrf.public_addresses` through `netguard.GuardedNetworkBackend`) and connects to
the address that passed. A refused endpoint is a 400 naming the URL (a refresh
keeps its tokens and reports `error`). The only opt-in is the policy's existing
one, `workspace_allow_private_url_sources` (`PRIMER_WORKSPACE_ALLOW_PRIVATE_URL_SOURCES`),
for deployments whose MCP servers use an authorization server on a private
network. Like the web tools' guarded client, the explicit transport means httpx
does not apply `HTTP(S)_PROXY` to these calls.

## 10. Testing patterns

`tests/toolset/oauth/` covers discovery, client exchanges, encryption, leases,
refresh races, bearer retry, provider preflight, and retired-library imports;
`tests/toolset/oauth/test_per_user_tokens.py` covers per-person token sets (whose
token a call or listing sends, no person no token, the shared rows, per-row lease
and lock, forget rules), and `tests/storage/test_m027_mcp_oauth_per_user.py` the
migration.
`tests/api/test_mcp_oauth_routes.py` covers browser and JSON callbacks, state
failures, status, and disconnect; `tests/api/test_mcp_oauth_per_user_routes.py` who
may start, read and end a sign-in (a grantee, a public-tool user, a stranger's 404,
the restricted tier, full access for client credentials) and the callback re-check
(access lost, a disabled, demoted or deleted user, another person signed in).
`tests/toolset/test_toolset_auth_status_per_user.py` pins the status tool to the
run's person, `tests/access/test_acting_person.py` and
`tests/bus/test_mcp_task_bridge_person.py` whose token a request, a run's listing
and the task bridge send, and `tests/ui/test_p5_oauth_ui.py` the console panel. The mock server in
`tests/_support/mock_oauth_mcp_fixtures.py` drives the real HTTP flow without an
external identity provider. The edit rules and the status system tool have focused
tests under `tests/api/` and `tests/toolset/`.

## 11. Historical decisions

- **The sign-in was shared by toolset, then became per person.** The previous
  in-memory, per-user OAuth library was never wired end to end and could not share
  state across API and worker pods, so a persisted, operator-owned identity per
  toolset replaced it. Access control (Phase 5) keys browser sign-ins by
  (toolset, user) in the same persisted store, because a shared upstream account
  let every user of a toolset act as whoever connected it; client credentials,
  which name no person, stay shared.
- **Browser callbacks live on the Primer API.** The public callback can complete an
  upstream redirect without a Primer session, and the browser returns to the
  console. The state record alone was first taken as the binding ("public like
  SSO's"), but SSO binds its state to a signed cookie, and a state-only callback
  let anyone who obtained an admin's authorize URL attach their own provider
  account as the shared sign-in (audit G4-01). The `primer_mcp_oauth` cookie now
  binds the callback to the browser that started the sign-in.
- **The callback re-checks the person (Decision 5 of the access-control Phase 5
  plan).** A sign-in can outlive the access it was started with by up to ten
  minutes, so the callback writes nothing for a person who lost access, was
  disabled, demoted or deleted meanwhile, or when someone else is signed in on the
  browser that brings it back.
- **No route ends every person's sign-in.** Each person ends their own; changing
  the toolset's OAuth settings or deleting it already forgets them all, which is
  the incident-response path (lead answer to the Phase 5 plan's Question 2).
- **Refresh is leased and fenced.** A process-local lock cannot protect several
  pods, so storage coordinates one refresher and compare-and-set keeps a late writer
  from overwriting a newer row.
