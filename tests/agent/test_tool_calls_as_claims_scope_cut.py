"""tool_calls_as_claims_enabled is TOP-LEVEL ONLY (01a0518b).

Unlike turn_no (pure bookkeeping, ambiguity-free to inherit), this flag
ARMS machinery - specifically a tool_wait park, which is session-anchored
(parked_state, the re-arm event, the resume coordinator all key off a
session row). A nested subagent turn (system__invoke_agent) has no
session row of its own to park on, so it must NEVER receive this flag -
the same deliberate scope-cut class as artifact_storage's own cut for
that exact surface (see run_agent_turn's docstring).

Two invariants pinned here:

* Structural: run_subagent / resume_subagent don't even ACCEPT the flag
  as a parameter - the scope-cut is enforced by the signature itself,
  not by a runtime check someone could accidentally bypass.
* Propagation: the executors DO thread it correctly at the top level
  (construction time, mirroring artifact_storage/turn_no's own pattern),
  and a subgraph node's child executor inherits it (same session row as
  its parent, unlike a subagent turn - see _build_sub_executor).
"""

from __future__ import annotations

import inspect

from primer.agent.invoke import resume_subagent, run_subagent


def test_run_subagent_has_no_tool_calls_as_claims_param() -> None:
    params = inspect.signature(run_subagent).parameters
    assert "tool_calls_as_claims_enabled" not in params, (
        "run_subagent must never accept this flag - a nested subagent "
        "turn has no session row to park a tool_wait batch on"
    )


def test_resume_subagent_has_no_tool_calls_as_claims_param() -> None:
    params = inspect.signature(resume_subagent).parameters
    assert "tool_calls_as_claims_enabled" not in params, (
        "resume_subagent must never accept this flag - same reasoning "
        "as run_subagent"
    )
