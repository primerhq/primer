# Codex as an LLM provider

Version: 1 (2026-11-23; approved by the operator)

## 1. Purpose

An operator who pays for a ChatGPT plan runs primer's agents on that plan. Primer drives the OpenAI Codex CLI, installed
and logged in on the worker host, as a model backend: Codex produces text, reasoning and tool calls, and primer runs its
own agent loop and its own tools. Codex never executes a tool. The provider sits in the console's "Agent runtimes"
class next to `claude_code`, and under the hood it plugs into the same `LLM.stream()` machinery, retries, model profiles
and rate limits as every other LLM provider.

This document is written to be built on a host where Codex and a ChatGPT login exist; the drafting host has neither.
Every statement about Codex is quoted from the `openai/codex` source at tag `rust-v0.169.0` (commit `d32063f`) or from
the official docs, and every behaviour not confirmed there is listed in section 11 as a check for the first live run.

Primer's main branch holds an unregistered package at `primer/llm/codex/` (PR #1066) that calls OpenAI's
"Sign in with ChatGPT" Responses endpoint through a separate credential broker. It is not this design: the operator
wants the Codex CLI already installed and logged in on the machine, used the way `claude_code` uses `claude`
(operator's call, 2026-11-23). The build removes that package, its tests and its documentation paragraphs, and writes
the provider below in the same package path.

## 2. Goals, non-goals and anti-goals

Goals:

- `LLMProviderType.CODEX` (`"codex"`), shown in the "Agent runtimes" console class with `claude_code`.
- Text, reasoning summaries, parallel tool calls, structured output, usage and errors map onto primer's stream events
  and error classes, so agents, graphs, compaction and aggregated profiles work unchanged.
- Each model step sees exactly the history primer replays for it, and nothing from any other session or agent.
- The operator's own ChatGPT login is used; primer never sees the credentials.
- The provider is used like any other LLM provider: admin-created, private by default, shared through the existing
  access model (the operator's decision for agent runtimes, carried over from `claude_code`).

Non-goals:

- Codex's own tools (shell, file edits, web search, image generation, MCP servers, plans) and its sandbox.
- API-key authentication through Codex. An OpenAI API key is served better by primer's native OpenAI providers.
- Codex's session history, resume and rollout files.
- Keeping a Codex turn alive across primer steps (section 5).
- Calling OpenAI's endpoints from primer, holding ChatGPT tokens in a broker, or any other sign-in than the Codex CLI's
  own (the PR #1066 approach).

Anti-goals (a build that does any of these fails, even if it works):

- Codex executes a tool of any kind: a shell command, a file change, a web search, an MCP call, or code in its code-mode
  runtime. Primer detects any such item and fails the step (section 6.6).
- Primer reads, copies, stores or forwards the ChatGPT credentials or `auth.json`.
- A model step sees history from another session, agent or step.
- A Codex process outlives the worker that started it while that worker is alive, or leaks threads without bound.
- The child sees primer's environment (database credentials, API keys, encryption key).

## 3. Constraints and platform facts

Read at `openai/codex` tag `rust-v0.169.0` (commit `d32063f`, published 2026-11-13); paths are under `codex-rs/`.
"Inferred" marks a reading of code that section 11 confirms on a live host.

| Fact | Source | Consequence |
|---|---|---|
| `codex app-server` speaks JSON-RPC over stdio, newline-delimited; one `initialize` request then an `initialized` notification per connection | `cli/src/main.rs:601-612`, `app-server-protocol/src/protocol/v1.rs:29-75`, docs | one stdio JSON-RPC client per process |
| Experimental methods and fields need `capabilities.experimentalApi: true` in `initialize`, otherwise "`<reason>` requires experimentalApi capability" | `app-server-protocol/src/experimental_api.rs` | the client opts in |
| `codex exec` refuses client tools: "dynamic tool calls are not supported in exec mode" | `exec/src/lib.rs:2288` | the driver is the app-server |
| One app-server hosts many threads; requests are serialised per thread; each thread has its own history (inferred) | `app-server/src/request_serialization.rs` | a fresh thread per step isolates history |
| `thread/start` takes `model`, `cwd`, `approvalPolicy`, `sandbox`, `config` (overrides), `baseInstructions`, `developerInstructions`, `ephemeral`, `dynamicTools` (experimental), `experimentalRawEvents` | `app-server-protocol/src/protocol/v2/thread.rs:64-224` | one call configures a step's thread |
| `ephemeral: true`: "session is not persisted on disk"; `history.persistence = "none"` stops `history.jsonl` | `core/src/config/mod.rs:1045`, `config/src/types.rs:237` | no rollout or history files |
| `thread_unload_delay_secs`, set per thread through `thread/start.config`, `0` "unloads immediately" once a thread has no subscribers and no activity; `thread/unsubscribe` exists; unload cancels pending requests and sends `thread/closed` | `config/src/config_toml.rs:350-353`, `protocol/common.rs:606-620`, `app-server/src/request_processors/thread_lifecycle.rs:660-680` | a step's thread is gone right after the step |
| A dynamic tool spec is `{"type":"function","name","description","inputSchema"}` or `{"type":"namespace","name","description","tools":[...]}` | `protocol/src/dynamic_tools.rs` | primer's tools go in one namespace |
| Codex asks the client to run a dynamic tool with the server request `item/tool/call {threadId, turnId, callId, namespace, tool, arguments}` and waits with no timeout until it is answered, the turn is interrupted or the request is cancelled | `protocol/common.rs:1930`, `v2/item.rs:1818`, `core/src/tools/handlers/dynamic.rs` | primer never answers it; the step interrupts the turn |
| Tool calls start while the response still streams; `thread/tokenUsage/updated` is sent only after every pending tool resolves ("Emit token counts only after pending tools resolve") | `core/src/stream_events_utils.rs`, `core/src/session/turn.rs:3791-3810` | usage cannot mark the end of a response with tool calls |
| With `experimentalRawEvents: true`, `rawResponseItem/completed` carries each recorded response item (a `function_call` before its tool starts) and `rawResponse/completed {responseId, usage}` marks the end of the response stream; both are "internal-only" | `core/src/session/mod.rs:4189`, `app-server/src/request_processors/thread_lifecycle.rs:552-568` | the reliable end-of-response signal; pin the version |
| `thread/inject_items {threadId, items}` takes raw Responses API items (`message`, `function_call`, `function_call_output`, `reasoning`, ...); an empty list is rejected; allowed before the first turn | `protocol/common.rs:939`, `protocol/src/models.rs:1096-1367`, `core/src/codex_thread.rs` | history is replayed as exact items, not text |
| `turn/start` accepts `input: []` on an idle thread and runs the model on the existing history; orphan tool outputs are dropped and unanswered calls get a synthesized "aborted" output | `app-server/src/request_processors/turn_processor.rs:594-603`, `core/src/session/turn_input.rs:405-505`, `core/src/context_manager/normalize.rs` | a tool round tail continues with empty input |
| `turn/start` also takes `effort`, `summary`, `outputSchema` (current turn only); `turn/interrupt {threadId, turnId}` ends the turn `interrupted` | `v2/turn.rs:181-321` | per-step parameters |
| Streaming: `item/agentMessage/delta`, `item/reasoning/summaryTextDelta`, `item/reasoning/textDelta`; items `agentMessage`, `reasoning`, `dynamicToolCall`, `commandExecution`, `fileChange`, `mcpToolCall`, `webSearch`, `imageView`, `imageGeneration`, `collabAgentToolCall`, `contextCompaction`, `plan` | `protocol/common.rs:2052-2131`, `v2/item.rs` | what the translator reads and what the guard refuses |
| Errors carry `codexErrorInfo`: `contextWindowExceeded`, `usageLimitExceeded`, `rateLimitExceeded`, `serverOverloaded`, `unauthorized`, `modelNotFound`, `badRequest`, `internalServerError`, `httpConnectionFailed`, `responseStreamDisconnected`, ...; the `error` notification has `willRetry` | `v2/shared.rs:76-133` | a direct mapping onto primer's error classes |
| The shell tool is removed by `features.shell_tool = false`; web search by `web_search = "disabled"`; view image, sleep, image generation, request-user-input, apps, plugins, tool suggestions and multi-agent by their feature flags; `apply_patch` has no config key (catalog field `apply_patch_tool_type`) | `core/src/tools/spec_plan.rs:1289-1320, 1473`, `core/src/config/mod.rs:4022` | most tools are switched off; the rest are guarded (section 6.6) |
| Every bundled model except `gpt-5.5` has `"tool_mode": "code_mode_only"`; `gpt-5.5` has no tool mode and is direct only while the `code_mode` and `code_mode_only` features are off (their default): client tools are reachable only through Codex's JavaScript `exec` tool in its embedded V8 runtime; no config key changes `tool_mode`; `[features.code_mode] direct_only_tool_namespaces` keeps named namespaces as direct tools; a `model_catalog_json` file replaces the catalog and stops the remote refresh; `model/list` does not report a model's tool mode | `models-manager/models.json`, `app-server-protocol/src/protocol/v2/model.rs:121-154`, `core/src/tools/mod.rs:65-75`, `features/src/feature_configs.rs:56-60`, `model-provider/src/provider.rs:714-720` | section 6.6 and the model rule in 6.1 |
| `baseInstructions` (or `model_instructions_file`) replaces Codex's 17 to 22 thousand character prompt; `include_permissions_instructions`, `include_environment_context`, `include_apps_instructions` can be `false`; `project_doc_max_bytes = 0` stops AGENTS.md loading | `models-manager/src/model_info.rs:43-46`, `core/src/agents_md.rs:65` | the model sees primer's prompt, not Codex's persona |
| Config, profiles and MCP servers load from `CODEX_HOME` (default `~/.codex`); `/etc/codex/config.toml` and managed layers still apply; the ChatGPT login is `$CODEX_HOME/auth.json` | `utils/home-dir/src/lib.rs`, `config/src/loader/README.md`, `login/src/auth/storage.rs:168` | primer uses the machine's Codex home and login by default; a separate home is optional |
| Processes sharing one `CODEX_HOME` coordinate credential writes with `auth.json.lock` and `auth.lock`; two simultaneous refreshes can still hit `refresh_token_reused`, recovered by reloading `auth.json` (inferred) | `login/src/auth/storage_commit.rs`, `login/src/auth/manager.rs:1757-1802, 2172` | worker processes on one host may share the home |
| `codex login status` writes to stderr; "Logged in using ChatGPT" exits 0, "Not logged in" exits 1; `account/read` returns `{account: {type:"chatgpt", email, planType, ...}|null}` | `cli/src/login.rs:488-552`, `v2` account types | the host status check |
| `model/list` returns `{data: [{id, model, displayName, hidden, supportedReasoningEfforts, defaultReasoningEffort, inputModalities, isDefault, ...}]}`; efforts include `none, minimal, low, medium, high, xhigh, max, ultra`, each model a subset | `v2` model types | model discovery and effort clamping |
| App-server docs: "App-server authentication has never been permitted for commercial or hosted services." and "If you've built a local or open-source application using Codex app-server authentication, you can continue using it" | `learn.chatgpt.com/docs/app-server` | primer is self-hosted by the plan's owner; the owner decides (section 6.12) |
| Measured by AionCore: a 4.5 to 6.5 s cold start and about 9.7k tokens of fixed overhead with Codex's default prompt | `iOfficeAI/AionCore` `codex_title.rs` | a long-lived process per worker; `baseInstructions` replaces the prompt |

## 4. Prior art

- The `claude_code` provider (`primer/llm/claude_code/`, spec `2026-11-14-claude-code-llm-provider-design.md` v1.7):
  per-step `claude -p`, a local MCP bridge that holds tool calls, history replayed as text, the "Agent runtimes" class,
  host status checks and the crash sweep. Its process hygiene, history split, status pattern and console class carry
  over; its MCP bridge does not, because Codex runs client-defined tools through the app-server protocol itself.
- AionUi's backend (`iOfficeAI/AionCore`) drives `codex app-server` and measured about 9.7k tokens of fixed overhead
  and a 4.5 to 6.5 s cold start when using it as a plain LLM (`codex_title.rs`). It never disables Codex's tools; it
  gates them with permission prompts.
- PR #1066 (`primer/llm/codex/`, merged 2026-11-22, unregistered): an HTTPS client for the "Sign in with ChatGPT"
  Responses endpoint with a credential broker under a separate OS user. Removed by this design (section 1). Its idea of
  one `primer` tool namespace with hashed aliases matches section 6.4; no code is kept.

## 5. Approaches

- **`codex exec --json` per step.** One process per step, JSONL events. Ruled out: exec refuses client tools ("dynamic
  tool calls are not supported in exec mode", `exec/src/lib.rs:2288`), streams text only as completed items, and would
  need the MCP bridge back.
- **`codex app-server`, one process per step.** Isolation by process, like `claude_code`. Costs a cold start on every
  model step (4.5 to 6.5 s measured by AionCore) and Codex's process start-up work each time.
- **`codex app-server`, one long-lived process per worker process, a fresh ephemeral thread per step.** One app-server
  hosts many threads, each with its own history; requests are serialised per thread
  (`app-server/src/request_serialization.rs`). A step pays a thread start, not a process start, and steps on one worker
  run concurrently in separate threads.
- **Keep the Codex turn alive across primer steps.** Codex waits on a client tool call with no timeout, so primer could
  answer it with the real result in the next step. Rejected: a primer step can resume hours later, on another worker or
  after a restart (approvals, parks), and the held turn would pin one process and lose everything on a crash.

Chosen: one long-lived app-server per worker process with a fresh ephemeral thread per step (drafter's call). It keeps
the per-step isolation the operator asked for in `claude_code` without paying a process start per step, and it uses
one process per worker rather than one per agent or session.

## 6. Design

### 6.1 Provider type and configuration

- `LLMProviderType.CODEX = "codex"`, added to `AGENT_RUNTIME_TYPES` (`primer/model/providers/llm.py`).
- `CodexConfig`:
  - `binary_path: str = "codex"`, resolved on `PATH` like `ClaudeCodeConfig.binary_path`.
  - `codex_home: str | None = None`. Unset, the child gets no `CODEX_HOME`, so the CLI uses its own default home
    (`~/.codex`) and the Codex login the operator already made on the machine with `codex login`, the way
    `claude_code` uses the machine's `claude` login (operator's call). Set, it points the CLI at another home that has
    its own login (`CODEX_HOME=<codex_home> codex login`). Either way the CLI reads the credentials; primer never
    opens `auth.json`. The operator's config, profiles, MCP servers and instructions in that home are kept out of
    primer's steps by the overrides in section 6.2 and the guard in section 6.6.
  - `pass_env: list[str]`, the same allowlist extension and secret-name screen as `ClaudeCodeConfig.pass_env`.
  - `model_catalog_path: str | None = None`, an optional Codex model catalog file the operator supplies to make more
    models direct (section 6.6). Passed as `-c model_catalog_json=<path>`; when set, Codex stops refreshing its catalog
    from the service and uses only this file.
- Limits ride the existing provider limits (`max_concurrency`, the timeouts, retries); the console class shows the
  same limit fields as `claude_code`.
- Primer stores no credential for this provider.

### 6.2 The app-server process

- One `codex app-server` process per worker process and provider, started by the first step that needs it and kept
  for later steps (section 5). It is keyed by provider id and the effective configuration; a provider change through
  the existing registry invalidation retires the old process: new steps start a new one, the old one closes when its
  last step ends.
- Command: `<binary_path> app-server --listen stdio://` followed by these `-c` overrides (drafter's call; each key is
  quoted in section 3):
  - `features.shell_tool=false`, `web_search="disabled"`, `features.view_image=false`, `features.sleep_tool=false`,
    `features.image_generation=false`, `tools.experimental_request_user_input.enabled=false`, `features.apps=false`,
    `features.plugins=false`, `features.tool_suggest=false`, `features.multi_agent=false`;
  - `features.code_mode=false`, `features.code_mode_only=false` (so a model without a catalog tool mode stays direct
    even if a managed config turns them on), and `features.code_mode.direct_only_tool_namespaces=["primer"]`;
  - the model provider's stream and request retry counts set to `0`, so Codex reports a dropped stream instead of
    retrying it after primer has already streamed part of the response (the key names are a live check, section 11);
  - `mcp_servers={}`, so MCP servers from the operator's config are not started (an inferred key, section 11);
  - `project_doc_max_bytes=0`, `history.persistence="none"`, `include_permissions_instructions=false`,
    `include_environment_context=false`, `include_apps_instructions=false`;
  - `model_catalog_json=<model_catalog_path>` when set.
- Environment: the `claude_code` allowlist (`HOME`, `PATH`, `USER`, `LOGNAME`, `LANG`, `TERM`, `SHELL`, `TMPDIR`), the
  provider's `pass_env`, and `CODEX_HOME=<codex_home>` only when `codex_home` is set (`HOME` lets the CLI find its
  default home otherwise).
- Working directory and record: a primer-owned temporary directory `primer-codex-<worker pid>-<random>` (mode 0700)
  holding `child.json` (pid and process group) and an empty `cwd/` subdirectory that every thread uses as its `cwd`.
  The child runs in its own process group with the parent-death signal on Linux, exactly as `claude_code` children do.
- Handshake: `initialize {clientInfo: {name: "primer", title: "primer", version: <primer version>}, capabilities:
  {experimentalApi: true}}`, then `initialized`.
- The client is a JSON-RPC multiplexer bound to the event loop that started it (the `claude_code` bridge's loop rule):
  it matches responses to request ids, routes notifications and server requests to the step that owns their
  `threadId`, and answers any server request that names no live step with a JSON-RPC error.
- The process ends on provider retirement, on the registry's `aclose`, and on worker shutdown: close stdin, wait a
  short grace period, then SIGKILL the process group and delete the directory. If the process exits or stops reading
  while steps are in flight, each of those steps fails with a retryable error and the next step starts a new process.

### 6.3 One model step

1. Acquire the provider's rate-limit slot (`llm:<provider_id>`), as every adapter does.
2. Get or start the worker's app-server (section 6.2).
3. `thread/start {model, cwd, ephemeral: true, experimentalRawEvents: true, sandbox: "read-only", approvalPolicy:
   "untrusted", baseInstructions: <primer system prompt>, dynamicTools: [<primer namespace>], config:
   {thread_unload_delay_secs: 0}}`. `dynamicTools` is omitted when the step has no tools or `tool_choice` is `none`.
4. `thread/inject_items {threadId, items}` with the replayed history (section 6.5), skipped when it is empty.
5. `turn/start {threadId, input, effort, summary, outputSchema}`: `input` is the tail user message's parts, or `[]`
   when the tail is a tool round (section 6.5).
6. Translate notifications into stream events (section 6.7) until one of these ends the step:
   - `rawResponse/completed` arrives after one or more primer `function_call` items: the response is complete and
     every call of it is known. Emit `Usage` from its `usage`, then `Done(stop_reason="tool_use")`.
   - `turn/completed` with status `completed` and no primer call: emit `Usage` and `Done(stop_reason="stop")`.
   - A guard violation (section 6.6), an error (section 6.8), primer Stop, cancellation or a timeout.
7. In every exit path: if the turn is still running, `turn/interrupt`; then `thread/unsubscribe`, so the thread
   unloads at once (`thread_unload_delay_secs` 0) and Codex cancels its pending `item/tool/call` requests. Primer never
   answers an `item/tool/call` request. Release the rate-limit slot.
8. Yield exactly one terminal event; after it, drop and log anything else the thread sends.

A step costs one thread start and one model response, never a process start after the first.

### 6.4 Tools

- Primer's tools are one dynamic namespace: `{"type": "namespace", "name": "primer", "description": "Tools of this
  primer agent.", "tools": [{"type": "function", "name": <alias>, "description": <Tool.description>,
  "inputSchema": <Tool.args_schema>}]}`.
- `<alias>` is the scoped tool id (`toolset__tool`) when it fits 64 characters and matches `[A-Za-z0-9_-]`; otherwise
  the first 55 characters with other characters replaced by `_`, then `_` and 8 hex characters of the SHA-256 of the
  id. A per-step map turns aliases back into tool ids on `ToolCallStart` (the `claude_code` alias rule with the
  Responses API limit).
- A call is read from the `function_call` item in `rawResponseItem/completed`: `call_id` becomes the primer call id,
  `name` the tool id, `arguments` (a JSON string) the call's arguments. Several calls in one response are a parallel
  tool round.
- The `item/tool/call` request that follows is held unanswered until the step ends (section 6.3, step 7).

### 6.5 History replay

- Primer's system messages, and the agent's system prompt, become `baseInstructions`. Codex's own prompt is replaced,
  not appended to.
- The tail is the last user message, or the last tool round (an assistant message with tool calls and the tool
  results that answer it). Everything before the tail is injected; a tail that is neither raises
  `BadRequestError("history must end with a user or tool message")`, as in `claude_code`.
- Injected items, in order, as Responses API items:
  - a user message: `{"type": "message", "role": "user", "content": [{"type": "input_text", "text": ...},
    {"type": "input_image", "image_url": "data:<mime>;base64,..."}]}`;
  - an assistant message's text: `{"type": "message", "role": "assistant", "content": [{"type": "output_text",
    "text": ...}]}`;
  - an assistant tool call: `{"type": "function_call", "namespace": "primer", "name": <alias>, "arguments":
    <JSON string, sorted keys>, "call_id": <id>}`;
  - a tool result: `{"type": "function_call_output", "call_id": <id>, "output": <text>}`; a failed result's text
    starts with `[error] `, because the item has no error flag (drafter's call).
- A call id that an earlier round already used is renamed with the `claude_code` `replay_tool_id` rule, so every call
  and output pair stays unique.
- Reasoning from earlier steps is dropped: primer holds no Codex `encrypted_content` to replay (drafter's call).
- A user-message tail becomes the `turn/start` input (`{"type": "text", "text": ...}` and `{"type": "image", "url":
  "data:..."}`); a tool-round tail is injected with the rest and the turn starts with `input: []`.
- Media other than images, and images over the input limits, become a text placeholder naming the media type.

### 6.6 Codex runs no tool

Three layers, so the anti-goal holds even when a Codex release changes its defaults:

1. **Off by configuration.** The overrides in section 6.2 remove the shell, web search, images, plans, apps, plugins,
   multi-agent and MCP tools (`mcp_servers={}` drops the operator's servers). `sandbox: "read-only"` and
   `approvalPolicy: "untrusted"` cover anything left.
2. **Direct models only.** Only models that call tools directly are used (the rule below), so Codex's JavaScript
   `exec` and `wait` tools are never registered. `direct_only_tool_namespaces = ["primer"]` stays set as a second
   line in case a model's mode changes under a new Codex release.
3. **The guard.** The step watches what Codex does and fails at the first sign of execution:
   - a `rawResponseItem/completed` item that is a `function_call` outside the `primer` namespace, a
     `custom_tool_call`, a `local_shell_call`, a `web_search_call` or any other tool-call type;
   - an `item/started` of type `commandExecution`, `fileChange`, `mcpToolCall`, `webSearch`, `imageView`,
     `imageGeneration` or `collabAgentToolCall`;
   - any approval request (`item/commandExecution/requestApproval`, `item/fileChange/requestApproval`), which is also
     answered `{"decision": "cancel"}`.
   On a violation the step interrupts the turn, unsubscribes the thread and ends with a fatal
   `Error(code="codex_tool_violation")` naming the item type (drafter's call). It is not retried: the same model and
   prompt would do it again.

Only direct-tool models are used (user's call):

- A model is direct when Codex will hand it primer's tools as direct function tools and register no code runtime:
  - it is in `CODEX_DIRECT_MODELS` (`primer/llm/codex/models.py`), the models verified direct with the section 6.2
    overrides at the tested Codex version (`{"gpt-5.5"}` at `rust-v0.169.0`; the builder keeps it current from live
    checks 1 and 11), or
  - the provider has a `model_catalog_path` and that file's entry for the model has `"tool_mode": "direct"`. Primer
    reads only that operator-supplied file, never Codex's own cache.
- Creating or updating a model profile on a `codex` provider with a model that is not direct is refused with a 422
  problem naming the rule. The adapter checks again at the start of every step and raises `ConfigError` before any
  Codex request, because a catalog file can change after a profile was saved.
- `gpt-5.5`'s bundled entry still registers Codex's `apply_patch` tool (`apply_patch_tool_type: "freeform"`). With a
  read-only sandbox and the `untrusted` approval policy, a patch attempt raises an approval request, which the guard
  cancels and fails the step on; a catalog file with `"apply_patch_tool_type": null` removes the tool (drafter's
  call to accept the guard for the bundled entry).

### 6.7 Event mapping

| Codex | Primer |
|---|---|
| `item/agentMessage/delta` | `TextDelta` |
| `item/reasoning/summaryTextDelta` | `ReasoningDelta` (summary text) |
| `item/reasoning/textDelta` | dropped (drafter's call) |
| `rawResponseItem/completed` with a primer `function_call` | `ToolCallStart`, one `ToolCallDelta` with the full arguments, `ToolCallEnd` |
| `rawResponse/completed` | `Usage` from its `usage`, then the end-of-response rule of section 6.3 |
| `turn/completed` status `completed` | `Usage` (from `thread/tokenUsage/updated` `last` when no raw usage was seen), `Done(stop)` |
| `turn/completed` status `interrupted` by primer | nothing (the step already ended) |
| `turn/completed` status `failed`, or `error` with `willRetry: false` | section 6.8 |
| `error` with `willRetry: true` | logged when nothing has been yielded yet; after an event has been yielded, the step interrupts the turn and ends with `Error(fatal=True, code="codex_stream_retried")`, because a retried response would repeat output (drafter's call) |
| `account/rateLimits/updated` | logged at DEBUG with the percent used and reset time |
| `contextCompaction` item | logged at WARNING; primer's own compaction should prevent it |

`Usage` maps input tokens, cached input tokens and output tokens; reasoning output tokens count as output.

### 6.8 Errors

| Condition | Primer |
|---|---|
| Binary missing or not executable | `ConfigError` naming the binary and the host, before any event |
| `codex_home` missing, or `account/read` returns no ChatGPT account | `AuthenticationError` naming the host and the `codex login` command, before any event |
| `codexErrorInfo` `unauthorized` | `AuthenticationError` |
| `usageLimitExceeded`, `rateLimitExceeded` | `RateLimitError`, so an aggregated profile fails over |
| `contextWindowExceeded` | `BadRequestError` before the first event, `Error(fatal=True, code="bad_request")` after it, keeping Codex's message, so primer's context-overflow handling recognises it |
| `modelNotFound`, `badRequest` | `BadRequestError` |
| `serverOverloaded`, `internalServerError`, `httpConnectionFailed`, `responseStreamDisconnected`, `responseTooManyFailedAttempts` | `ServerError` (retryable) |
| The app-server exits or stops answering; a request gets no response within `connect_timeout_seconds` | `TransientError(code="codex_app_server_failed")` with the last lines of its stderr |
| No notification for `request_timeout_seconds` while a turn runs | `ProviderTimeoutError` |
| A guard violation (section 6.6) | `Error(fatal=True, code="codex_tool_violation")` |
| Anything else | `Error(fatal=True)` through the existing `describe_failure` helper |

### 6.9 Parameters

| Primer parameter | Handling |
|---|---|
| `model` | `thread/start.model`; model ids come from `model/list` (section 6.11) |
| profile `reasoning` `off`, `minimal`, `low`, `medium`, `high` | `turn/start.effort` `none`, `minimal`, `low`, `medium`, `high` through a new `codex_extended(config)` in `primer/llm/_reasoning.py`, clamped to the model's `supportedReasoningEfforts` from `model/list`: the nearest lower supported value, else the lowest supported value (so `off` never gives more reasoning than `low`) |
| profile `extended` `reasoning_effort`, `reasoning_summary` | override the mapped effort and summary, as for the OpenResponses adapter, so an operator can ask for `xhigh` or `detailed` |
| reasoning summary | `turn/start.summary` `auto` when the profile reasoning is above `off`, else `none` |
| `tools`, `tool_choice` | the `primer` namespace; `tool_choice` `none` omits it, other values are ignored |
| `response_format` | `turn/start.outputSchema`; the reply is the JSON text and streams as `TextDelta` |
| `max_output_tokens`, `temperature`, `top_p`, `stop` | ignored, logged once per adapter at DEBUG (section 11 checks for an output cap) |
| `count_tokens` | raises `TokenCounterUnavailable`; compaction estimates from text, as for `claude_code` |

### 6.10 Process hygiene

- Every exit path of the app-server (section 6.2) kills its process group and deletes its directory.
- The `claude_code` crash sweep runs for `primer-codex-*` directories too: at the first `codex` adapter build in a
  process, directories whose worker pid is dead are swept with the same trust checks (our uid, mode 0700, no symlinks,
  `child.json` opened with `O_NOFOLLOW`, never our own process group).
- A step never leaves a thread loaded: `thread/unsubscribe` with `thread_unload_delay_secs` 0 unloads it, and Codex
  sends `thread/closed`. The client counts loaded threads; one still loaded 30 seconds after its step ended is logged at
  WARNING, and the process is recycled when the count passes 100 (drafter's call).

### 6.11 Host requirements, status and model discovery

- Minimum Codex version `0.169.0` (`codex --version`), the release this design was read at; the raw-response events
  the step relies on are internal-only, so the builder records the highest version verified live in
  `CODEX_MAX_TESTED_VERSION` and the status check warns, without refusing, above it (drafter's call).
- `runtime_status` for `codex` (the existing `GET /v1/llm_providers/{id}/runtime_status` route and its
  `llm_providers__get_llm_provider_runtime_status` tool) reports `installed`, `version`, `meets_minimum`,
  `logged_in` and `plan_type`: it runs `codex --version`, then a short-lived app-server with the provider's
  environment (the default home, or `codex_home`) for `initialize` and `account/read`, killing it on any outcome
  including cancellation.
- Model discovery (`POST /v1/llm_providers/_discover_models`, `GET /v1/llm_providers/{id}/discovered_models`) calls
  `model/list` on the same short-lived app-server and returns the non-hidden ids with their display names, reasoning
  efforts and input modalities, each marked `direct: true|false` by the rule in section 6.6. The operator sets the
  context length on the model profile.

### 6.12 Access and sharing

The provider is used like any other LLM provider: admin-created, private by default, shared and published through the
existing access model, with no warning and no special restriction. The plan's terms (section 3) are the provider
owner's responsibility, the rule the operator set for agent runtimes.

## 7. Console

- `PC_CLASSES.agent_runtime.kinds` becomes `["claude_code", "codex"]`; `PC_KIND_LABELS.agent_runtime.codex` is
  "Codex"; the `llm` class's `excludeKinds` gains `codex`.
- The register form for `codex` shows Binary path, Codex home (optional, help text "Leave empty to use this machine's
  Codex login"), Pass-through environment and Model catalog file.
- The status block shows version, minimum met, logged in and plan type; Import models uses discovery and offers only
  direct models, listing the others as unavailable with the reason.
- Every new control has an accessible name and the class keeps its mobile layout.

## 8. Agent tools and documentation

- No new route: `codex` rides the provider CRUD, status and discovery routes and their generated tools.
- `docs/wiki/providers/agent-runtimes.md` gains Codex: setup (install the CLI and run `codex login` on the worker
  host, or log in a separate home and set Codex home), what Codex
  replaces and disables, the `code_mode_only` model rule and the catalog file, and that every request runs on the
  plan of the login in that home.
- `docs/dev/subsystems/model-providers.md` gains the Codex adapter: the app-server process, the step, the guard and the
  error mapping. Its PR #1066 paragraph about the executor-free bridge and broker (lines 772-782 at `e51e36664`) and
  any #1066 wording in the wiki page are replaced.
- The operator site (`primerhq.github.io`) gains the same setup steps, as a follow-up in that repo.

## 9. Code layout

- The PR #1066 contents of `primer/llm/codex/` (`adapter.py`, `boundary.py`, `bridge.py`, `harness.py`,
  `lifecycle.py`, `request.py`, `README.md`, `broker/`) and `tests/llm/codex/` are deleted first; nothing outside the
  package imports them.
- `primer/llm/codex/`: `client.py` (JSON-RPC multiplexer), `server.py` (the per-worker app-server and its lifecycle),
  `history.py` (Responses item replay), `translate.py` (notifications to events, the guard), `status.py`, `adapter.py`
  (`CodexLLM`).
- Provider-agnostic pieces move out of `primer/llm/claude_code/` into `primer/llm/agent_runtime/`: the child
  environment allowlist, the process-group child and its record, the crash sweep (taking the directory prefix), the
  history tail split, the call-id renaming and the tool alias helper (taking the length limit). `claude_code` imports
  them from there, and its tests run unchanged (drafter's call).
- `primer/api/registries/provider_registry.py` builds `CodexLLM` for `LLMProviderType.CODEX`, sweeps once per process,
  and closes the app-servers from its `aclose`.

## 10. Testing

- **Recordings first.** On the Codex host the builder records the real wire with a small stdio proxy between primer
  and `codex app-server` (both directions, one JSONL line per message) for: a plain answer, one tool call, parallel
  tool calls, a tool-round tail continued with `input: []`, structured output, a usage-limit or context error if one
  can be provoked cheaply, and an interrupt. Fixtures are built from these recordings, with ids that name no live
  process.
- **A fake app-server** (`tests/llm/codex/fake_codex.py`) replays a fixture as a scripted JSON-RPC peer and records
  what the client sent, selected through `binary_path` like the `claude_code` fake.
- **Unit:** client multiplexing and loop binding; the process lifecycle and every teardown path (kill before
  directory removal, no leaked children, crash sweep); history items for every message shape, call-id renaming and the
  tail rule; the step's end conditions (tool calls, plain answer, structured output, interrupt, Stop, cancellation,
  stall and total timeouts) with exactly one terminal event; every guard violation; the error table; parameters and
  effort clamping; status and discovery.
- **E2E:** a `tests/e2e/` journey on the fake, like the `claude_code` journey: register, status, a session step with a
  held tool call run by primer, and the next step's answer; run in CI with the fake's environment.
- **Direct-model rule:** a profile with a non-direct model is a 422; a direct model passes; a catalog file entry with
  `"tool_mode": "direct"` makes a model direct; the adapter raises `ConfigError` at step start for a non-direct model.
- **Console:** the register form and status block for `codex`, the import list marking non-direct models, and the a11y
  sweep floor for the form.
- **Live smoke** (`tests/integration/test_codex_smoke.py`, gated by `PRIMER_TEST_CODEX=1`, never in CI): one tool
  round against the real login, then the leftover check: no `codex app-server` beyond the worker's own, no loaded
  thread, no `primer-codex-*` directory after shutdown.

## 11. Live checks on the Codex host

The builder runs these before relying on the behaviour they name, records each result in the PR, and changes the
design (through an amendment of this document) when one fails:

1. **No Codex tool reaches the model.** With the section 6.2 overrides and `gpt-5.5`, a probe prompt that asks for a
   shell command, a file edit and a web search produces no tool item other than primer's; then the same with a
   `code_mode_only` model, without and with a catalog file. This decides whether `code_mode_only` models are usable
   without a catalog file.
2. **The end-of-response signal.** `rawResponseItem/completed` carries each primer `function_call` before its
   `item/tool/call`, and `rawResponse/completed` follows the last one, for one call and for parallel calls.
3. **Tool-round continuation.** Injected `function_call` and `function_call_output` items followed by `turn/start`
   with `input: []` produce an answer that uses the output.
4. **Thread cleanup.** `turn/interrupt` then `thread/unsubscribe` with `thread_unload_delay_secs` 0 produces
   `thread/closed`, and the pending `item/tool/call` is cancelled.
5. **Usage shape** in `rawResponse/completed` (input, cached input, output and reasoning tokens) and in
   `thread/tokenUsage/updated`.
6. **Prompt caching across steps:** the second step of a session reads most of its input from cache.
7. **Timing:** the process cold start, a thread start, and the first token, recorded for the console's timeout
   defaults.
8. **Overrides that are inferred:** `features.code_mode.direct_only_tool_namespaces` as a `-c` key, the model
   provider's stream and request retry keys, the auto-compaction limit key (to stop Codex compacting inside a step),
   and an output-token cap if Codex has one.
9. **Errors:** the `codexErrorInfo` and message for a usage limit and for an oversized prompt, if they can be provoked
   without cost.
10. **Shared login:** two worker processes, and the operator's own interactive `codex`, sharing one home over a token
    refresh.
12. **The operator's home stays out:** with the default home, MCP servers from its `config.toml` are not started
    (`mcp_servers={}`), and its global instructions file and profiles do not reach the model.
11. **Model ids** returned by `model/list` on the operator's plan, and which of them are direct with the section 6.2
    overrides (from check 1).

## 12. Left to the builder

- Fixture contents and the recorder script, within section 10.
- The JSON-RPC client's internal structure, within the loop and routing rules of section 6.2.
- Exact log wording and metric names, following the `claude_code` adapter.
- Console layout of the Codex form within the existing agent-runtime register form.
