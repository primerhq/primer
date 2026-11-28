# Task 4 report: codex provider type, CodexConfig and the direct-model rule

**Status: done.** Commit `558fa6bb091205cfecfb43741830b5037167ef51`
(`feat(llm): add the codex provider type, config and direct-model rule`) on
`feat/cx-t4` (worktree `../primer-cx-t4`, base
`f45a3b39f`), 10 files changed, 376 insertions, 6 deletions, tree clean.
No `Co-Authored-By` footer; staged exactly the 10 touched files; no em-dash
introduced (the U+2014 hits in `llm.py`/`providers.py` are pre-existing on
untouched lines - the diff-level grep is empty).

## Files

| File | Change |
|---|---|
| `primer/model/providers/llm.py` | `LLMProviderType.CODEX = "codex"` (beside `CLAUDE_CODE`); `AGENT_RUNTIME_TYPES` now `frozenset({CLAUDE_CODE, CODEX})` and its stale comment fixed (no longer claims `bootstrap/seed.py` refuses agent-runtime profiles - agent runtimes are used like any other provider); new `CodexConfig` beside `ClaudeCodeConfig`; `CodexConfig` added to the `LLMProvider.config` union AND to the `_coerce_config_to_provider` mapping dict (both were needed: the union rejects an instance of a member it does not list); `model_catalog_path` documented there |
| `primer/model/provider.py` | `CodexConfig` re-export (import + `__all__`) |
| `primer/llm/codex/__init__.py` | new package, re-exports the rule names from `models` |
| `primer/llm/codex/models.py` | new: the direct-model rule (spec 6.6) |
| `primer/api/routers/providers.py` | new `_types` entry for `codex` beside the `claude_code` one, describing the config fields (`binary_path`, `codex_home`, `pass_env`, `model_catalog_path`) the way the claude_code entry does |
| `primer/api/routers/model_profiles.py` | imports (`LLMProviderType` beside the existing `LLMProvider` import; `is_direct_model` from `primer.llm.codex.models`); new `_check_codex_direct_model` hook beside `_check_provider_exists`; the call added to `_on_pre_create` (order: provider-exists, codex direct-model, aggregation-valid) |
| `tests/model/test_codex_config.py` | new (3 tests + 5 parametrized) |
| `tests/llm/codex/__init__.py`, `tests/llm/codex/test_models.py` | new (4 tests) |
| `tests/api/test_codex_profile_rule.py` | new (2 tests) |

## Signatures

```python
# primer/model/providers/llm.py
LLMProviderType.CODEX = "codex"
AGENT_RUNTIME_TYPES: frozenset[LLMProviderType] = frozenset({CLAUDE_CODE, CODEX})

class CodexConfig(BaseModel):
    binary_path: str = "codex"
    codex_home: str | None = None
    pass_env: list[str]  # comma-separated form string; same before-validator screen as ClaudeCodeConfig
    model_catalog_path: str | None = None

# primer/llm/codex/models.py
CODEX_DIRECT_MODELS: frozenset[str] = frozenset({"gpt-5.5"})
MINIMUM_VERSION = "0.169.0"
CODEX_MAX_TESTED_VERSION: str = "0.169.0"

def is_direct_model(model: str, *, catalog_path: str | None) -> bool
def _catalog_tool_modes(catalog_path: str | None) -> dict[str, str | None]

# primer/api/routers/model_profiles.py
async def _check_codex_direct_model(entity: ModelProfile, request: Request) -> None
```

Behavior, per the lead's rulings:

- `is_direct_model` checks `CODEX_DIRECT_MODELS` first, then the catalog
  entry's `tool_mode == "direct"` (OR semantics per spec 6.6). `gpt-5.5`
  stays direct even on a broken catalog (pinned by a test edge case).
- Catalog format: Codex's `model_catalog_json` - a JSON object with a
  `models` list (entries: `slug` + optional `tool_mode`); a top-level list
  of entries is also accepted. Any read/parse error, missing file, or
  absent model -> `False`, never an exception (`OSError`/`ValueError`
  caught; non-dict/non-list shapes ignored).
- The 422 hook is a no-op unless `entity.kind == "single"`; it fetches the
  provider row and returns unless `provider.provider == LLMProviderType.CODEX`;
  it raises `HTTPException(422, detail={"error": "codex_model_not_direct",
  "field": "model_name", "message": <rule>})` via the existing
  `_aggregation_error` helper - the same house shape as the other hooks, so
  the app's RFC7807 envelope puts all three keys in `extensions` (the tests
  assert on `r.json()["extensions"]`). `field` is `model_name`, the
  `ModelProfile` field name. The message is the lead's rule string verbatim:
  `codex profiles need a direct-tool model: gpt-5.5, or a model the
  provider's model catalog file marks "tool_mode": "direct"`.
