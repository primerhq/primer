# Codex LLM Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `codex` LLM provider that drives the Codex CLI installed and logged in on the worker host through
`codex app-server`, replacing the unregistered PR #1066 package.

**Architecture:** One long-lived `codex app-server` per worker process and provider. A fresh ephemeral thread per
model step: inject history as Responses items, run one turn, and end the step at the response's end. Primer's tools are
offered as one `primer` dynamic-tool namespace whose calls are never answered. A guard fails any step in which Codex
starts a tool of its own.

**Tech Stack:** Python 3.12 asyncio subprocess and JSON-RPC over stdio, pydantic v2, pytest, the no-build React JSX
console, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-16-codex-llm-provider-design.md` (Version 1, 2026-11-23)

**Where it runs:** Tasks 3 and 13 need a host with the Codex CLI (`codex --version` at or above `0.169.0`) and a
ChatGPT login (`codex login status` exits 0). The other tasks run anywhere, but Tasks 5 to 9 use Task 3's recordings
as fixtures, so build on the Codex host or copy the fixtures across.

## Global Constraints

- Minimum Codex version `0.169.0`; `CODEX_MAX_TESTED_VERSION` holds the highest version verified live (Task 3 sets it).
- App-server command: `<binary_path> app-server --listen stdio://` plus the spec section 6.2 `-c` overrides, verbatim:
  `features.shell_tool=false`, `web_search="disabled"`, `features.view_image=false`, `features.sleep_tool=false`,
  `features.image_generation=false`, `tools.experimental_request_user_input.enabled=false`, `features.apps=false`,
  `features.plugins=false`, `features.tool_suggest=false`, `features.multi_agent=false`, `features.code_mode=false`,
  `features.code_mode_only=false`, `features.code_mode.direct_only_tool_namespaces=["primer"]`, `mcp_servers={}`,
  `project_doc_max_bytes=0`, `history.persistence="none"`, `include_permissions_instructions=false`,
  `include_environment_context=false`, `include_apps_instructions=false`, the stream and request retry counts at `0`
  (key names from Task 3), and `model_catalog_json=<model_catalog_path>` when set.
- Environment: `HOME`, `PATH`, `USER`, `LOGNAME`, `LANG`, `TERM`, `SHELL`, `TMPDIR`, plus `pass_env`, plus
  `CODEX_HOME` only when `codex_home` is set.
- `initialize` sends `clientInfo {name: "primer", title: "primer", version: <primer version>}` and
  `capabilities {experimentalApi: true}`, then the `initialized` notification.
- `thread/start` sends `ephemeral: true`, `experimentalRawEvents: true`, `sandbox: "read-only"`,
  `approvalPolicy: "untrusted"`, `baseInstructions`, `config: {thread_unload_delay_secs: 0}`, and `dynamicTools` only
  when the step has tools and `tool_choice` is not `none`.
- Tool namespace `primer`. An alias is the scoped id when it is at most 64 characters of `[A-Za-z0-9_-]`; otherwise
  the first 55 characters (others replaced by `_`), then `_`, then 8 hex characters of SHA-256.
- Direct models: `CODEX_DIRECT_MODELS = frozenset({"gpt-5.5"})`, or a `model_catalog_path` entry with
  `"tool_mode": "direct"`.
- Error codes: `codex_tool_violation`, `codex_stream_retried`, `codex_app_server_failed`. `held_back` is not used here.
- Temp directory prefix `primer-codex-<worker pid>-`, mode 0700, with `child.json` and an empty `cwd/`.
- A thread still loaded 30 s after its step logs a WARNING; recycle the process past 100 loaded threads.
- Primer never opens `auth.json`.
- Conventional commits, no `Co-Authored-By`, stage only touched files, no U+2014; implementers run focused tests with
  `-n0`, never whole-area sweeps or e2e (the coordinator runs those).

## Review Focus

- The app-server exits or closes stdout mid-step: every in-flight step on that process fails once with
  `TransientError(code="codex_app_server_failed")`, and the next step starts a new process. Pinned in Task 6
  (`test_process_death_fails_inflight_steps_and_restarts`).
- Two steps run at once on one worker: notifications and server requests reach only the step that owns their
  `threadId`. Pinned in Task 5 (`test_two_threads_never_cross`).
- A JSONL line over 64 KiB (a long answer or large tool arguments) breaks nothing: pinned in Task 5
  (`test_300kb_line_is_read_whole`, a 300 KB line that straddles read events).
