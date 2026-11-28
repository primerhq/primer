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

