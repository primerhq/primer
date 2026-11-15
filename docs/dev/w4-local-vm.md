# Local isolated-VM W4 F1 evidence

This developer evidence lane is an explicit alternative authority to the manual
GitHub lane, not a way to bypass it. Neither ordinary CI nor the scripted
base-prompt journey establishes real-model F1. There is no automatic VM creation,
model reservation, shared-service change or retry in these entry/gate modules.

The original workflow, CI acceptance module, protected prerequisites and CI gate
remain CI-only. Local evidence has `authority_mode: local-vm`, a different named
JUnit class, `local-accepted.json`, a signed `local-gate.json` and a separate
`local-cleanup.json`. CI rejects local authority. The local path rejects GitHub
and runner receipts instead of fabricating them.

## Authority before any VM boot

A real model owner must first identify every other K1 consumer and establish
bounded holds with their owners. The exact endpoint is
`http://asus-gx10.piranha-ph.ts.net:8000/v1`, model `qwen3.8-27b`. Capacity, idle
metrics, SSH access, provider concurrency and a client-side lock do not establish
a model-wide reservation. If actual complete holds are unavailable, stop. A
shared-capacity exception or admission-policy alternative needs a separate review.

Host execution authority and model-owner authority use distinct independently
pinned Ed25519 keys. They are trust roles, not evidence that somebody has already
established the holds. The lead must verify the real principals/key fingerprints
and original owner acknowledgements. Never generate a key/owner label to invent
reservation authority. No actual keys or reservation are supplied by this code.

`local_authority.validate_owner_reservation` defines the signed owner payload:
exact source, planned run/VM UUIDs, endpoint/model, finite window, complete consumer
inventory and one covering acknowledgement per consumer. Every acknowledgement
names an original host evidence file and SHA256. Unknown fields reject. Signature
messages are canonical sorted compact JSON with nonfinite values forbidden.
The envelope is `{payload, signature}`; signature is Ed25519 hex.

The trusted host runs `uv run python -m scripts.e2e.w4_f1.local_preflight --help` and
supplies the independently pinned host and owner public keys/fingerprints,
host-only signing key, original hold evidence directory and exact clean source.
This pure gate runs before any VM boot and writes an exclusive host-signed
canonical receipt. It never contacts or reserves the model,
starts a VM, or substitutes an acknowledgement for its factual verification.
A fresh preflight window is 15 to 30 minutes; the owner holds must cover it.
Preserve the original files for independent review, not only their digests.
The signed execution authority must embed this exact envelope and its original
file SHA256, source/driver hashes, run/planned VM, window and owner reservation.
The gate requires its timestamp at least two seconds before the actual QEMU
kernel start time. Missing, swapped or post-boot receipts reject.

## Host and guest execution evidence

Only after the real reservation and resource window are verified may the owner
prepare the isolated Ubuntu 24.04 KVM guest. Use its own Docker/private
`primer_w4_f1` Postgres, native `/usr/bin/python3.12` with pidfds, 2 CPUs, 6 GiB RAM
and a 20 GiB private qcow2 disk. No host filesystem mounts, host device passthrough,
shared Docker socket, credentials or shared database may enter the guest. The
headless XML policy permits no channels, serial/console backends, graphics,
host devices, extra LUNs or arbitrary media. Use SSH over the default libvirt NAT;
prepare the guest without automatically added console/agent channels. The only
host files are the exact private `guest.qcow2`, `base.qcow2`, and optional
read-only `seed.iso` under `/var/tmp/w4-f1-<vm-uuid>`. Record the seed digest when
present. The host checks canonical non-symlink files, ownership, link counts and
permissions, the base/seed bytes and the complete backing chain. Guest
network authorization and actual guest `/models` reachability must be recorded;
host reachability is not guest reachability.

