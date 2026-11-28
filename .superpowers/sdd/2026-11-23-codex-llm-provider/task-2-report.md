# Task 2 report: shared agent-runtime helpers

**Commit:** `58b0a0a43` `refactor(llm): share agent-runtime process, history and alias helpers`
on `feat/cx-t2` (off `e51e36664`), worktree `../primer-cx-t2`.
13 files: 4 new modules + 3 new test files + 1 test `__init__`, 5 modifications.

## Files moved / created

### `primer/llm/agent_runtime/` (new package)

- **`__init__.py`** - package docstring only (no imports, no import-cycle surface).
- **`process.py`** - moved from `primer/llm/claude_code/process.py`, parameterized:
  - `child_env(pass_env: Sequence[str], *, extra: Mapping[str, str] | None = None) -> dict[str, str]` -
    ALLOWED names + `pass_env` names + the provider's `extra` entries. The Claude-specific
    `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0` constant is gone from the shared module; it comes in
    via `extra`.
  - `child_process(argv_without_tmp, *, env: dict[str, str], prefix: str, files: Mapping[str, str] | None = None, line_limit: int = _STREAM_LINE_LIMIT)` -
    async context manager; temp dir named `f"{prefix}-{os.getpid()}-"` (was hard-coded
    `primer-cc-`); writes the `files` mapping (was the hard-coded `system.md`); everything else
    (0700 chmod, `child.json`, `NEW_SESSION`, Linux `prctl` death signal, shielded group-kill
    cleanup, `stderr.log`, spawn-failure rmtree) unchanged. `line_limit` is a new keyword with
    the old constant as default - see "old import paths" for why it exists.
  - `sweep_orphans(tmp_root: Path | None = None, *, prefixes: Sequence[str] = DEFAULT_SWEEP_PREFIXES) -> int` -
    `DEFAULT_SWEEP_PREFIXES = ("primer-cc", "primer-codex")`. `_worker_pid_from_dir` now takes
    the prefixes and matches `<prefix>-<pid>-...` for each one; the pid parse is
    shape-equivalent to the old one (prefix required, numeric pid segment, optional suffix).
    Every trust check kept verbatim for every prefix: directory must be a real (lstat) 0700
    directory of the sweep's uid; `child.json` opened with `O_NOFOLLOW` + `fstat` (symlink /
    non-regular / foreign uid = tampered, left alone); a record pointing at the sweep's own
    process group = tampered, left alone; `pgid <= 1` never signalled; missing/unparseable
    record = delete without kill.
  - `read_stderr_tail`, `ALLOWED`, `CLEANUP_WAIT_S`, `_ChildRecord`, `_child_pgid`,
    `_worker_is_alive`, `_resolve_pdeathsig`, `_cleanup`, `_STREAM_LINE_LIMIT` - moved as is.
  - Module docstring generalized (`primer-cc-<pid>-*` to `<prefix>-<pid>-*`, idle-timeout line
    rephrased to "provider-named extra").
- **`history.py`** - moved from `primer/llm/claude_code/history.py`:
  - `replay_tool_id(original: str) -> str` - moved as is.
  - `split_tail(messages: Sequence[Message]) -> tuple[list[Message], list[Message]]` - new;
    returns (older history, tail) with the exact split `plan_replay` always did (user-last ->
    last message is the tail; tool-last -> the making assistant message plus the tool result
    messages are the tail; tool results with no making assistant -> the results are the tail);
    raises `BadRequestError("history must end with a user or tool message")` (imported from
    `primer.model.except_`, same source the adapter used) when the history is empty of
    non-system messages or ends in an assistant message.
  - `plan_replay` now calls `split_tail` internally, so it raises that same `BadRequestError`
    at the same point (it is a pure computation with no side effects before it). All rendering
    (`ReplayPlan`, `_escape_history_tag`, `_content`, `_media_line`, `_part_lines`, the
    incremental id-to-name map, append-only prefix behaviour) is byte-for-byte the old code.
