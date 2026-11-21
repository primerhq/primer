# Credential broker candidate (unregistered)

This package implements broker-side code, exercised with synthetic OAuth
receipts and real cryptographic signatures. It does not provide a deployed
broker, authenticated account, login UI, or selectable Primer provider. There
is no CLI that silently logs in, loads personal Codex auth, or starts a service.
The approved bridge in the parent commit is retained unchanged.

## Concrete boundary

`PrivateStore` exclusively locks a pre-provisioned owner-only directory. It
uses no-follow descriptors, bounded reads and atomic owner-only writes with
file/directory fsync. A second broker cannot rotate the same store concurrently.
One store contains one account registration, bound to issuer, subject and issued
client ID; email is never an identity or workspace key. Different registrations
need separate stores. Same-UID code and the directory's ancestors remain trusted.

`SignedPolicy` verifies every configured Ed25519 authority and intersects model
permissions. Unknown fields, missing/extra authorities, expiry, wrong audience,
suspension and required native effects refuse. It checks before enrollment,
credential use, discovery, model construction and each delivered model event.
Revision and token generation changes invalidate existing steps. Keys and the
complete authority inventory must come from an authenticated administrator,
not Primer JSON, a model response or a broker capability claim. This code does
not discover enterprise/cloud policy or prove that a supplied inventory is
complete. A deployment must establish that provenance before activation; this
is not permission to override an existing administrator policy. The trusted
loader must enforce authenticated freshness and rollback protection; a still-valid
old signed bundle is not rejected solely because its revision was seen earlier.

`SignIn` creates fresh state, nonce and S256 PKCE, uses a single-use expiring
attempt, verifies RS256 signatures from issuer metadata/JWKS, and checks issuer,
audience, expiry, issued-at, nonce and subject. Returning authentication cannot
replace the selected registration with another identity/client. Token response
scopes gate plan use. Completion stages validated tokens privately for at most
five minutes and returns only issuer/subject/client/account identity to the
management caller. This lets an administrator approve a previously unknown
binding. Explicit `commit_registration()` requires a current signed account
grant before persisting or using those tokens. Failed approval leaves staged
tokens unusable. Starting another attempt refuses until explicit staged-session
disposal; sign-out attempts revocation and retains the verified registration.
The model socket cannot enroll, approve or commit credentials.

Malformed or wrong-state callbacks leave the legitimate attempt intact. A
matching denial or exchange consumes it before network I/O. An issued client ID
from a matching callback is persisted separately as an **unverified continuation**
before exchange, so invalid_grant and restart reuse that client with fresh PKCE,
state and nonce. This hint confers no subject identity, model access or signed
policy approval. A validated staged identity is volatile until commitment or
sign-out retains its safe mapping; restart can reauthenticate with the hint.

Refreshes serialize in one broker and quarantine rotating credentials durably
before network I/O. Failure or cancellation disables local use instead of
retrying the old refresh token. A restart sees either the quarantine marker or
a fully written replacement, never the spent old token after a successful
quarantine fsync. A filesystem failure is surfaced, not retried. Successful
rotation atomically replaces the token set. Reauthorization retains the client
mapping and host ID. Sign-out disables local use and captures both active and
staged renewable sessions, preserving verified identity without access tokens.
Distinct refresh tokens are revoked under an absolute ten-second budget. Failed
or cancelled attempts remain in a private, non-authorizing revocation queue for
explicit retry; new enrollment refuses while that queue is unresolved. Failures
remain visible, and no token from the queue can be used for inference. The
management host must close active connections on sign-out; subsequent event
authorization already refuses.

`FixedHTTP` has literal inference/token/catalog endpoints, TLS verification,
no environment proxies or redirects, bounded bodies/frames and sanitized errors.
Requests advertise identity encoding; other Content-Encoding values refuse before
reading or invoking a decoder. Raw byte budgets and announced lengths are checked
before buffer extension. Small SSE events are delivered without batching to an
artificial chunk size or waiting for EOF.
Issuer metadata may select only same-origin well-known JWKS and same-origin
revocation paths. It never follows token header URLs or accepts an API-key
fallback. SSE is translated by the existing bridge; only Primer function tools
are admitted and complete tool calls remain passive outputs for Primer to run.

`BrokerServer` accepts a host-provisioned Unix listening socket. Each connection
uses Linux SO_PEERCRED and a pinned peer UID/account. Its closed protocol has
only `models/list` and `step/start`; one request per connection, eight active
operations, bounded input/output and deadlines. Disconnect or extra input
cancels the operation and closes its bridge/channel. It returns normalized model
events and generic errors, never credentials, arbitrary HTTP, auth, raw RPC,
reload, executor, hook, patch or MCP methods. Host code must provision the socket
ACL and a broker OS identity distinct from Primer, with credential directory
access denied to Primer. A UID check alone does not isolate same-UID processes.
Tests use temporary same-UID sockets and a deliberately mismatched UID refusal;
they do not claim a production cross-user deployment.

No production registry, executable selection, environment activation flag or
provider configuration was added. Python/dependencies remain trusted, and
in-process timeouts are cooperative. Host process supervision, OS isolation,
hard termination, whole-operation OAuth/refresh management deadlines, authority
provisioning and executable/package pinning are still required before calling this an operational security boundary.

## Official contract refreshed 2026-11-21

- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in):
  dynamic public-client registration, exact loopback callback and issued client
  reuse; signed identity and granted scopes, rather than callback scope claims.
- [Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions):
  account separation, serialized rotating refresh, protected storage and
  renewable-session revocation. This implementation conservatively requires
  reauthorization after an uncertain refresh; it never retries that token.
- [Token reference](https://developers.openai.com/siwc/token-sharing-open-source/token-reference):
  OAuth response shape and lifetimes. Access tokens stay opaque to the broker;
  it verifies the signed ID token rather than inferring identity from an
  unverified access-token payload.
- [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference):
  the account's `/v1/models` list with visible slugs and OAuth-authenticated
  `https://api.openai.com/v1/responses`, `store:false`, `stream:true`.

These are documented endpoints, not proof of account eligibility or a successful
live request. Exact fetched pages and source evidence accompany the review
packet. No external auth or model calls were made.

## Remaining delivery

Next review must examine this code and adversarial tests before adding a trusted
host launcher/client and management UI. It must establish real administrator
key/inventory provenance, OS-user/socket isolation, package/dependency identity,
loopback listener ownership and consent UX. Then genuine dedicated sign-in,
workspace/plan entitlement, model discovery and Responses tool/replay evidence
can be gathered. Config/registry/UI/MCP integration, broader response fidelity
and hosted delivery checks remain open. No existing personal credentials may
be copied to close these gates. There is no active broker to deploy or register
merely because offline tests pass.
