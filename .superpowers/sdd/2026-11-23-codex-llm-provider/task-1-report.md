# Task 1 report: remove the unregistered PR #1066 Codex package

**Status: done.** Commit `e26dfbe6f` `refactor(llm): remove the unregistered Codex
broker package` on `feat/cx-t1` (worktree `../primer-cx-t1`,
base `e51e36664`), 26 files changed, 5558 deletions, 0 insertions, worktree clean.

## What was removed

`git rm -r primer/llm/codex tests/llm/codex` (24 files) plus the two doc sections
that PR #1066 added (both proven by `git log -S` to come from `2f7dcf989`
"feat(llm): add executor-free ChatGPT plan bridge foundation"):

| File | Detail |
|---|---|
| `primer/llm/codex/{__init__,adapter,boundary,bridge,harness,lifecycle,request}.py` | the package body: HTTPS client, `ModelOnlyBridge`, `CodexFoundationLLM` adapter, harness, lifecycle |
| `primer/llm/codex/README.md` | the package's own doc |
| `primer/llm/codex/broker/{__init__,auth,host,http,security,service}.py` | the credential broker candidate |
| `primer/llm/codex/broker/{README.md,HOST.md}` | broker docs |
| `tests/llm/codex/{__init__,test_boundary,test_bridge,test_broker,test_broker_recovery,test_cleanup_ancestry,test_host,test_stream_ownership}.py` | the package's own tests |
| `docs/dev/subsystems/model-providers.md` | the trailing `## Codex / ChatGPT plan foundation (not registered)` section (heading + the paragraph the brief names at lines 772-782; the heading itself is #1066 wording, added by `2f7dcf989`, so it went with the section). Every other "codex" mention in the file (OpenAI codex models) is untouched. |
| `docs/wiki/providers/agent-runtimes.md` | the trailing unheaded paragraph "Codex / ChatGPT plan support is not currently available as a registered Primer provider. ..." (also `2f7dcf989`). The page's remaining content is untouched. |

The relative links `../../../primer/llm/codex/README.md` and
`../../../primer/llm/codex/broker/README.md` die with their paragraph, so the
docs suite's link-resolution gate is satisfied.

## Importers check (the stop condition)

The before-grep below shows every importer is inside the two removed trees.
No importer outside `primer/llm/codex/` or `tests/llm/codex/` was found, so
nothing was edited outside the package and the docs.

## Grep before (on `e51e36664`)

Command (exactly the brief's):

```
grep -rn "llm.codex\|llm/codex\|codex/broker\|CodexFoundationLLM\|ModelOnlyBridge" --include='*.py' --include='*.md' .
```

(excluded: `docs/superpowers/` per the brief, and `.venv/`). 113 matching lines
in 19 files, all self-contained:

```
docs/dev/subsystems/model-providers.md:3        # lines 772, 781, 782 (the #1066 section)
primer/llm/codex/README.md:4
primer/llm/codex/adapter.py:6                   # class CodexFoundationLLM(LLM), ModelOnlyBridge import
primer/llm/codex/bridge.py:5                    # class ModelOnlyBridge
primer/llm/codex/broker/auth.py:1
primer/llm/codex/broker/host.py:3
primer/llm/codex/broker/http.py:2
primer/llm/codex/broker/security.py:2
primer/llm/codex/broker/service.py:5
primer/llm/codex/harness.py:5
primer/llm/codex/lifecycle.py:1
primer/llm/codex/request.py:1
tests/llm/codex/test_boundary.py:12
tests/llm/codex/test_bridge.py:9
tests/llm/codex/test_broker.py:8
tests/llm/codex/test_broker_recovery.py:7
tests/llm/codex/test_cleanup_ancestry.py:10
tests/llm/codex/test_host.py:21
tests/llm/codex/test_stream_ownership.py:8
```

(Per-file counts from `git grep -c -E "llm\.codex|llm/codex|codex/broker|
CodexFoundationLLM|ModelOnlyBridge" e51e36664 -- '*.py' '*.md'`,
`docs/superpowers/` excluded; the full line list was the first grep run,
113 lines.)

## Grep after (working tree at `e26dfbe6f`)

Same command: **0 lines**. No hit outside `docs/superpowers/` (and none inside
the removed trees either, since they are gone).

## Verification (the brief's commands, exact)

```
$ uv run python -c "import primer.api.app"
IMPORT-OK   # exit 0, no output

$ uv run pytest tests/docs -q -n 2 -p no:cacheprovider
Pytest: 3227 passed, 0 failed, 133 skipped in 19.82s

$ uv run pytest tests/llm -q -n 2 -p no:cacheprovider --tb=no -rf
FAILED tests/llm/test_adapter_characterization.py::TestAnthropic::test_request_and_stream[no-connect-timeout]
FAILED tests/llm/test_adapter_characterization.py::TestAnthropic::test_request_and_stream[connect-timeout]
2 failed, 1653 passed, 2 skipped, 2 warnings in 26.57s
```

### The two tests/llm failures are pre-existing environment failures, not import errors

- Collection of `tests/llm` is clean (1653 tests executed; the removed package
  had no importers, so no `ImportError`/collection error anywhere).
- The two failures assert the literal URL `https://api.anthropic.com/v1/messages`
  while the request goes to `http://127.0.0.1:8787/v1/messages`: this host's
  environment sets `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` (plus
  `HEADROOM_BACKEND=anthropic`), which overrides the adapter's base URL.
- Proof on base: with all of my changes stashed (tree back at `e51e36664`), the
  same node ids fail in 1.31s:
  `uv run pytest "tests/llm/test_adapter_characterization.py::TestAnthropic::test_request_and_stream" -q -n0 --tb=no -rf`
  -> `2 failed in 1.31s`. Changes were then popped and re-staged.

## Commit

```
$ git add -u        # the 24 git rm'd files + the two doc edits (nothing else in the tree)
$ git commit -m "refactor(llm): remove the unregistered Codex broker package"
ok 26 files changed, 5558 deletions(-)
ok e26dfbe
```

No `Co-Authored-By` footer; conventional message exactly as the brief specifies;
only the 26 touched/deleted files staged; no em-dash introduced (the two edited
doc files were deletion-only edits and checked with a U+2014 grep).

## Concerns

- The two `TestAnthropic::test_request_and_stream` failures will appear on any
  `tests/llm` run on this host regardless of this change (env override above).
  CI without that env var is expected green.
- `docs/superpowers/` still references the removed package (excluded from the
  grep per the brief); left untouched as instructed.
