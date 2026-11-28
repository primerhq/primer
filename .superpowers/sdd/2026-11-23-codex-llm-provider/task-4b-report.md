# Task 4b report: classify the codex kind in the console (sweep regression)

**Status: done.** Commit `6cd43e421b25075124ee16ca2daa8e9d95e30c44`
(`fix(console): classify the codex kind under Agent runtimes`) on `feat/cx-t4b`
(worktree `../primer-cx-t4b`, base `9803133f5`, which
includes the T4 commits), 3 files changed, 45 insertions, 27 deletions, tree
clean, ruff clean, no em-dash introduced (diff-level grep: 0). Classification
slice only: no codex form fields, no status block, no import-list changes;
`register-llm.jsx` untouched (the claude_code-only form block at :292 left
alone as instructed).

## Files

| File | Change |
|---|---|
| `ui/components/providers/register-common.jsx` | `PC_CLASSES.llm.excludeKinds` -> `["claude_code", "codex"]`; `PC_CLASSES.agent_runtime.kinds` -> `["claude_code", "codex"]`; `codex: "Codex"` added after `claude_code` in both `PC_KIND_LABELS` maps (llm and agent_runtime); the two comments describing the claude_code classification (the agent_runtime class comment and the `PC_classOf` comment) now name codex too. Every classification special-case of claude_code in the file is covered; `register-llm.jsx` has no classification special-case to extend (its :292 block is the form block, out of scope). |
| `tests/ui/test_register_common.py` | 7 assertions changed + 1 new test (below) |
| `tests/ui/test_register_llm.py` | 2 assertions changed + 1 test renamed + 1 assertion extended + comment updates (below) |

## Every changed assertion and why

All changes are the new correct behaviour the task defines (codex is an
agent-runtime kind: named like claude_code, classified under Agent runtimes,
excluded from the LLM class); none weakens an assertion.

`tests/ui/test_register_common.py`:

1. `test_agent_runtime_class_shape` - `cls["kinds"] == ["claude_code"]` ->
   `["claude_code", "codex"]`. Reason: the agent runtimes class now owns both
   agent-runtime kinds (the backend's `AGENT_RUNTIME_TYPES = {claude_code,
   codex}`, T4).
2. `test_llm_class_excludes_agent_runtimes` -
   `PC_classByKey('llm').excludeKinds == ["claude_code"]` ->
   `["claude_code", "codex"]`. Reason: a codex row must not draw under the
   LLM class.
3. NEW `test_codex_row_draws_only_under_agent_runtimes` (the required test,
   next to the claude_code classification tests): `PC_classOf('llm_providers',
   {provider: 'codex'}) == "agent_runtime"`, `PC_classAccepts(agent_runtime,
   codex row) is True`, `PC_classAccepts(llm, codex row) is False`. Reason:
   pins that a codex row is drawn under Agent runtimes and not under LLM, the
   same classification claude_code rows get.
4. `test_kind_labels_rename_the_served_kinds` - the `Object.keys(PC_KIND_LABELS.llm)`
   list and the full map both gain `codex: "Codex"` right after claude_code.
   Reason: the console names the served codex kind, in label order.
5. `test_every_kind_the_server_serves_has_a_console_name` - the `named["llm"]`
   set gains `"codex"`. Reason: the server now serves `codex` from
   `GET /llm_providers/_types` (the T4 `_types` entry), so the console must
   carry its name. (One of the 4 sweep regressions.)
6. `test_served_kinds_drop_built_ins_and_follow_label_order` - the pinned
   `PC_servedKinds('llm', ...)` list gains `"codex"` right after
   `"claude_code"`. Reason: served kinds follow the label map's key order, and
   codex sits after claude_code in the map. (One of the 4 sweep regressions.)
7. `test_the_wizards_offer_their_class_kinds` - the pinned
   `PC_servedKinds('agent_runtime', ...)` list `["claude_code"]` ->
   `["claude_code", "codex"]`; the pinned `PC_KIND_LABELS.agent_runtime` map
   gains `codex: "Codex"`; one new explicit
   `assert "codex" not in PC_servedKinds('llm', ..., excludeKinds)` beside the
   existing claude_code one; docstring updated. Reason: the agent runtimes
   wizard offers exactly its own kinds; the LLM wizard excludes both.

`tests/ui/test_register_llm.py`:

8. `test_the_type_select_lists_the_llm_kinds_in_the_designs_order_and_never_claude_code`
   (one of the 4 sweep regressions) - the pinned full-list equality
   `options == ["openchat", ..., "openrouter"]` is kept UNCHANGED (it now
   passes because codex enters the class's `excludeKinds`, so the LLM select
   no longer offers it); EXTENDED with an explicit
   `assert "claude_code" not in options and "codex" not in options` so the
   "never offered" rule holds for codex by name, not only by the pinned list;
   docstring extended. Not weakened.
9. `test_the_kinds_a_caller_excludes_are_not_offered` (one of the 4 sweep
   regressions) - the pinned options list is UNCHANGED (passes for the same
   reason as 8); only the inline comment updated to name both exclusions.
10. `test_the_agent_runtime_wizard_offers_exactly_claude_code` renamed to
    `test_the_agent_runtime_wizard_offers_exactly_its_own_kinds` (the old name
    would now be false) - the pinned options list `["claude_code"]` ->
    `["claude_code", "codex"]`; the pinned labels `["Claude Code"]` ->
    `["Claude Code", "Codex"]`; the default `value == "claude_code"` assertion
    kept (claude_code is still the first option). Reason: the agent runtimes
    wizard now offers exactly its class's kinds.

## Commands and outputs

```
$ env -u ANTHROPIC_BASE_URL uv run pytest tests/ui/test_register_common.py tests/ui/test_register_llm.py -n0 -q -p no:cacheprovider
118 passed, 1 warning in 13.57s

$ env -u ANTHROPIC_BASE_URL uv run pytest tests/ui -n 2 --dist load -q -p no:cacheprovider
FAILED tests/ui/test_graph_builder_import_shapes.py::test_a_schema_nested_far_deeper_than_any_graph_has_its_own_message
1 failed, 7172 passed, 1 skipped, 3 warnings in 182.86s (0:03:02)

$ uv run ruff check tests/ui/test_register_common.py tests/ui/test_register_llm.py
Ruff: No issues found
```

The single broad-sweep failure is the known pre-existing worker SIGILL crash
(`worker 'gw0' crashed while running ...`), which fails on main too and is not
a regression; every other tests/ui test passes. No other sweeps, no e2e, no
subagents.

## Concerns

- In the agent runtimes wizard, a codex draft currently falls into the
  standard LLM form block: `register-llm.jsx:292` special-cases
  `s.kind === "claude_code"` for the form, and the brief says to leave that
  block alone - the codex form fields/status block land in the later console
  task.
- The backend codex `_types` entry is `discoverable: False` (T4 concern), so
  there is no model discovery for codex yet; the console classification is in
  place for when it lands.
- The broad tests/ui sweep took ~3 minutes and the known SIGILL crash kills
  its worker mid-file (pre-existing, main too).
