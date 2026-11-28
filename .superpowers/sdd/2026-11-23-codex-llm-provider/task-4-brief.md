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

