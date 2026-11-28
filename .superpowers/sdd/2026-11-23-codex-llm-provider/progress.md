# SDD ledger - plan: docs/superpowers/plans/2026-11-23-codex-llm-provider.md
Spec: docs/superpowers/specs/2026-11-16-codex-llm-provider-design.md (Version 1, 2026-11-23)
Base: main e51e36664. Integration branch feat/codex-provider, worktree ../primer-codex. Implementers: OC1 01a1dac6-b098-7473-81c0-aff66b30f807, OC2 01a1dac6-a452-7e33-9e6a-33e2dca65213.

## Pre-flight scan
| Pair / task | Shared | Finding | Ruling |
|---|---|---|---|
| T1 <-> T4 | primer/llm/codex/ | T1 deletes, T4 recreates | T4 after T1 merges |
| T2 -> T6/T7/T8 | child_process, sweep_orphans, split_tail, tool_alias | plan's Codex alias rule replaces disallowed chars; T2 signature lacks it | tool_alias gains sanitize: bool = False; Codex passes True; claude_code unchanged |
| T3 <-> T4 | CODEX_MAX_TESTED_VERSION | T3 (wave 1) sets a constant T4 creates | T4 creates it = "0.169.0"; T3 raises it after the live run |
| T2 <-> T8 | provider_registry.py | both touch | sequential by waves |
| T4 <-> T9 | primer/api/routers/providers.py | both touch | sequential by waves |
| T2 self | child_env | claude_code adds CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT | passed through the new `extra` mapping |
| T1, T2, T4 self | - | tests match code; files consistent | clean |

## Rulings
- Ruling: tool_alias(scoped_id, *, max_len, keep_len, sanitize=False) - the plan's Codex alias needs character replacement the claude rule must not change - cost if wrong: one signature change in T7.
- Ruling: CODEX_MAX_TESTED_VERSION = "0.169.0" in T4, raised by T3 - the constant must exist before T3 can set it - cost if wrong: none.
- Ruling: this host has no Codex CLI: execute T1, T2, T4 here; T3 and every task that needs its recordings (T5-T12) wait for the Codex host - the spec requires fixtures from real recordings (section 10) - cost if wrong: idle time until the Codex host runs T3.

