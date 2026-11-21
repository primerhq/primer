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
  Different authorities must have distinct decoded keys; aliases of one key refuse.

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
0600, with exactly `identity`, `revisions` and an empty `conflicts` list. Identity
uses `manifest_identity`: SHA256 of UTF-8 `primer-codex-host-v1` followed by a
newline and JSON with recursively sorted keys, compact separators, no ASCII
escaping and no nonfinite numbers. Both initial and live manifests undergo the
same strict schema validation. Harmless object-key reorder keeps this identity;
a numeric UID replaced by a boolean or float refuses. Revisions map each authority
to its initial signed payload's `revision` and hex `sha256`. Missing records or
identity mismatch refuse; there is no automatic migration of older candidates.

Every bundle authenticates ALL authorities and the audience before any ledger
write. After complete authentication, every higher revision is recorded, even
when another authority supplies an older revision. The mixed bundle is still
refused. Thus a newly observed signed denial survives a different authority's
rollback in either iteration order. Same-revision differing signed payloads
record a durable conflict and refuse subsequent grants until trusted recovery.
Unauthenticated sets never advance the ledger. All policy handles reread the
same exclusively held store, including unchanged-revision authorization.

A second mandatory 0600 record, `policy_session.json`, contains exactly the same
`identity` and `state: "ready"`. Before observing any policy, the broker durably
replaces ready with active, including file AND directory fsync. It never clears
active automatically, including on graceful close. The same open store may have
several ledger handles; a reopened store or new process refuses an active marker.
This intentionally trades automatic restart availability for explicit recovery.
A failed initial marker write observes no policy and authorizes nothing. A failed
later ledger write is preceded by the already durable active marker. In addition,
a write exception fences the store device/inode for the entire interpreter
lifetime, including after all handles close and are reconstructed. No reset API
or enrollment option clears that uncertainty.

Before a new broker process can start after ANY store close, the administrator
must stop all owners, reconcile with every authority's current authenticated
monotonic decision (including restrictions), resolve conflicts/unknown write
outcomes, and durably checkpoint the ledger and a ready session record. Restore
from an old backup alone is not recovery. Do not mark ready unless the current
high-water decisions are established; otherwise leave the broker unavailable.
After an in-process uncertainty, recovery also requires a new interpreter. This
slice supplies no automated recovery/provisioning command, and no claim that root
ownership establishes enterprise inventory completeness. Deployment remains gated
on an actual reviewed supervisor and administrative recovery procedure.

Open trust/store handles are mandatory for every authorization/read/write and
socket operation. Both handles belong to their constructing process and thread;
construction accepts only the actual Linux process leader (native TID equals PID)
registered as main, or an exact standard `threading.Thread`. The kernel identity
check prevents a cold import on an unmanaged thread from claiming main ownership.
Non-leader unmanaged native threads (including persistent dummy-thread wrappers)
and custom Thread subclasses refuse before opening files. A newly forked process
leader promoted to Python's main object may construct fresh handles irrespective
of its pre-fork Thread subclass; the kernel leader lifetime is its authority.
Inherited parent handles remain forbidden for authorization. Within supported managed lifetimes,
thread object identity prevents recycled numeric thread IDs from acquiring access.
Foreign-thread use and close refuse; the public APIs are deliberately not a
cross-thread ledger service. Sibling ledgers share the same single owner thread,
so their complete synchronous transitions serialize without a partial-vector race.
Forked children may only close their inherited descriptor copies, without an
explicit flock unlock; they cannot read, write, authorize, or connect. The parent's
lock and handles remain usable. A supervisor must construct handles in the final
broker process/thread, never preconstruct and transfer them across fork.
The composed ledger checks both owner lifetimes before private-store IO; closing
only HostTrust or only PrivateStore refuses retained policy grants before reads. Private
record and lock acquisition is nonblocking followed by regular-file validation,
so a misprovisioned FIFO cannot hang initialization. Closing the store releases
its lock, and retained policies cease authorizing.

These controls defend against policy-file replay, not hostile root/kernel,
trusted broker-UID code or rollback of all host storage. Filesystem fsync semantics,
policy expiry and clock trust remain assumptions. A crash before the initial
active-marker fsync has not observed new policy; a crash after successful fsync
requires the explicit recovery above. Authenticated malformed newer payloads may
advance the fence and deny access. Authority/key rotation and backup restore need
stopped administrative reprovisioning. No production activation is supplied.

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

Disposal revokes descriptor fields before calling the OS. A late close error
cannot leave authority live or cause a retry against a recycled descriptor number.
Store lock and directory release are independently attempted once; composed host
cleanup attempts trust disposal even if server/store cleanup failed, and surfaces
all cleanup errors. OS release after an error is not assumed successful; no retry
or automatic session-ledger recovery is authorized by disposal.

Descriptor acquisition obeys the same transfer rule: directory walking tracks the
new child before releasing the previous descriptor. Policy stores and sockets
remain locally owned until verified-parent disposal completes; a parent close
error disposes the unreturned resource. Independent cleanup failures are retained
without retrying an uncertain descriptor number. If the OS reports an error
before releasing a descriptor, its actual state is unknown to production code;
this boundary deliberately does not retry it or claim guaranteed kernel release.