- Primer Stop or cancellation while a turn runs: `turn/interrupt` then `thread/unsubscribe` are sent, exactly one
  terminal event is yielded, and cancellation still propagates. Pinned in Task 8 (`test_cancel_interrupts_and_unsubscribes`).
- The operator's own `~/.codex` holds MCP servers and instructions: none reach a step. Pinned in Task 6
  (`test_argv_carries_every_override`) and verified live in Task 3 (check 12).

## Task order

| Wave | Tasks | Where |
|---|---|---|
| 1 | Task 1, Task 2 | anywhere |
| 1 | Task 3 | Codex host |
| 2 | Task 4 | anywhere |
| 3 | Task 5 | anywhere, with Task 3 fixtures |
| 4 | Task 6, Task 7 | anywhere |
| 5 | Task 8 | anywhere |
| 6 | Task 9, Task 10, Task 11 | anywhere |
| 7 | Task 12 | anywhere |
| 8 | Task 13 | Codex host |

---

### Task 1: Remove the PR #1066 package

**Files:**
- Delete: `primer/llm/codex/` (all of it: `__init__.py`, `adapter.py`, `boundary.py`, `bridge.py`, `harness.py`,
  `lifecycle.py`, `request.py`, `README.md`, `broker/`) and `tests/llm/codex/`