The host-signed execution payload also binds a fresh run UUID, VM domain XML/image
hash/resource limits, actual guest boot and machine IDs, exact source/driver
hashes, lead authorization and window, original preboot receipt, actual host
QEMU PID/start ticks/start time, and guest boot epoch. A trusted host brackets an
actual guest UTC reading with its own UTC readings; interval and skew must each
be at most two seconds. The final host observation must find the same QEMU
process. The payload schema is explicit in
`local_authority.validate_authority`. The model-owner payload is separately signed
inside it. The host must observe the real VM rather than sign guest assertions.
No host or owner signing private key enters the guest, image or private app.

Install only the raw 32-byte public keys at the fixed root-owned, non-writable,
non-symlink paths `/etc/primer-w4-f1/authority.pub` and
`/etc/primer-w4-f1/model-owner.pub`. Their ancestor chain must also be protected.
The guest cannot select another trust key. Run the probe unprivileged. Where DMI
UUID permissions require it, guest sudoers may grant only
`/usr/bin/cat /sys/class/dmi/id/product_uuid`; never elevate the probe/app.
The guest observes KVM/DMI/boot/machine identity, native Python and mount types.
It refuses a host process, wrong guest, shared filesystem or optimized Python.

The exact clean reviewed source must already be committed. Outputs live outside
it at `/var/tmp/w4-f1-local-artifacts/<run-uuid>`; prepare the private parent only.
No attempt output may already exist or be deleted to retry a failed/uncertain run.
After all prerequisites are approved, run exactly the explicit local case:

```bash
W4_F1_LOCAL_AUTHORIZATION=/run/f1/authorization.json \
W4_F1_LOCAL_OUTPUT='/var/tmp/w4-f1-local-artifacts/<run-uuid>' \
W4_F1_LOCAL_POSTGRES_DSN='<private guest DSN>' \
uv run pytest scripts/e2e/w4_f1/local_acceptance.py -o addopts= -n0 \
  --timeout=660 --timeout-method=thread \
  --junitxml='/var/tmp/w4-f1-local-artifacts/<run-uuid>/local-junit.xml'
```

This is an execution example, not an authorization to run it. The original real
lifecycle, relay, 11-request bound, process ownership, Postgres lease observations,
compaction, two unpredictable nonces and cleanup/raw oracles are shared unchanged.
The local monotonic timer starts before source, key, guest and network checks;
that preparation consumes the 570-second active allowance. Bounded app cleanup
remains 25 seconds and accepted total at most 600 seconds. Absolute request
intervals, native commits, watch/snapshot/lease observations and actual process
start ticks must join the signed UTC window and measured start/end duration.
Coherent replay outside the window, excessive clock skew and nonfinite/negative
durations reject. Model credentials and DSN are not put in argv/raw evidence.

## Actual host acceptance, then resource cleanup

Copy the raw/native artifact and actual named JUnit to a trusted host directory,
retaining `local-junit.xml` and the original signed `owner-preboot.json` there.
Before destroying the VM, run the separate `local_verify --help` gate with host
and owner public-key fingerprints pinned outside the guest, original hold files
and host-only private execution signing key. It rechecks the actual libvirt XML,
image bytes and disk metadata, original native transcript/runtime bytes, named
JUnit and every lifecycle oracle. It signs the actual host gate exclusively.
A guest-created gate JSON or guest-chosen key is not host acceptance.

App/private-root cleanup is not VM/model restoration. After actual bounded VM
removal and model-owner release, run `local_cleanup --help`. It requires the
complete closed signed prior host gate, the exact domain/private host root absent, an independently
pinned owner-signed source/run/VM/reservation release, and original restoration
evidence. It joins source/run/VM/reservation and the full actual-host observation,
then rechecks retained JUnit, native bytes and original preboot digests. It does
not try to observe a VM already destroyed. Partial, unknown or inconsistent
signed fields reject. It performs no destroy, service, reservation-release or cleanup action.
Preserve partial evidence and uncertain resources for their owner; never release
a hold while upstream completion/ownership is unknown. Independent review of the
actual host packet, JUnit and post-cleanup/restoration receipt remains mandatory.
