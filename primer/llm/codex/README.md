# ChatGPT plan model bridge foundation

The provider registry routes an explicitly bound `codex` row to a provisioned
model broker. The broker owns credential handling and signed-policy admission.
The offline harness replays synthetic data. A live session through the installed
Codex CLI remains a separate verification requirement.
See the [host binding and deployment prerequisites](broker/HOST.md).

## Architecture decision

The provider uses an executor-free **Sign-in-with-ChatGPT Responses adapter**.
The broker holds the dedicated account's credentials. Primer supplies the
conversation history, executes authorized tool calls, and returns their results
on the next model step.

Official documentation fetched on 2026-11-21:

- [Plan usage overview](https://developers.openai.com/siwc/token-sharing-open-source):
  open-source/local applications can request plan usage through their own
  registration; it is not permission to copy a personal Codex login. Host IDs
  are stable opaque installation identifiers, not credentials.
- [Sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in):
  the direct flow starts with `dynamic_agent_client`, retains the issued client
  ID, and validates state, PKCE, signed ID-token identity/issuer/audience/expiry/
  nonce, and granted scopes. Plan scopes include `resource.invoke` and
  `chatgpt.tokens.use.direct`; refresh uses `offline_access`. No partner API key
  or client secret is required by this documented flow.
- [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference):
  the supported endpoint is `https://api.openai.com/v1/responses`, with the
  selected account's OAuth bearer token. Discovery uses `/v1/models` with the
  same identity and the documented `models`/`visibility`/`slug` fields. Neither
  a bundled catalog nor a successful list proves a successful inference.
- [Preview limits](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations):
  HTTP requests use full input replay, `store: false`, `stream: true`, developer
  messages rather than explicit system items, and namespaced function tools.
  `max_output_tokens`, temperature, top-p, persistent conversation references,
  and several hosted tools are unsupported. This foundation refuses an output
  token cap rather than silently pretending it was enforced.

These are documentation facts, not executed account/entitlement proofs. The
endpoint is not a ChatGPT `backend-api` URL. No API-key fallback is implemented.
The separate credential-owning broker implements this flow. Primer's adapter
receives model data through the peer-verified socket.
Its deployment and genuine authentication gates remain open.

The current released Codex source was also inspected at tag `rust-v0.171.0`,
commit `ccf9be8cc458bd6b8336fe84d68d8893f2691a5e`. In
[spec_plan.rs](https://github.com/openai/codex/blob/ccf9be8cc458bd6b8336fe84d68d8893f2691a5e/codex-rs/core/src/tools/spec_plan.rs#L1485)
`ApplyPatchHandler::new` is evaluated before registry admission; the
[registry](https://github.com/openai/codex/blob/ccf9be8cc458bd6b8336fe84d68d8893f2691a5e/codex-rs/core/src/tools/registry.rs#L540)
accepts an already-constructed handler. This is a source observation, not proof
that that constructor executes a patch. It means the stock registry filter is
not a literal pre-constructor ceiling. No production binary is approved here.

### Installed CLI qualification for 0.171.0

The inspected Linux x86_64 musl executable reported `codex-cli 0.171.0`
and had SHA-256 `b76a027d6b210f9a8da7aaf321dd235c700f0ef7a794d4a5314b9af6c134f0f0`.
This fingerprint identifies the sampled bytes. The source references below
use the matching release tag. The inspection used empty private home and
configuration directories for CLI help and schema generation.

- **App-server threads.** `thread/start.dynamicTools` adds caller tools.
  Sandbox, approval and config settings are independent of the tool allowlist
  supplied through the host's `ExtensionDataInit`. Ordinary threads default to
  normal tool selection. Startup also constructs hooks and installs the MCP
  runtime. Even the built-in Guardian reviewer permits native command and image
  handlers. A successful dynamic call alone cannot establish exclusive tool
  ownership through startup and refresh.
  [Tool policy](https://github.com/openai/codex/blob/ccf9be8cc458bd6b8336fe84d68d8893f2691a5e/codex-rs/ext/extension-api/src/tool_policy.rs#L6-L29)
  · [Session startup](https://github.com/openai/codex/blob/ccf9be8cc458bd6b8336fe84d68d8893f2691a5e/codex-rs/core/src/session/session.rs#L1682-L1702)
  · [Dynamic registration](https://github.com/openai/codex/blob/ccf9be8cc458bd6b8336fe84d68d8893f2691a5e/codex-rs/core/src/tools/spec_plan.rs#L1658-L1671)
  · [Reviewer policy](https://github.com/openai/codex/blob/ccf9be8cc458bd6b8336fe84d68d8893f2691a5e/codex-rs/ext/guardian-reviewer/src/settings.rs#L44-L55)
- **Thread sampling.** `thread/sample` requires `threadId`, `expectedTurnId`
  and `prompt`. Its `disableMcp` option applies only to inherited incremental
  tools on Responses Lite. A tool attempt produces null `text`. The response
  omits the tool name, arguments and call ID needed for dispatch.
- **Discovery.** `model/list` and `modelProvider/capabilities/read` return
  metadata. Neither accepts a conversation and tool definitions for inference.
- **Internal Responses proxy.** `responses-api-proxy` forwards a Responses
  HTTP POST. Its credential inputs are an API key on stdin or GitHub workload
  identity. It provides no Codex plan-login lifecycle or app-server dynamic-tool
  protocol.
  [Command dispatch](https://github.com/openai/codex/blob/ccf9be8cc458bd6b8336fe84d68d8893f2691a5e/codex-rs/cli/src/main.rs#L1943-L1951)
  · [Authentication](https://github.com/openai/codex/blob/ccf9be8cc458bd6b8336fe84d68d8893f2691a5e/codex-rs/responses-api-proxy/src/lib.rs#L92-L107)

To check the protocol surface on this release, run
`codex app-server generate-json-schema --experimental --out <scratch-dir>`
with an empty private `HOME` and `CODEX_HOME`. Inspect the 189 methods in
`ClientRequest.json` and the request and response schemas under `v2/`.
This operation requires no sign-in or running server.

A CLI transport still needs an identifiable build or entry point that enforces
the Primer-only tool set before native initialization and across configuration
refresh, accepts the dedicated account's managed policy, and returns structured
tool calls to Primer. Verify it with a full result-replay turn through the
normal Primer factory, including refusal on policy loss. This release inspection
did not establish such an entry point or an end-to-end Primer session.

## What executes

`prepare_request` snapshots input and tool schemas, maps canonical Primer tool
IDs to opaque function aliases, preserves paired tool history, and rejects
unpaired/duplicate calls. Historical tools are not re-advertised implicitly.
Text-only replay and structured-output request construction are implemented;
media and advanced reasoning/history fidelity remain later work.

`ModelOnlyBridge` validates the closed request shape and authoritative policy
before invoking its synchronous model-channel constructor. It contains no
shell, patch engine, MCP client, hook/plugin/snapshot loader, config discovery,
process launcher, network endpoint selection, auth reader, or refresh helper.
The offline harness exposes only status and one complete stateless step; it
never forwards arbitrary app-server RPCs. Unsupported config/RPC/native-tool
requests are rejected before channel construction. `open_app_server` always
refuses, even for a recognized upstream version.

A policy snapshot is immutable, expires, binds an opaque account and model,
and refuses suspended/missing policy and all mandatory native effects. Every
received event rechecks it; any refresh/revision/account change cancels the step
instead of silently broadening capability. The policy resolver is a trusted
code dependency, NOT a caller-provided attestation. The default live resolver
is unavailable and fails closed. The broker's signed-policy implementation needs
authenticated administrator keys and a complete authority inventory before live
use. The synthetic harness's `RecordedPolicy` is
not an administrator-policy implementation and cannot authorize a live model.

One fresh channel serves each step. A channel receives only the snapshotted
request, not other steps' history. Text/reasoning stream incrementally. Complete
function calls are correlated with the terminal response, checked against the
step's namespace, tool IDs and choice, and offered to Primer only AFTER channel
close succeeds. Each terminal event rechecks current policy and closure after
consumer suspension; authorization linearizes immediately before that event's
yield, not once for the entire call batch. Previously delivered events cannot
be retracted, and Primer must still authorize tools at execution time. There is
no tool-execution callback inside the bridge. Unknown
native outputs cause failure as defense in depth, not the prevention boundary.
Usage and exactly one successful terminal event follow; truncated streams,
changed calls and failed cleanup cannot report success.

Deadline, capacity, output-size and event-loop checks are executable. Channel
cleanup survives repeated caller cancellation. Bridge and adapter own retained
stream objects, including idle streams advanced by completed temporary tasks.
Closing the adapter closes those scopes and releases their rate slots. Only one
advance per iterator is allowed. Trusted callbacks cannot recursively close their
own advancing or finalizing owner, including from child cleanup tasks. Task-local
ownership ancestry is checked before joining an existing cleanup task, so a
callback cannot join the ancestor waiting for it. Refusal unwinds normal cleanup;
external concurrent closes still join one cleanup operation. Ancestry is reset
before returning each event to the consumer. Hard-process-bounded regressions
cover recursive bridge, adapter and source-finalizer close, including registry,
channel, rate-slot and pending-task checks. The
absolute step deadline includes consumer idle time and is checked on the next
advance; owners must explicitly close abandoned idle streams. No timeout context
spans an async-generator yield. Timeouts rely on cooperative
Python transport/authority implementations: this is not an OS sandbox, does
not preempt a blocking Python constructor, and cannot kill an implementation
that swallows cancellation forever. The broker bounds IPC. Hard process
deadlines still require an OS-isolated service with a supervisor that can
terminate and reap it.
`CodexFoundationLLM` supplies the Primer stream shape and rate-limit ownership;
error classification is deliberately generic and sanitized for this first slice.

## Executable verification

Run the narrow tests serially:

```sh
python -m pytest tests/llm/codex -o addopts= -n0 -p no:cacheprovider -q
```

The suite drives the actual bridge, adapter and separate Python harness process.
It covers construction canary positive/refusal controls, native config and RPC
escapes, policy refresh, concurrent sessions, malformed/truncated/parallel tool
responses, repeated cancellation, cleanup failure/timeout and rate-slot release.
An audit-hook subprocess test runs the harness under actual file-write,
process-launch and socket tripwires, with an active-tripwire positive control.
This qualifies tested OFFLINE paths only; synthetic events are not recordings
from a real Codex model. Python/dependency imports remain trusted code.

The harness can be driven independently with a JSON event-list transcript:

```sh
python -m primer.llm.codex.harness --transcript /private/synthetic-events.json
```

Its newline JSON input accepts `{"id":1,"method":"bridge/status","params":{}}`
or `step/start` with exactly `request`, `aliases`, and `config`. Replies always
say `mode: offline`; there is no live mode switch. It reads bounded local
transcript/input data, writes only protocol output, and owns no child process.

## Next slice and remaining gates

1. Independently review this actual source and tests. Pin the accepted package
   and dependency identity; a hash or a runtime's own capability flag alone is
   not an authority grant.
2. Independently review the credential/policy broker and
   its offline OAuth, storage, policy, HTTP and peer-identity tests. Then wire
   trusted host supervision, genuine authority inventory, separate OS identity,
   callback listener ownership and credential management before deployment. Honor
   every applicable administrator requirement; incompatible mandatory native
   actions refuse. Passing signature tests does not establish who the real
   administrators are or authorize disabling host policy.
3. Actual dedicated user sign-in/consent, authorized administrator policy and
   entitlement are external facts. No such identity was read or used here.
   Obtain genuine model/tool/usage/error/refresh/cleanup evidence; never borrow
   personal auth. Missing login does not block the offline implementation work.
4. Verify the provider factory, saved-provider discovery, profile validation
   and console import with hosted CI. The [host guide](broker/HOST.md) covers
   the binding and registration steps. An installed-CLI session still requires
   a qualified transport and its own end-to-end tool replay. Full response
   fidelity and error/retry semantics also remain open.

Original semantic mapping: ephemeral app-server threads become fresh stateless
Responses steps; raw response completion becomes `response.completed`; held
Codex dynamic calls become passive namespaced function outputs; Primer still
executes tools and supplies their results on its next request. App-server
resume/fork, native model tools and automatic prompt injection are not exposed.
Discovery must come from the broker's selected authenticated account. These
changes need review as an explicit architecture amendment, not a compatibility
claim about the stock CLI.