- **`alias.py`** - new:
  - `tool_alias(scoped_id: str, *, max_len: int, keep_len: int, sanitize: bool = False) -> str`
    - `sanitize=False`: exactly the old `alias_for` rule - `len <= max_len` passes through;
      longer becomes `scoped_id[:keep_len] + "_" + sha256(scoped_id).hexdigest()[:8]`.
    - `sanitize=True`: pass-through only when `len <= max_len` AND
      `re.fullmatch(r"[A-Za-z0-9_-]+")`; otherwise the FIRST `keep_len` characters with every
      character outside `[A-Za-z0-9_-]` replaced by `_`, then `_` + the 8 hex of the SHA-256
      of the ORIGINAL id.

### Modifications

- **`primer/llm/claude_code/process.py`** - now a thin re-export layer (384 -> ~70 lines).
  Keeps the public names: `ALLOWED`, `CLEANUP_WAIT_S`, `_ChildRecord`, `_child_pgid`,
  `read_stderr_tail`, `sweep_orphans` (re-exported; its default prefixes include
  `primer-cc`) and two wrappers that carry claude_code's parameters:
  - `child_env(pass_env)` -> `_shared_child_env(pass_env, extra={"CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT": "0"})`
    - the constant stays here as `CHILD_IDLE_TIMEOUT_ENV`; the produced child environment is
      byte-identical to before (same ALLOWED/pass_env base, same last-added override key/value).
  - `child_process(argv_without_tmp, *, env, system_text)` ->
    `_shared_child_process(..., prefix="primer-cc", files={"system.md": system_text}, line_limit=_STREAM_LINE_LIMIT)` -
    same temp-dir name pattern (`primer-cc-<pid>-`), same `system.md` content, same
    readline limit.
- **`primer/llm/claude_code/history.py`** - thin re-export: `ReplayPlan`, `plan_replay`,
  `replay_tool_id`, `split_tail` from `agent_runtime.history`.
- **`primer/llm/claude_code/bridge.py`** - `alias_for(scoped_id)` is now
  `tool_alias(scoped_id, max_len=_ALIAS_MAX, keep_len=_ALIAS_HEAD)` (56/47, unsanitized =
  the exact old rule); the `hashlib` import is gone. `_ALIAS_MAX`/`_ALIAS_HEAD` stay here.
- **`primer/llm/claude_code/adapter.py`** - removed the two-line
  `if not plan.tail or plan.tail[-1].role not in ("user", "tool"): raise BadRequestError(...)`
  after the `plan_replay` call: `plan_replay` now raises the identical `BadRequestError`
  (same message, same point, before any side effect) via `split_tail`. `BadRequestError`
  import kept (still used later in the adapter).
- **`primer/api/registries/provider_registry.py`** - `_sweep_agent_runtime_orphans_once`
  now imports `sweep_orphans` from `primer.llm.agent_runtime.process` (was
  `primer.llm.claude_code.process`) and its docstring names both prefixes.
- **`primer/api/routers/providers.py`** - UNTOUCHED: its
  `from primer.llm.claude_code.process import child_env` keeps working via the shim.

### Tests (written first, RED confirmed)

New `tests/llm/agent_runtime/` package:

- `test_process.py`: `test_child_process_uses_the_given_prefix` (a `primer-codex-<pid>-*`
  dir holding exactly the given files, 0700, correct `child.json`, removed on exit),
  `test_sweep_handles_both_prefixes` (dead-worker dirs of BOTH prefixes swept, a
  `primer-<pid>-` name matching no prefix left alone).
- `test_alias.py`: `test_tool_alias_limits` (56/47 and 64/55, pass-through bounds,
  `sha256[:8]` tails, unsanitized character-irrelevant, sanitized pass-through rule,
  sanitize-the-first-keep_len-then-hash-of-the-full-id for long ids).
- `test_history.py`: `test_split_tail_user_and_tool_round`, `test_split_tail_refuses_assistant_tail`
  (assistant-last and system-only both raise the exact `BadRequestError` message).

`tests/llm/claude_code/` is UNCHANGED (0 files touched there).

## RED evidence

