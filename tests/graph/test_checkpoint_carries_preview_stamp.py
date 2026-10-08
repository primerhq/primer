"""The graph checkpoint's own ``original_call`` writers carry the preview stamp (design note 01a11cd3-66b0, slice 1 condition (a)).

``approval_resume_metadata`` is the one builder of an approval park's metadata, and it stamps ``preview``. The graph executor builds TWO ``original_call`` blocks of its own for a
suspended TOOL_CALL node, from the node's ``tool_id`` and the pending call's arguments:

* ``_build_pending_park_yield`` (the outer ``_approval`` yield whose ``resume_metadata`` ``GET /v1/yields/pending`` reads for the card);
* ``_toolcall_dispatch_entry`` (the channel / REST dispatch entry of each pending call).

Both dropped every key but ``original_call``. The pending call keeps the ``Yielded.resume_metadata`` the tool manager's gate stamped (``_PendingToolCall.resume_metadata``), so both
now pass the ``preview`` key on; a call parked without one (a park from before the field) stays unstamped and takes the default rule at read time. The agent-node yields already copy
their whole ``resume_metadata``.

``tests/agent/test_original_call_writers.py`` fails when a new writer of ``original_call`` appears that nobody settled.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from primer.graph._checkpoint import _CheckpointMixin
from primer.graph._node_refs import _PendingAgentYield, _PendingToolCall

STAMP = {"paths": ["path", "mode"], "source": "tool"}


class _Executor(_CheckpointMixin):
    """The part of the executor the two writers read."""

    def __init__(self, pending: list[_PendingToolCall], agent_yields: list[_PendingAgentYield] | None = None) -> None:
        self._pending_toolcalls = pending
        self._pending_agent_yields = agent_yields or []

    def _resolve_node_def(self, node_id: str):
        return SimpleNamespace(tool_id="crud__create_agent")

    def snapshot_state(self) -> dict[str, Any]:
        return {}


def _pending(resume_metadata: dict[str, Any] | None, tool_name: str | None = "_approval") -> _PendingToolCall:
    return _PendingToolCall(
        node_id="node-1", tool_call_id="tc-1", parked_event_key="tool_approval:s:tc-1", arguments={"path": "/a", "note": "secret words"},
        tool_name=tool_name, resume_metadata=resume_metadata or {},
    )


def test_the_outer_park_carries_the_stamp_of_the_first_pending_call() -> None:
    yld = _Executor([_pending({"policy_id": "p-1", "preview": STAMP})])._build_pending_park_yield()

    meta = yld.yielded.resume_metadata
    assert meta["preview"] == STAMP
    assert meta["original_call"] == {"id": "tc-1", "name": "crud__create_agent", "arguments": {"path": "/a", "note": "secret words"}}


def test_the_outer_park_of_a_call_parked_without_a_stamp_stays_unstamped() -> None:
    yld = _Executor([_pending({"policy_id": "p-1"})])._build_pending_park_yield()

    assert "preview" not in yld.yielded.resume_metadata


def test_the_dispatch_entry_carries_the_stamp() -> None:
    entry = _Executor([])._toolcall_dispatch_entry(_pending({"policy_id": "p-1", "preview": STAMP}))

    assert entry["kind"] == "_approval"
    assert entry["resume_metadata"]["preview"] == STAMP
    assert entry["resume_metadata"]["original_call"]["name"] == "crud__create_agent"


def test_the_dispatch_entry_of_a_call_parked_without_a_stamp_stays_unstamped() -> None:
    entry = _Executor([])._toolcall_dispatch_entry(_pending({}))

    assert "preview" not in entry["resume_metadata"]


def test_a_value_yielding_call_keeps_its_own_metadata_untouched(monkeypatch) -> None:
    """An ask_user park is not an approval gate and has no card allowlist: its dispatch entry is the tool's own metadata."""
    monkeypatch.setattr("primer.worker.yield_resume_registry.has_resume_hook", lambda name: name == "ask_user")
    entry = _Executor([])._toolcall_dispatch_entry(_pending({"prompt": "which one?"}, tool_name="ask_user"))

    assert entry["kind"] == "ask_user" and entry["resume_metadata"] == {"prompt": "which one?"}


def test_an_agent_nodes_yield_already_copies_its_whole_metadata_stamp_included() -> None:
    agent = _PendingAgentYield(
        node_id="n", tool_call_id="tc-2", event_key="tool_approval:s:tc-2", tool_name="_approval",
        resume_metadata={"policy_id": "p-2", "preview": STAMP, "original_call": {"id": "tc-2", "name": "x", "arguments": {}}},
        llm_messages=[], iteration=0,
    )

    yld = _Executor([], [agent])._build_pending_park_yield()

    assert yld.yielded.resume_metadata["preview"] == STAMP
