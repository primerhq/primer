"""merge_pending_dispatch: agent-yield dispatch entries are derived, not stored.

The checkpoint persists ``pending_dispatch`` only for tool-call nodes (they
bake the graph node's tool_id, which the channel layer can't recompute).
Agent yields are derived from ``pending_agent_yields`` so their
resume_metadata lives in the blob once. Old blobs that still carry
agent-yield entries in ``pending_dispatch`` keep working because the channel
dispatcher dedups by tool_call_id.
"""

from __future__ import annotations

from primer.worker.yield_runtime import merge_pending_dispatch


def test_merges_stored_toolcalls_with_derived_agent_yields():
    checkpoint = {
        "pending_dispatch": [
            {
                "kind": "_approval",
                "tool_call_id": "tc-tool",
                "resume_metadata": {
                    "original_call": {
                        "id": "tc-tool", "name": "fs__write", "arguments": {},
                    }
                },
            }
        ],
        "pending_agent_yields": [
            {
                "node_id": "n1",
                "tool_call_id": "tc-ask",
                "event_key": "ask_user:s:tc-ask",
                "tool_name": "ask_user",
                "resume_metadata": {"prompt": "color?"},
                "llm_messages": [],
                "iteration": 0,
            }
        ],
    }
    merged = merge_pending_dispatch(checkpoint)
    by_tcid = {p["tool_call_id"]: p for p in merged}
    assert set(by_tcid) == {"tc-tool", "tc-ask"}
    # tool-call entry carries the baked graph tool_id
    assert by_tcid["tc-tool"]["resume_metadata"]["original_call"]["name"] == "fs__write"
    # agent-yield entry derived from pending_agent_yields
    assert by_tcid["tc-ask"]["kind"] == "ask_user"
    assert by_tcid["tc-ask"]["resume_metadata"] == {"prompt": "color?"}


def test_old_blob_with_agent_yields_in_dispatch_does_not_duplicate_after_dedup():
    # A pre-slim checkpoint: pending_dispatch already holds the agent-yield
    # entry. merge appends a derived duplicate, but each tool_call_id appears
    # for the consumer to dedup; assert the duplicate is the SAME tcid so the
    # channel dispatcher's per-tcid dedup collapses it.
    checkpoint = {
        "pending_dispatch": [
            {
                "kind": "ask_user",
                "tool_call_id": "tc-ask",
                "resume_metadata": {"prompt": "color?"},
            }
        ],
        "pending_agent_yields": [
            {
                "node_id": "n1",
                "tool_call_id": "tc-ask",
                "event_key": "ask_user:s:tc-ask",
                "tool_name": "ask_user",
                "resume_metadata": {"prompt": "color?"},
                "llm_messages": [],
                "iteration": 0,
            }
        ],
    }
    merged = merge_pending_dispatch(checkpoint)
    tcids = [p["tool_call_id"] for p in merged]
    # Both entries are present but share the tcid; the dispatcher dedups by it.
    assert tcids.count("tc-ask") == 2
    assert len(set(tcids)) == 1


def test_empty_checkpoint_yields_empty_list():
    assert merge_pending_dispatch({}) == []
