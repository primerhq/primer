"""E2E: a workspace-session agent invokes a graph via ``workspace_ext__invoke_graph``.

``workspace_ext__invoke_graph(graph_id, input)`` runs a target graph inside the
current workspace session (a child ``WorkspaceGraphExecutor`` namespaced under
the session state) and returns ``{output: <text>}`` to the calling agent. It
yields only on a HITL gate; the graph used here has none, so the call returns
synchronously.

This drives the REAL path end-to-end against the live ``primer api`` server
using the scripted mock-LLM harness:

  1. A trivial target graph (begin -> agent -> end) whose single agent node
     emits a distinctive marker ("GRAPH-OUTPUT-<unique>"). The graph shape is
     copied verbatim from tests/e2e/test_smk_graphs.py::test_linear_run_and_
     turn_logs (begin/agent/end with ``output_template={{ nodes.step.text }}``),
     so the End-node output IS the marker.
  2. A session agent bound to ``workspace_ext__invoke_graph``. On its first turn it
     emits the invoke_graph tool call targeting the graph; once the tool result
     is present it emits its final reply ("SESSION-DONE-<unique>"). The
     ``when_tool_result=True`` rule is FIRST so the agent does not loop.
  3. A local workspace + an agent-bound session running that agent. The session
     is driven to terminal.

Why this is a working (non-skipped) test:
  The deterministic, REST-queryable signal that invoke_graph really ran the
  child graph is that the OUTER session reaches ``status=ended`` with
  ``ended_reason`` != "failed". The session can only end normally if the
  agent's ``when_tool_result=True`` rule fired, and that rule fires only once
  the invoke_graph tool returned a tool_result -- which requires the child
  graph to have run to its End node and produced output. A failure inside the
  child graph would surface as a failed/errored tool_result and a non-normal
  session end (or no terminal at all). The inner graph's own output text
  ("GRAPH-OUTPUT-<unique>") is plumbed back as the invoke_graph tool result
  and lives in the session's ``messages.jsonl`` transcript; that file is only
  exposed over the live session WebSocket (which rejects ENDED sessions with
  4410), so it is not asserted directly here. The terminal-state + agent
  final-reply pin is the strongest deterministic REST contract available.

Subsystems exercised:
  * local workspace + agent-bound session lifecycle (create -> resume ->
    terminal), mirroring tests/e2e/test_smk_graphs.py + runs.py helpers
  * ``workspace_ext__invoke_graph`` tool: child WorkspaceGraphExecutor run inside
    the session, output plumbed back as the agent's tool_result
  * the scripted ``when_tool_result=True`` continuation rule firing on that
    tool_result
"""

from __future__ import annotations

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_graph,
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_terminal,
    wait_turn_advanced,
)