## Progress
- T1 dispatched to OC2 (team task), worktree ../primer-cx-t1, BASE e51e36664.
- T2 dispatched to OC1 (team task), worktree ../primer-cx-t2, BASE e51e36664 (rulings: tool_alias sanitize, child_env extra, child_process prefix/files, old import paths kept).
- T1 implementer DONE: e26dfbe6f (OC2; 26 files -5558; docs 3227 passed; tests/llm 1653 passed + 2 pre-existing Anthropic env failures). PR #1066 touched only the two docs outside its package (no deps). Reviewer dispatched (sonnet, BASE e51e36664).
- Task 1: complete (commits e51e366..e26dfbe, review clean); merged into feat/codex-provider at f45a3b39f; docs 3227 passed.
- Task 1: minor (noted, not ours): six wiki route-style links in docs/wiki/providers/agent-runtimes.md do not resolve as files (pre-existing route links).
- T4 dispatched to OC2 (team task 01a20760), worktree ../primer-cx-t4, BASE f45a3b39f. Catalog format verified at d32063f: {"models": [{slug, tool_mode, ...}]}; gpt-5.5 tool_mode null, the rest code_mode_only.
- T2 implementer DONE: 58b0a0a43 (OC1; 13 files; agent_runtime 5 passed; claude_code + status route 120 passed unchanged). Deviations: removed claude_code adapter's redundant post-plan_replay check; child_process line_limit kwarg so the _STREAM_LINE_LIMIT monkeypatch still works. Reviewer dispatched (sonnet, BASE e51e36664).
- T2 review (sonnet): VERDICT clean, no Important. Merged feat/cx-t2 into feat/codex-provider at e7c0a1bef; merged tree agent_runtime + claude_code + status route 125 passed.
- Parked minor (T11 owns): docs/dev/subsystems/model-providers.md:105,107 still place lifecycle/replay in claude_code/process.py and history.py; point them at primer/llm/agent_runtime/.
- Parked minor (T6 owns): add a test pinning claude_code TMP_PREFIX and the codex prefix in agent_runtime DEFAULT_SWEEP_PREFIXES, plus a tampered child.json under primer-codex left alone.
- Parked minor (T7 owns): tool_alias sanitize=True boundary cases (len 64, 65, long all-safe id).
Task 2: complete (commits e51e366..58b0a0a, review clean)
- T4 implementer DONE: 558fa6bb0 (OC2; 10 files +376/-6; focused 13 passed; tests/api/test_providers*.py + tests/model 1023 passed). OC2 concerns: hook create-only; discoverable False; extra coerce test; module docstring. Ruling: the 422 must also fire on update - spec 6.6 line 261 says 'Creating or updating' - cost if wrong: none, it is the spec text. Goes into the T4 fix round with the reviewer's findings. Reviewer dispatched (sonnet, BASE f45a3b39f).
- T4 review (sonnet): changes requested. Critical 1: 422 only on router create (not update, not system tools). Important 1: RecursionError escapes reader. Important 2: unbounded read / FIFO. Ruling for Important 2: S_ISREG check + 4 MiB cap, longer = not direct.
- Task 4: minor (deferred): reader accepts a top-level JSON list; upstream format is {"models": [...]} only (codex would reject the file).
- Task 4: minor (deferred): 422 tests do not assert ProblemDetails status/title.
- Task 4: minor (deferred): _types discoverable False until T9 discovery flips it.
- T4 fix round 1 dispatched to OC2 (team task 01a20771), FIX_BASE 558fa6bb0.
- T4 fix round 1 implementer DONE: 879a578d2 (6 files +270/-31; focused 23 passed; router/checks files 53 passed). Scoped re-review dispatched (sonnet, FIX_BASE 558fa6bb0).
- Task 4: fix round 1/5 (3 addressed, 0 open - 422 on all four write paths via primer/model_profile/checks.py; RecursionError caught; S_ISREG + 4 MiB cap; commits 558fa6b..879a578)
- Task 4: minor (deferred): stat-then-open window, no O_NONBLOCK/fstat recheck; a path swapped to a FIFO between stat and open would block (needs write access to the operator's path).
- Task 4: minor (deferred): is_direct_model is a sync read in async handlers (bounded 4 MiB, regular files only).
Task 4: complete (commits f45a3b3..879a578, review clean after 1 fix round). Merged into feat/codex-provider at 9803133f5; merged-tree focused codex + profile + agent_runtime + claude_code 228 passed. Full sharded sweep + docs running.
- Merged-tree sweep at 9803133f5 (4 shards -n 2 --dist load + docs): docs 3227 passed; shards 55 failed rows / 53 ids. Rerun of those files on clean main e51e36664: 49 fail there too (host). 4 REGRESSIONS from T4: tests/ui/test_register_common.py (every served kind has a console name; served kinds order) and tests/ui/test_register_llm.py (excluded kinds; type select never lists agent runtimes) - the server now serves llm kind "codex" with no console label.
- Ruling: bring forward the classification slice of T10 as a T4 follow-up (codex labelled "Codex" in PC_KIND_LABELS llm and agent_runtime like claude_code, PC_CLASSES.agent_runtime.kinds, llm excludeKinds) and update the pinned served-kinds lists - the sweep must be green at every commit (AGENTS.md section 5) and T10 already specifies this classification - cost if wrong: T10 re-touches two lines. Form fields, status block, import list stay in T10.
- T4b dispatched to OC2, worktree ../primer-cx-t4b, branch feat/cx-t4b, BASE 9803133f5.
- T4b implementer DONE: 6cd43e421 (OC2; 3 files +45/-27; focused 118 passed; tests/ui 7172 passed, 1 known SIGILL). OC2 concern: agent_runtime wizard offers codex but renders the generic LLM form until T10. Reviewer dispatched (sonnet, BASE 9803133f5), asked to judge that concern and grep ui/ for missed claude_code classification sites.
- T4b review (sonnet): VERDICT clean; grep of ui/ found no missed classification site; wizard sends a valid CodexConfig (four fields, blanks omitted).
- Task 4b: minor (deferred, T9 owns): Next in the Agent runtimes wizard for codex hits 400 from _discover_models (no codex branch in _dispatch_llm_probe); its message points at a "Suggest models" fallback that does not exist. "Register without models" works.
- Task 4b: minor (deferred, T10 owns): no Codex disclosure line at register-llm.jsx:292; runtime panel register-llm.jsx:378-384 hardcodes "Claude Code" and must become kind-aware when codex discovery returns a runtime payload.
Task 4b: complete (commits 9803133..6cd43e4, review clean). Merged at d4ad2cff0; the 4 sweep regressions + codex tests 168 passed on the merged tree.
STOP POINT on this host: T1, T2, T4, T4b merged on feat/codex-provider (local only, not pushed). Next: T3 on the Codex host.

## 2026-11-26: branch rebuilt on main (read before the next task)
- The repo only rebase-merges, and main's #1077 (a974b3920, "Extract shared agent runtime helpers for Claude Code")
  landed its own `primer/llm/agent_runtime/` package. feat/codex-provider was force-pushed as a linear branch: our
  Task 1, Task 4 (two commits), Task 4b and the handover docs replayed onto main; our Task 2 commit (58b0a0a43) was
  dropped because main's package replaces it. The old history survives only as the local branch
  feat/codex-provider-history on the review machine. On the Codex machine: rebase any unpushed work onto the new
  branch, then `git fetch && git reset --hard origin/feat/codex-provider`.
- Task 2 is now main's version. Differences the remaining tasks must follow (the plan and spec still describe ours):
  - `sweep_orphans(tmp_root=None, *, prefix)` is per provider with a required `prefix`; there is no
    DEFAULT_SWEEP_PREFIXES. Codex needs its own `primer/llm/codex/process.py` wrapper (prefix "primer-codex") and its
    own registry sweep wiring, mirroring `primer/llm/claude_code/process.py` and
    `primer/api/registries/provider_registry.py`; do not extend the Claude sweep.
  - `child_process` and the sweep validate the prefix and file names as single path components and reserve
    `stderr.log` and `child.json`.
  - `plan_replay` no longer raises on an empty or assistant-ending tail; the adapter validates the tail itself (see
    `primer/llm/claude_code/adapter.py`, "history must end with a user or tool message"). `split_tail` still raises.
- Verified on the rebuilt branch: ruff, lint-imports, tests/llm + provider API + docs + the branch's test files
  5165 passed.
