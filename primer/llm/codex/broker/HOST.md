# Host trust boundary candidate (not activated)

This slice supplies `HostTrust`, `PolicyLedger`, `host_broker` and a peer-checked
model-socket connection. It keeps the approved broker unregistered. It is an
executor-free Sign-in-with-ChatGPT Responses adapter, not the Codex app-server.
No service, user, key, credential, socket or configuration is provisioned here.

## Trust contract

The embedding host must provide an absolute trust-directory path. Every path
component is opened relative to a verified descriptor with O_NOFOLLOW; each must
be root-owned and not writable by group or others. `host.json` and `policy.json`
are bounded regular single-link root-owned files, also not group/other writable.
No environment, HOME, model request, config selector or personal auth is consulted.
This establishes OS-root provenance only. It cannot establish that the root
administrator's authority inventory accurately includes every enterprise policy.
That authenticated provisioning is still an external prerequisite.

The closed host manifest has these fields:

- `audience`: signed-policy audience.
- `broker_uid`, `primer_uid`: distinct, nonzero integer Linux UIDs.
- `account`: 64-character subject/client binding from verified dedicated identity.
- `store`: absolute path to an existing broker-owned 0700 credential directory,
  with exclusively root-owned non-writable ancestors.
- `socket`: absolute model socket path in a root-owned non-writable directory.
  The socket itself must be broker-owned 0660; the administrator provisions its
  group membership and listener. The code never relaxes permissions.
- `authorities`: complete map from authority ID to base64 Ed25519 public `key`
  and canonical decimal `minimum_revision` (1 through 19 digits, no leading zero).

Root must supply the actual account binding after dedicated identity verification;
this is not an invitation to fill in an invented value. Authenticating a new
account and management approval remain a separate, unexposed flow. Policy/key,
account, UID or path changes require process stop and explicit reprovisioning.
Changing `host.json` in place refuses subsequent grants/connections. Replace
`policy.json` atomically inside the same trust directory; replacing the directory
itself is not a hot-reload mechanism because the process pins its descriptor.

`host_broker` checks the actual effective UID, securely opens/locks the store,
loads the policy ledger and verifies an account grant BEFORE constructing HTTP
or SignIn. It yields a BrokerServer pinned to the root-selected Primer UID and
account. It does not bind or serve a socket automatically. All existing model
operation and event policy checks use this same ledger-backed SignedPolicy.

The client-side connection checks the actual Primer UID, socket path owner/mode,
and Linux SO_PEERCRED broker UID before returning a nonblocking connected socket.
No request bytes are sent during verification. The server already checks the
opposite peer UID. Same-UID code remains trusted. This is not executable identity
attestation: compromised broker-UID code can impersonate the broker. A separately
reviewed supervisor must pin the code/package, dependency environment and service
identity before deployment. No arbitrary command or executable path is accepted.

## Rollback and uncertain persistence

An administrator must pre-provision `policy_ledger.json` in the private store as
0600, with exactly `identity` and `revisions`. Identity is SHA256 of the canonical
compact JSON host manifest. Revisions map each authority to its initial signed
payload's `revision` and hex `sha256`. The code will not silently bootstrap a
missing record or migrate identity: deletion/identity mismatch fails closed.
Keys/inventory are never read from this broker-writable record.

Every bundle authenticates ALL authorities and the audience before any state
write. Revisions must be at least the root-pinned floor and previously durable
revision. Reuse of a revision with a different signed payload refuses. A newer
signed denial, including suspension, is persisted before grant evaluation, so a
later older permissive bundle cannot undo it. File and directory fsync precede
return to the authorizer. Failure leaves this instance fenced permanently,
including when rename may have succeeded. No same-process retry clears the fence.

This defends against replay of policy files, not rollback of the entire trusted
host/storage or hostile kernel/root/broker code. Following an uncertain write,
recovery must reconcile the authoritative policy and durable ledger; no restart
claim exceeds the last confirmed fsync. Policy expiry and clock trust remain
required. Deliberately signed malformed newer payloads may advance the fence and
refuse access; correcting them requires an even newer revision. Authority/key
rotation and backup restore need explicit stopped administrative reprovisioning.

## Evidence and remaining gates

Offline tests use real signatures, actual temporary descriptors/modes/symlinks,
real SO_PEERCRED and local sockets with no network inference. Where root owner or
alternate UID is simulated, tests say so. They do not attest actual separate OS
users, production socket ACLs or complete enterprise policy inventory.

The official [accounts and sessions contract](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
requires separate account registrations and protected token storage. Refreshed
2026-11-21. The root-manifest, ledger and Linux peer checks are Primer's controls,
not an OpenAI-admin-policy API or OpenAI attestation. Original sign-in, token and
Responses endpoint citations remain in README.md. There is no API-key fallback.

Remaining: real administrator provisioning and authenticated inventory, distinct
OS users and supervised immutable package identity, loopback consent/management
UX, dedicated live sign-in and entitlement/model evidence, full model IPC client
translation/replay and configuration/UI integration, hosted regression gates and
reviewed activation. Production registry, public tools/UI, live E2E and deployment
are deliberately absent from this internal security slice, not declared complete.