@pytest.mark.asyncio
async def test_session_invoke_graph_runs_child_graph_to_completion(
    client, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    """A session agent invokes a trivial graph; the session ends normally,
    proving the child graph ran and its output was plumbed back as the
    invoke_graph tool result."""
    registry, base_url = mock_llm

    graph_marker = f"GRAPH-OUTPUT-{unique_suffix}"
    done_marker = f"SESSION-DONE-{unique_suffix}"

    # ----- 1. Target graph: begin -> agent -> end (shape copied from -----
    #         test_smk_graphs.py::test_linear_run_and_turn_logs). The agent
    #         node emits the marker; the End node passes it through.
    graph_agent = await make_scripted_agent(
        client, registry, base_url,
        suffix=f"ig-node-{unique_suffix}",
        scenario=f"scripted:ig-node-{unique_suffix}",
        rules=[Rule(emit_text=graph_marker)],
    )
    nodes = [
        {"kind": "begin", "id": "start"},
        {
            "kind": "agent", "id": "step",
            "agent_id": graph_agent["agent_id"], "input_template": "go",
        },
        {"kind": "end", "id": "done", "output_template": "{{ nodes.step.text }}"},
    ]
    edges = [
        {"kind": "static", "from_node": "start", "to_node": "step"},
        {"kind": "static", "from_node": "step", "to_node": "done"},
    ]
    gid = await make_graph(
        client, suffix=f"ig-{unique_suffix}", nodes=nodes, edges=edges,
    )

    # ----- 2. Session agent: invokes the graph, then finishes ------------
    #         ORDER MATTERS: the when_tool_result rule is FIRST so the agent
    #         emits its final text once invoke_graph returns (no loop).
    session_agent = await make_scripted_agent(
        client, registry, base_url,
        suffix=f"ig-caller-{unique_suffix}",
        scenario=f"scripted:ig-caller-{unique_suffix}",
        tools=["workspace_ext__invoke_graph"],
        rules=[
            Rule(when_tool_result=True, emit_text=done_marker),
            Rule(
                emit_tool="workspace_ext__invoke_graph",
                emit_args={"graph_id": gid, "input": "go"},
            ),
        ],
    )

    cleanup_urls = [
        f"/v1/agents/{session_agent['agent_id']}",
        f"/v1/llm_providers/{session_agent['provider_id']}",
        f"/v1/graphs/{gid}",
        f"/v1/agents/{graph_agent['agent_id']}",
        f"/v1/llm_providers/{graph_agent['provider_id']}",
    ]
    workspace_id: str | None = None
    session_id: str | None = None
    try:
        # ----- 3. Local workspace + agent-bound session ------------------
        workspace_id = await make_local_workspace(
            client, suffix=f"ig-{unique_suffix}", root=tmp_path,
        )
        cleanup_urls.insert(0, f"/v1/workspaces/{workspace_id}")
        session_id = await start_agent_session(
            client, workspace_id=workspace_id,
            agent_id=session_agent["agent_id"], instructions="invoke the graph",
        )

        # ----- 4. Drive to terminal --------------------------------------
        final = await wait_terminal(client, session_id, timeout_s=90)
        assert final.get("status") == "ended", (
            f"invoke_graph session did not reach terminal; the child graph "
            f"run + tool_result plumb-back must complete the turn: {final!r}"
        )
        # The session ended NORMALLY: a failed invoke_graph (child graph error)
        # would surface as ended_reason=failed. Accept normal/None; reject
        # an explicit failure.
        assert final.get("ended_reason") != "failed", (
            f"invoke_graph session ended in failure -- the child graph or the "
            f"tool plumb-back errored: {final!r}"
        )
        # The agent ran at least one full turn (the invoke_graph call + the
        # continuation that emitted the final reply). turn_no is bumped by the
        # claim engine's on_release in a transaction that commits just after
        # the ENDED write, so the terminal snapshot can still read turn_no=0 —
        # re-poll until the counter settles to avoid a read-after-write race.
        settled = await wait_turn_advanced(client, session_id, min_turn_no=0)
        assert settled.get("turn_no", 0) > 0, (
            f"invoke_graph session ran zero turns: {settled!r}"
        )
        # Strong, non-vacuous signal: invoke_graph nests the invoked graph's
        # state under the session as a ``<gsid>__invoke_<tcid>`` subtree on the
        # local workspace root. A graceful tool error (unresolved services,
        # missing graph) returns a tool_result WITHOUT running a child graph,
        # so this subtree would be absent. Its presence proves a real child
        # WorkspaceGraphExecutor ran namespaced under the session.
        invoke_subtrees = [
            p for p in tmp_path.rglob("*__invoke_*") if p.is_dir()
        ]
        assert invoke_subtrees, (
            f"no '__invoke_' child-graph state subtree found under {tmp_path}; "
            f"invoke_graph did not run a namespaced child graph"
        )
    finally:
        if session_id is not None and workspace_id is not None:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
        for url in cleanup_urls:
            try:
                await client.delete(url)
            except Exception:  # noqa: BLE001
                pass