Step 2 (tests first, code not yet moved), `uv run pytest tests/llm/agent_runtime -n0 -q`:

```
ImportError while importing test module '.../tests/llm/agent_runtime/test_process.py'
    tests/llm/agent_runtime/test_process.py:12: in <module>
E   ModuleNotFoundError: No module named 'primer.llm.agent_runtime'
```

(same for `test_alias.py` and `test_history.py`). One test-side correction before GREEN:
the long-id expectation in `test_tool_alias_limits` initially miscounted which character
falls inside the first-55 window (the code was right, the expectation off by one position);
fixed the test, kept the code.

## Test commands and outputs (post-commit, on 58b0a0a43)

| Command | Result |
|---|---|
| `uv run pytest tests/llm/agent_runtime -n0 -q` | `5 passed in 1.59s` |
| `uv run pytest tests/llm/claude_code tests/api/test_claude_code_status_route.py -n0 -q` | `120 passed, 1 warning in 25.42s` |
| `uv run ruff check` (all 9 touched source files + new test dir) | `No issues found` |
| em-dash (U+2014) scan of all touched files | 0 in every file I created or edited (2 pre-existing ones in `provider_registry.py` lines 342/864, outside this diff and outside the docs hygiene scope; diff adds zero) |

## Old import paths confirmed working

- `primer.llm.claude_code.process`: `child_env`, `child_process`, `sweep_orphans`,
  `read_stderr_tail` (lead's ruling 4) plus `ALLOWED`, `CLEANUP_WAIT_S`, `_ChildRecord`,
  `_child_pgid`, `_STREAM_LINE_LIMIT`, `CHILD_IDLE_TIMEOUT_ENV` (the last three are imported
  or monkeypatched by the UNCHANGED claude_code tests: `test_process.py` imports the privates,
  `test_adapter.py:750` does `monkeypatch.setattr("primer.llm.claude_code.process._STREAM_LINE_LIMIT", 1024)`).
- `primer.llm.claude_code.history`: `replay_tool_id`, `plan_replay` (plus `ReplayPlan`,
  `split_tail`).
- `primer.llm.claude_code.bridge`: `alias_for`.
- `primer.api.routers.providers`: `child_env` (untouched, works via the shim).

All of the above are exercised by the unchanged suites above passing green. An explicit
import smoke test (`python -c` importing every listed name from every old path plus the new
`primer.llm.agent_runtime.*` names, checking the pinned `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`
value, the pass-through alias, and the default sweep prefixes) printed `import smoke: OK`.

### The one subtlety worth knowing: `_STREAM_LINE_LIMIT`

`test_adapter.py:750` monkeypatches the constant on the CLAUDE_CODE module at call time.
A plain re-export would make the patch a silent no-op (the shared `child_process` reads its
own module's global). So the shared `child_process` takes a `line_limit` keyword (default:
the shared constant), and the claude_code wrapper forwards ITS module's
`_STREAM_LINE_LIMIT` binding at call time - the monkeypatch target still resolves and the
patch takes effect. The oversize-line test
(`test_oversize_stdout_line_is_classified_not_raw`) is the proof: it failed with
`AttributeError: ... no attribute '_STREAM_LINE_LIMIT'` after the move and passes with the
forwarding wrapper. The brief's `child_process` signature is a subset of the real one
(`line_limit` is an added keyword with a default), so nothing calling it per the brief
changes.

## Concerns

- None blocking. The adapter change (removing the now-redundant `BadRequestError` check)
  was not named in the brief's Files list but is the direct consequence of `plan_replay`
  now raising via `split_tail`; behaviour at the call site is identical (same exception,
  same message, raised before any side effect).
- `plan_replay` now raises `BadRequestError` where it previously returned a plan with an
  invalid tail; every in-tree caller is the adapter (covered), and the raised case is
  exactly the one the adapter used to raise itself.
- The two pre-existing em-dashes in `provider_registry.py` (lines 342, 864) predate this
  change and were left alone; the repo hygiene suite scans `docs/dev/` and `AGENTS.md`,
  not `primer/` source.