- Modify: `docs/dev/subsystems/model-providers.md` (the PR #1066 paragraph, lines 772-782 at `e51e36664`),
  `docs/wiki/providers/agent-runtimes.md` (any #1066 wording), and any other file `grep -rn "llm.codex\|llm/codex\|codex/broker\|CodexFoundationLLM\|ModelOnlyBridge" --include='*.py' --include='*.md' .` finds outside `docs/superpowers/`

- [ ] **Step 1:** `git rm -r primer/llm/codex tests/llm/codex`; remove the doc paragraphs; re-run the grep and expect
  no hit outside `docs/superpowers/`.
- [ ] **Step 2: Verify:** `uv run python -c "import primer.api.app"` succeeds;
  `uv run pytest tests/docs -q -n 2 -p no:cacheprovider` passes; `uv run pytest tests/llm -q -n 2 -p no:cacheprovider`
  shows no import errors (the package had no importers).
- [ ] **Step 3: Commit** `refactor(llm): remove the unregistered Codex broker package`.

### Task 2: Shared agent-runtime helpers

**Files:**
- Create: `primer/llm/agent_runtime/__init__.py`, `primer/llm/agent_runtime/process.py`,
  `primer/llm/agent_runtime/history.py`, `primer/llm/agent_runtime/alias.py`
- Modify: `primer/llm/claude_code/process.py`, `history.py`, `bridge.py` (import from `agent_runtime`, keep their
  public names as re-exports), `primer/api/registries/provider_registry.py` (`_sweep_agent_runtime_orphans_once`, :49)
- Test: `tests/llm/agent_runtime/test_process.py`, `tests/llm/agent_runtime/test_alias.py`; `tests/llm/claude_code/`
  unchanged

**Interfaces:**
- Produces:
  - `child_env(pass_env: Sequence[str], *, extra: Mapping[str, str] | None = None) -> dict[str, str]`;
  - `child_process(argv_without_tmp, *, env, prefix: str, files: Mapping[str, str] | None = None)`, the
    `claude_code` async context manager with the directory prefix as a parameter (`"primer-cc"` for `claude_code`,
    `"primer-codex"` for Codex) and its `system.md` turned into a `files` mapping;
  - `sweep_orphans(tmp_root: Path | None = None, *, prefixes: Sequence[str] = ("primer-cc", "primer-codex")) -> int`;
  - `split_tail(messages: Sequence[Message]) -> tuple[list[Message], list[Message]]` (older history, tail; raises
    `BadRequestError("history must end with a user or tool message")`);
  - `replay_tool_id(original: str) -> str` (moved as is);
  - `tool_alias(scoped_id: str, *, max_len: int, keep_len: int) -> str` (the bridge's rule with the limits as
    parameters; `claude_code` keeps 56 and 47).

- [ ] **Step 1: Write the failing tests** `test_child_process_uses_the_given_prefix`,
  `test_sweep_handles_both_prefixes`, `test_tool_alias_limits` (64/55 for Codex, 56/47 for `claude_code`, both
  sha256[:8]), `test_split_tail_user_and_tool_round`, `test_split_tail_refuses_assistant_tail`.
- [ ] **Step 2: Run them to see them fail:** `uv run pytest tests/llm/agent_runtime -n0 -q`.
- [ ] **Step 3: Move the code**, then switch `claude_code` to the shared helpers without changing its behaviour.
- [ ] **Step 4: Run them to see them pass,** plus `uv run pytest tests/llm/claude_code tests/api/test_claude_code_status_route.py -n0 -q` unchanged and green.
- [ ] **Step 5: Commit** `refactor(llm): share agent-runtime process, history and alias helpers`.

### Task 3: Live checks and recordings (Codex host)

**Files:**
- Create: `scripts/codex/record_app_server.py` (a stdio proxy between a client and `codex app-server` that appends
  every line in both directions to a JSONL file, tagged `"dir": "c2s" | "s2c"`), `scripts/codex/live_checks.py` (drives
  checks 1 to 12 of spec section 11 through the proxy, using the real CLI and login),
  `tests/llm/fixtures/codex/*.jsonl`, `docs/superpowers/notes/2026-11-23-codex-live-checks.md` (local, git-ignored)

- [ ] **Step 1:** Run `codex --version` and `codex login status`; record both in the notes.
- [ ] **Step 2:** Run every check in spec section 11 with the section 6.2 overrides, `gpt-5.5` and the cheapest
  effort. Record each one as PASS or FAIL in the notes, with the evidence: a line excerpt or a measured number. For
  check 8, find the stream and request retry keys and the auto-compaction key, and quote the source or the help text.
  Set `CODEX_MAX_TESTED_VERSION` (Task 4 creates the constant) to the version checked.
- [ ] **Step 3:** Record these fixtures through the proxy: `plain_answer`, `one_tool_call`, `parallel_tool_calls`,
  `tool_round_continue` (inject plus `input: []`), `structured_output`, `interrupt`, `context_overflow` and
  `usage_limit` (only if they can be provoked without cost; skip with a note otherwise), `account_read`, and
  `model_list`. Replace ids that look like live process or socket paths, and remove emails and account ids.
- [ ] **Step 4: Gate.** If any check fails, stop and report it to the coordinator with the evidence. The spec is
  amended before Tasks 5 to 9 rely on that behaviour.
- [ ] **Step 5: Commit** the scripts and fixtures as `test(llm): record Codex app-server fixtures and live checks`.
  The notes stay local.

### Task 4: Provider type, config and the direct-model rule

**Files:**
- Modify: `primer/model/providers/llm.py` (`LLMProviderType`, `AGENT_RUNTIME_TYPES` and its stale comment about seed
  refusal, plus a new `CodexConfig` next to `ClaudeCodeConfig` at :260), `primer/model/provider.py` (re-export of
  `AGENT_RUNTIME_TYPES`, :69), `primer/api/routers/providers.py` (the `_types` entry next to :490),
  `primer/api/routers/model_profiles.py` (a hook next to `_check_provider_exists`, :184)
- Create: `primer/llm/codex/__init__.py`, `primer/llm/codex/models.py`
- Test: `tests/model/test_codex_config.py`, `tests/llm/codex/test_models.py`, `tests/api/test_codex_profile_rule.py`

**Interfaces:**
- Produces:
  - `LLMProviderType.CODEX = "codex"`, in `AGENT_RUNTIME_TYPES`;
  - `CodexConfig(binary_path: str = "codex", codex_home: str | None = None, pass_env: list[str],
    model_catalog_path: str | None = None)`, with the `pass_env` screen copied from `ClaudeCodeConfig`;
  - `CODEX_DIRECT_MODELS: frozenset[str] = frozenset({"gpt-5.5"})`, `MINIMUM_VERSION = "0.169.0"`,
    `CODEX_MAX_TESTED_VERSION: str`;
  - `is_direct_model(model: str, *, catalog_path: str | None) -> bool`: reads only the operator's catalog file, and
    returns `False` when the file cannot be read;
  - the profile hook refuses a `codex` profile whose model is not direct with a 422 problem naming the rule.

- [ ] **Step 1: Write the failing tests** `test_codex_config_defaults` (`codex_home is None`),
  `test_pass_env_screens_secrets`, `test_gpt_5_5_is_direct`, `test_catalog_direct_entry_is_direct`,
  `test_catalog_non_direct_entry_is_not`, `test_unreadable_catalog_is_not_direct`,
  `test_profile_with_non_direct_model_is_422`, `test_profile_with_direct_model_is_created`.
- [ ] **Step 2: Run them to see them fail:** `uv run pytest tests/model/test_codex_config.py tests/llm/codex/test_models.py tests/api/test_codex_profile_rule.py -n0 -q`.
- [ ] **Step 3: Implement**, and fix the `AGENT_RUNTIME_TYPES` comment so it no longer claims a seed refusal.
- [ ] **Step 4: Run them to see them pass,** plus `uv run pytest tests/api/test_providers*.py tests/model -n 2 -q -p no:cacheprovider`.
- [ ] **Step 5: Commit** `feat(llm): add the codex provider type, config and direct-model rule`.

### Task 5: JSON-RPC client and the fake app-server

**Files:**
- Create: `primer/llm/codex/client.py`, `tests/llm/codex/fake_codex.py` (an executable script selected through
  `binary_path` that replays a Task 3 fixture's `s2c` lines in response to the matching `c2s` method, and records what
  it received to `FAKE_CODEX_RECORD`; fixtures chosen with `FAKE_CODEX_FIXTURE` / `FAKE_CODEX_FIXTURE_SEQ`, like
  `tests/llm/claude_code/fake_claude.py`)
- Test: `tests/llm/codex/test_client.py`, `tests/llm/codex/test_fake_codex.py`

**Interfaces:**
- Produces:
  - `class AppServerClient` over a process's stdin and stdout, bound to the running event loop:
    - `async request(method: str, params: dict) -> dict`, which raises `AppServerError(code, message)` on a JSON-RPC
      error;
    - `async notify(method: str, params: dict) -> None`;
    - `subscribe(thread_id: str) -> ThreadInbox`, an async iterator of `Notification(method, params)` and
      `ServerRequest(id, method, params)` items for that thread;
    - `async respond(request_id, result: dict) -> None` and `async respond_error(request_id, code: int, message: str)`;
    - `closed: asyncio.Event`, set when stdout ends.
  - A server request whose `threadId` has no inbox gets `respond_error(-32601, "no step owns this thread")`.
  - The stdout reader uses a limit of `2**26`.

- [ ] **Step 1: Write the failing tests** `test_request_response_matching`, `test_notifications_route_by_thread`,
  `test_two_threads_never_cross`, `test_unknown_thread_server_request_gets_an_error`, `test_300kb_line_is_read_whole`,
  `test_stdout_end_sets_closed_and_fails_pending_requests`, `test_client_is_bound_to_its_loop`.
- [ ] **Step 2: Run them to see them fail:** `uv run pytest tests/llm/codex/test_client.py -n0 -q`.
- [ ] **Step 3: Implement**, and the fake.
- [ ] **Step 4: Run them to see them pass,** plus `tests/llm/codex/test_fake_codex.py`.
- [ ] **Step 5: Commit** `feat(llm): add the Codex app-server JSON-RPC client and fake`.

### Task 6: The per-worker app-server

**Files:**
- Create: `primer/llm/codex/server.py`
- Test: `tests/llm/codex/test_server.py`

**Interfaces:**
- Consumes: Task 2 `child_env`, `child_process` (prefix `"primer-codex"`) and `sweep_orphans`; Task 5
  `AppServerClient`.
- Produces:
  - `build_argv(config: CodexConfig, *, overrides: Sequence[str]) -> list[str]` and
    `APP_SERVER_OVERRIDES: tuple[str, ...]`, the Global Constraints list;
  - `async get_app_server(provider_id: str, config: CodexConfig) -> AppServer`, one per (loop, provider id, config
    hash). It is started lazily with the `initialize` handshake. A config change retires the old server once its last
    step ends;
  - `class AppServer` with `client: AppServerClient`, `async start_thread(params: dict) -> str`,
    `async end_thread(thread_id: str, *, turn_id: str | None) -> None` (`turn/interrupt` when a turn id is given,
    then `thread/unsubscribe`), `loaded_threads: int` and `async aclose()`;
  - `async aclose_app_servers() -> None`, for the registry.
  - When the process exits or `closed` is set with steps in flight, each step fails with
    `TransientError(code="codex_app_server_failed")` and the last stderr lines; the next `get_app_server` starts a new
    one. Past 100 loaded threads the server is retired.

- [ ] **Step 1: Write the failing tests** `test_argv_carries_every_override`, `test_codex_home_sets_env_only_when_set`,
  `test_initialize_handshake_opts_into_experimental_api`, `test_one_server_per_provider_and_loop`,
  `test_config_change_retires_after_last_step`, `test_process_death_fails_inflight_steps_and_restarts`,
  `test_aclose_kills_group_before_removing_dir`, `test_thread_count_recycles_past_100`,
  `test_unloaded_thread_warning_after_30s` (with a patched clock).
- [ ] **Step 2: Run them to see them fail:** `uv run pytest tests/llm/codex/test_server.py -n0 -q`.
- [ ] **Step 3: Implement**, using the Task 3 retry-count keys in `APP_SERVER_OVERRIDES`.
- [ ] **Step 4: Run them to see them pass.**
- [ ] **Step 5: Commit** `feat(llm): run one Codex app-server per worker`.

### Task 7: History items, event translation and the guard

**Files:**
- Create: `primer/llm/codex/history.py`, `primer/llm/codex/translate.py`
- Test: `tests/llm/codex/test_history.py`, `tests/llm/codex/test_translate.py`

**Interfaces:**
- Consumes: Task 2 `split_tail`, `replay_tool_id` and `tool_alias(..., max_len=64, keep_len=55)`.
- Produces:
  - `plan_items(messages, *, alias_of) -> ItemPlan(base_instructions: str, inject: list[dict], turn_input: list[dict])`,
    built by spec section 6.5. `turn_input` is `[]` for a tool-round tail. Failed results start with `[error] `.
    Arguments are JSON with sorted keys. Images are data URLs, and other media become a placeholder.
  - `dynamic_tools(tools, *, alias_of) -> list[dict]`, the single `primer` namespace spec.
  - `class StepTranslator(aliases: Mapping[str, str])` with `feed(item) -> list[StreamEvent]` and
    `outcome -> StepOutcome | None`:
    - spec section 6.7: `ReasoningDelta`, `ToolCallStart`, `ToolCallDelta`, `ToolCallEnd`, `Usage`;
    - spec section 6.6, the guard: a violation sets `outcome` to `Violation(item_type)`;
    - end conditions: `ToolUseEnd` after `rawResponse/completed` with primer calls, `PlainEnd` on `turn/completed`
      completed, `Failed(codexErrorInfo, message)`, and `Retried` for `willRetry: true` after an event was yielded.

- [ ] **Step 1: Write the failing tests**
  - history: `test_user_tail_becomes_turn_input`, `test_tool_round_tail_is_injected_with_empty_input`,
    `test_reused_call_ids_are_renamed`, `test_failed_result_is_prefixed`, `test_args_are_sorted_json`,
    `test_image_becomes_data_url`, `test_other_media_is_a_placeholder`, `test_long_tool_id_alias`;
  - translate, each fed from a Task 3 fixture: `test_plain_answer_streams_text_then_plain_end`,
    `test_one_tool_call_from_raw_item`, `test_parallel_tool_calls`, `test_reasoning_summary_is_reasoning_delta`,
    `test_raw_reasoning_dropped`, `test_usage_from_raw_response_completed`, `test_structured_output_text`,
    `test_will_retry_before_any_event_is_ignored`, `test_will_retry_after_text_is_retried`;
  - guard: `test_guard_function_call_outside_namespace`, `test_guard_custom_tool_call`,
    `test_guard_command_execution_item`, `test_guard_file_change_item`, `test_guard_mcp_tool_call_item`,
    `test_guard_web_search_item`, `test_guard_approval_request`;
  - logging: `test_context_compaction_logged_warning`, `test_rate_limit_update_logged_debug`.
- [ ] **Step 2: Run them to see them fail:** `uv run pytest tests/llm/codex/test_history.py tests/llm/codex/test_translate.py -n0 -q`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them to see them pass.**
- [ ] **Step 5: Commit** `feat(llm): replay history as Responses items and translate Codex events`.

### Task 8: The adapter, reasoning mapping and registry wiring

**Files:**
- Create: `primer/llm/codex/adapter.py`
- Modify: `primer/llm/_reasoning.py` (a `codex_extended` next to `claude_code_extended` at :324, and its export),
  `primer/api/registries/provider_registry.py` (a `CODEX` case next to the `CLAUDE_CODE` case at :114, plus the sweep
  and `aclose_app_servers` in `aclose`)
- Test: `tests/llm/codex/test_adapter.py`, `tests/llm/test_reasoning_codex.py`

**Interfaces:**
- Consumes: Tasks 4 to 7.
- Produces:
  - `class CodexLLM(LLM)`:
    - `stream()` follows spec section 6.3 steps 1 to 8, including the step-start check with `is_direct_model`
      (`ConfigError`) and the error table of section 6.8;
    - `count_tokens` raises `TokenCounterUnavailable`;
  - `codex_extended(config: ModelProfileConfig) -> dict[str, Any]`, the effort and summary mapping of spec section
    6.9, with `extended` `reasoning_effort` and `reasoning_summary` overrides, clamped against the model's
    `supportedReasoningEfforts` (from `model/list`, cached per server).

- [ ] **Step 1: Write the failing tests** (all on the fake with Task 3 fixtures)
  - `test_plain_answer_one_done`, `test_tool_call_ends_step_with_tool_use_then_interrupts_and_unsubscribes`,
    `test_tool_call_request_never_answered`, `test_tool_round_continuation_sends_empty_input`;
  - `test_structured_output_streams_json`, `test_violation_is_fatal_codex_tool_violation_and_not_retried`,
    `test_retried_after_text_is_codex_stream_retried`;
  - `test_context_overflow_before_event_is_bad_request`, `test_context_overflow_after_event_is_bad_request_error_event`,
    `test_usage_limit_is_rate_limit_error`, `test_unauthorized_is_authentication_error`;
  - `test_stall_timeout`, `test_cancel_interrupts_and_unsubscribes`, `test_stop_mid_stream_one_terminal_event`;
  - `test_model_not_found_is_bad_request`, `test_server_overloaded_is_server_error`,
    `test_missing_binary_is_config_error`, `test_no_chatgpt_account_is_authentication_error`;
  - `test_tool_choice_none_omits_dynamic_tools`, `test_ignored_params_logged_once`;
  - `test_non_direct_model_raises_config_error_before_any_request`, `test_slot_is_released_on_every_path`;
  - `test_registry_builds_codex_and_sweeps_once`, `test_registry_aclose_closes_app_servers`;
  - reasoning: `test_effort_mapping`, `test_effort_clamps_to_supported`, `test_off_gives_lowest_supported`,
    `test_extended_overrides`, `test_summary_auto_above_off`.
- [ ] **Step 2: Run them to see them fail:** `uv run pytest tests/llm/codex/test_adapter.py tests/llm/test_reasoning_codex.py -n0 -q`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them to see them pass,** plus `uv run pytest tests/llm/codex tests/llm/claude_code tests/api/test_provider_registry*.py -n 2 -q -p no:cacheprovider`.
- [ ] **Step 5: Commit** `feat(llm): add the codex agent runtime adapter`.

### Task 9: Host status and model discovery

**Files:**
- Create: `primer/llm/codex/status.py`
- Modify: `primer/api/routers/providers.py` (the runtime status route at :830-846 accepts `codex` too; the
  `_dispatch_llm_probe` `elif` chain next to the `claude_code` branch at :1065)
- Test: `tests/llm/codex/test_status.py`, `tests/api/test_codex_status_route.py`

**Interfaces:**
- Produces:
  - `async runtime_status(config: CodexConfig) -> CodexRuntimeStatus(host, installed, version, meets_minimum,
    above_max_tested, logged_in, plan_type)`: `codex --version`, then a short-lived app-server for `initialize` and
    `account/read`. It kills the process group on any outcome, cancellation included;
  - `async discover_models(config: CodexConfig) -> list[DiscoveredModel(id, display_name, efforts, input_modalities,
    direct: bool)]` from `model/list`, non-hidden only, `direct` by `is_direct_model`;
  - the route answers `codex` rows like `claude_code` rows, and discovery returns `{"models": [...], "runtime": ...}`.

- [ ] **Step 1: Write the failing tests** `test_status_logged_in`, `test_status_logged_out`, `test_status_missing_binary`,
  `test_status_below_minimum`, `test_status_above_max_tested_warns`, `test_status_cancel_kills_child`,
  `test_discovery_marks_direct`, `test_route_answers_codex_rows`, `test_route_404_for_other_kinds`.
- [ ] **Step 2: Run them to see them fail;** **Step 3: implement;** **Step 4: run them to see them pass**
  (`uv run pytest tests/llm/codex/test_status.py tests/api/test_codex_status_route.py -n0 -q`).
- [ ] **Step 5: Commit** `feat(llm): codex host status and model discovery`.

### Task 10: Console

**Files:**
- Modify: `ui/components/providers/register-common.jsx` (`PC_CLASSES.agent_runtime.kinds` :62-63, `PC_KIND_LABELS`
  :131-134, the `llm` class `excludeKinds`), `ui/components/providers/register-llm.jsx` (the `codex` form fields and
  status block), the import-models list, and the pinned UI tests the run names
- Test: `tests/ui/test_register_codex.py`

**Interfaces:**
- Produces:
  - the `codex` kind in the Agent runtimes class, labelled "Codex";
  - form fields Binary path (`pc-codex-binary`), Codex home (`pc-codex-home`, help "Leave empty to use this machine's
    Codex login"), Pass-through environment (`pc-codex-pass-env`) and Model catalog file (`pc-codex-catalog`);
  - a status block showing version, minimum met, logged in and plan type;
  - an import list that offers direct models and lists the others as unavailable with the reason;
  - no terms banner and no extra gate (spec section 6.12).

- [ ] **Step 1: Write the failing tests** `test_codex_listed_under_agent_runtimes_not_llm`,
  `test_codex_form_fields_and_help`, `test_codex_status_block`, `test_import_marks_non_direct_unavailable`.
- [ ] **Step 2: Run them to see them fail;** **Step 3: implement;** **Step 4: run them to see them pass**
  (`uv run pytest tests/ui/test_register_codex.py -n0 -q`, then `uv run pytest tests/ui -n 2 -q -p no:cacheprovider`;
  the known `test_graph_builder_import_shapes` SIGILL crash on this Mac is not a regression).
- [ ] **Step 5: Commit** `feat(console): codex in the Agent runtimes class`.

### Task 11: Documentation

**Files:**
- Modify: `docs/wiki/providers/agent-runtimes.md` (a Codex section: install and `codex login` on the worker host, or a
  separate home; what Codex replaces and disables; the direct-model rule and the catalog file; that every request runs
  on the plan of that login), `docs/dev/subsystems/model-providers.md` (the Codex adapter: app-server, step, guard,
  errors, live-check results from Task 3), and its PageSpec if the page cap needs it

- [ ] **Step 1:** Write both sections, naming only real routes and tools.
- [ ] **Step 2: Run** `uv run pytest tests/docs -n 2 -q -p no:cacheprovider` until it passes (no U+2014, PageSpec caps).
- [ ] **Step 3: Commit** `docs: codex agent runtime provider`.

### Task 12: Journeys and CI

**Files:**
- Create: `tests/e2e/test_codex_provider_journey.py`, `tests/ui_e2e/test_codex_register_journey.py`
- Modify: `.github/workflows/e2e.yml` (job-level `FAKE_CODEX_FIXTURE`, `FAKE_CODEX_FIXTURE_SEQ` and
  `FAKE_CODEX_RECORD` next to the `FAKE_CLAUDE_*` values), `tests/ui_e2e/_a11y_surfaces.py` (the Codex form floor,
  measured)

- [ ] **Step 1: Write the journeys** in the shape of `tests/e2e/test_claude_code_provider_journey.py`:
  - backend: register a `codex` provider on the fake, read its status, run a session step that ends with a held tool
    call that primer runs, then the next step's answer, and assert no leftover `primer-codex-*` directory after
    teardown;
  - console: the register form, the status block and the import list, navigated through the shell facade
    (`tests/ui_e2e/_shell_helpers.py`).
- [ ] **Step 2: Collect only:** `uv run pytest tests/e2e/test_codex_provider_journey.py tests/ui_e2e/test_codex_register_journey.py --collect-only -q`.
  The coordinator runs them exclusively.
- [ ] **Step 3: Commit** `test(e2e): codex provider journeys`.

### Task 13: Live smoke and coordinator verification (Codex host)

- [ ] Create `tests/integration/test_codex_smoke.py`, gated by `PRIMER_TEST_CODEX=1` and never run in CI. It runs one
  tool round against the real login with a direct model, then the leftover check: only the worker's own app-server
  is running, no thread is loaded, and no `primer-codex-*` directory remains after shutdown.
- [ ] The coordinator runs:
  - the narrowed unit sweep and the docs suite;
  - the e2e and UI journeys, exclusively;
  - a scratch-server session on the real CLI: a two-step session, checking that the second step reads most of its
    input from cache, plus a guard probe;
  - then the final whole-branch review, the PR, green CI and the merge.