- Aggregated profiles need no check: every member is a single profile,
  checked when it is created.
- `CodexConfig`'s `pass_env` screen is copied from `ClaudeCodeConfig`
  (same `_denied_pass_env_name` helper, same before-validator shape).

## Tests

Brief steps followed: failing tests first, then implementation.

**Red (before implementation):**

```
$ uv run pytest tests/model/test_codex_config.py tests/llm/codex/test_models.py tests/api/test_codex_profile_rule.py -n0 -q
1 warning, 3 errors in 0.23s
  E   ImportError: cannot import name 'CodexConfig' from 'primer.model.provider'
  E   ModuleNotFoundError: No module named 'primer.llm.codex'
```

(one more red found after first implementation pass: the `LLMProvider.config`
union rejected `CodexConfig` with 7 `model_type` validation errors until
`CodexConfig` was added to the union - the mapping dict alone is not enough.)

**Focused green (the brief's command):**

```
$ uv run pytest tests/model/test_codex_config.py tests/llm/codex/test_models.py tests/api/test_codex_profile_rule.py -n0 -q
13 passed, 1 warning in 2.05s
```

**Broader (the brief's command):**

```
$ uv run pytest tests/api/test_providers*.py tests/model -n 2 -q -p no:cacheprovider
1023 passed, 1 skipped, 5 warnings in 15.64s
```

**Hygiene:** `uv run ruff check <the 10 files>` -> No issues found. No
subagents used; no other sweeps or e2e run.

## Test coverage vs the brief

All of the brief's named tests are present under the same names:
`test_codex_config_defaults`, `test_pass_env_screens_secrets` (5 cases),
`test_gpt_5_5_is_direct`, `test_catalog_direct_entry_is_direct` (also covers
the top-level-list variant), `test_catalog_non_direct_entry_is_not`,
`test_unreadable_catalog_is_not_direct` (missing file, broken JSON, and the
gpt-5.5-pinned edge case), `test_profile_with_non_direct_model_is_422`
(asserts status, `extensions.error`, `extensions.field == "model_name"`, and
the verbatim rule in `extensions.message`), `test_profile_with_direct_model_is_created`.
One extra beyond the brief's list: `test_codex_row_coerces_config`
guards the new `CODEX -> CodexConfig` mapping branch end to end.

## Concerns

1. **Create-only scope.** The hook runs on `_on_pre_create` only, per the
   lead's ruling and the brief's create-only tests. Spec 6.6 frames the rule
   as the provider's standing behavior; an *update* that re-points an
   existing single codex profile at a non-direct model is not refused at the
   write door. The adapter's per-step re-check (a later task) is the
   backstop, and `_on_pre_update` could gain the same call if the lead wants
   the write door closed for updates too.
2. **`discoverable: False`** on the new `_types` entry: no codex model
   discovery route exists yet this task (claude_code's is `True` because it
   has one). The discovery task should flip it.
3. **`MINIMUM_VERSION == CODEX_MAX_TESTED_VERSION == "0.169.0"`** for now,
   per the lead's ruling; the live-check task raises it on a host with Codex
   installed.
4. The `model_profiles.py` module docstring enumerates the router's "four
   additions"; the new check is not added to that list (the brief scoped this
   file to the hook itself). One-line doc sync if desired.
5. The 422's `field` is `model_name` (the entity's field name), not `model`;
   the console surfaces it from `extensions` either way.

## Fix round 1 (task 01a20771)

Commit `879a578d2b05c056ba20f18ac192f0c8b7630bfa`
(`fix(llm): refuse non-direct codex models on every profile write path`) on
`feat/cx-t4`, on top of `558fa6bb0`, 6 files changed, 270 insertions,
31 deletions, tree clean, ruff clean, no em-dash introduced (diff-level
grep: 0).

### CRITICAL 1: the rule enforced only on POST

