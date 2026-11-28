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