**What changed.** The check moved into `primer/model_profile/checks.py` as
`async def check_codex_direct_model(entity, *, storage_provider) -> None`
raising `EntityCheckError` via the file's `_refuse` helper (code
`codex_model_not_direct`, field `model_name`, the same message text). It is
called from both `check_profile_on_create` (after `check_provider_exists`,
before `check_aggregation_valid`, the router's order) and
`check_profile_on_update` (same relative position, after
`check_kind_unchanged` + `check_provider_exists`). All four write paths are
covered: REST create (the router's `_on_pre_create` now calls a THIN
`_check_codex_direct_model` wrapper that delegates to the shared check and
re-raises `_as_rest_error` - the house pattern of the other two hooks, so
the 422 wire body is byte-identical), REST update (`_on_pre_update` already
runs `check_profile_on_update`, which now includes the check), and the
system tools `system__create_model_profile` / `system__update_model_profile`
(`primer/toolset/system.py:536,539` call the two composites directly and
never reach the router). The router-local logic (storage fetch,
`is_direct_model` call, message text) was removed; the router no longer
imports `is_direct_model` or `LLMProviderType`. The module docstring now
lists the rule as a fifth addition (create AND update, no-op for
aggregateds) and `checks.py`'s docstring bullet list gained the check.

### IMPORTANT 1: RecursionError escapes the reader

**What changed.** `except (OSError, ValueError)` in
`_catalog_tool_modes` (models.py) is now `except (ValueError, RecursionError)`
- `RecursionError` is a `RuntimeError`, not a `ValueError`, and
`json.loads` raises it on a deeply nested file, which on the profile route
was a 500 instead of "not direct".

### IMPORTANT 2: unbounded file read

**What changed.** `_catalog_tool_modes` now, before opening: `os.stat` the
path and treat anything that is not a regular file (`stat.S_ISREG` - a FIFO
would block the read, a directory cannot be read) as not direct; then read
at most `MAX_CATALOG_BYTES + 1` bytes (new module constant
`MAX_CATALOG_BYTES = 4 * 1024 * 1024`) and treat a longer file as not
direct. `read_text()` on the whole file is gone.

### Covering tests

- `tests/llm/codex/test_models.py`: `test_deeply_nested_catalog_is_not_direct`
  (`"[" * 200_000`, the RecursionError file),
  `test_over_cap_catalog_is_not_direct` (`MAX_CATALOG_BYTES + 2` bytes),
  `test_directory_is_not_direct`, `test_fifo_is_not_direct`
  (`os.mkfifo` in `tmp_path`; the stat gate refuses before any open, so it
  neither raises nor blocks).
- `tests/api/test_codex_profile_rule.py`: `test_update_repoint_to_codex_non_direct_is_422`
  (profile created on an anthropic provider, PUT re-pointed at the codex
  provider with `gpt-5.4-codex` -> 422 with the same error/field/message,
  stored row untouched), `test_profile_on_non_codex_provider_is_created`
  (any model on a non-codex provider still creates),
  `test_catalog_direct_model_is_created` (codex provider whose
  `model_catalog_path` file marks `frontier-x` `"tool_mode": "direct"`
  accepts that model - the config wiring end to end).
- `tests/model_profile/test_codex_direct_check.py` (new; the directory
  exists): `test_check_profile_on_create_refuses_non_direct_codex` and
  `test_check_profile_on_update_refuses_non_direct_codex` call
  `check_profile_on_create` / `check_profile_on_update` directly with a
  stand-in storage provider and expect `EntityCheckError` with code
  `codex_model_not_direct`, field `model_name` (the system tools' path);
  plus `test_check_profile_on_create_allows_direct_codex` (the verified
  `gpt-5.5` still passes both composites).

### Commands and outputs

```
$ uv run pytest tests/model/test_codex_config.py tests/llm/codex/test_models.py tests/api/test_codex_profile_rule.py tests/model_profile/test_codex_direct_check.py -n0 -q -p no:cacheprovider
23 passed, 1 warning in 2.75s

$ uv run pytest tests/api/test_model_profile_kind_immutable.py tests/api/test_model_profiles_aggregation.py tests/api/test_model_profiles_router.py tests/api/test_model_profile_choices.py tests/api/test_rest_validator_wire_bodies.py -n0 -q -p no:cacheprovider
53 passed, 1 warning in 21.73s

$ uv run ruff check <the 6 touched files>
Ruff: No issues found
```

The five existing files are the grep finds for `primer/model_profile/checks.py`
(`test_model_profile_kind_immutable.py` exercises `check_profile_on_update`;
`test_rest_validator_wire_bodies.py` references the checks module) and the
`model_profiles` router (`test_model_profiles_router.py`,
`test_model_profiles_aggregation.py`, `test_model_profile_choices.py`),
run with `-n0` as instructed. No other sweeps, no e2e, no subagents.

### Concerns (round 1)

- The check is now on every write path per spec 6.6 line 261 (create OR
  update, both doors); the round-1 "create-only" concern is resolved.
- `MAX_CATALOG_BYTES` (4 MiB) is a fixed cap: a legitimate catalog larger
  than 4 MiB is treated as not direct (fail-closed); raise the constant if
  Codex catalogs outgrow it.
- The shared check re-reads the catalog file on every profile write that
  names a codex provider (no cache): cheap for operator-sized files, and
  re-reading keeps a just-edited catalog effective without a restart.
