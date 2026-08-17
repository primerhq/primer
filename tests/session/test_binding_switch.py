"""Queued binding switch, applied at the drain checkpoint (S1 P3 T17).

Spec section 3 and 6. A switch requested mid-turn cannot take effect
immediately: the running turn owns its binding. It is queued on the row
and applied at the next checkpoint, BEFORE any queued steer drains, so
a follow-up that was waiting behind the turn runs under the incoming
binding rather than the outgoing one.
"""

import json
from datetime import UTC, datetime

from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.binding_switch import (
    agent_marker_payload,
    apply_binding_switch,
    build_switched_binding,
)


def _row(**kw):
    base = {
        "id": "s", "workspace_id": "w",
        "binding": AgentSessionBinding(agent_id="agent-a"),
        "status": SessionStatus.WAITING,
        "created_at": datetime.now(UTC),
        "last_seq": 6,
    }
    base.update(kw)
    return WorkspaceSession(**base)


class _Sessions:
    def __init__(self, row):
        self.row = row

    async def get(self, _sid):
        return self.row

    async def update(self, row):
        self.row = row
        return row


class _IO:
    def __init__(self):
        self.lines: list[bytes] = []

    async def append_message_line(self, session_id, line):
        self.lines.append(line)


async def _no_snapshot(_binding):
    return None


class TestModelField:
    def test_defaults_none_and_round_trips(self):
        assert _row().pending_binding_switch is None
        row = _row(pending_binding_switch={
            "kind": "agent", "agent_id": "agent-b", "profile_id": None,
            "actor": "user",
        })
        again = WorkspaceSession.model_validate(row.model_dump(mode="json"))
        assert again.pending_binding_switch["agent_id"] == "agent-b"


class TestBuildSwitchedBinding:
    def test_agent_to_graph(self):
        binding = build_switched_binding(
            _row(), {"kind": "graph", "graph_id": "g-1", "profile_id": "p-9"},
        )
        assert isinstance(binding, GraphSessionBinding)
        assert (binding.graph_id, binding.profile_id) == ("g-1", "p-9")

    def test_profile_only_change_keeps_the_agent(self):
        binding = build_switched_binding(
            _row(), {"kind": "agent", "agent_id": "agent-a",
                     "profile_id": "p-2"},
        )
        assert isinstance(binding, AgentSessionBinding)
        assert (binding.agent_id, binding.profile_id) == ("agent-a", "p-2")

    def test_graph_to_agent(self):
        row = _row(binding=GraphSessionBinding(graph_id="g-0"))
        binding = build_switched_binding(
            row, {"kind": "agent", "agent_id": "agent-z"},
        )
        assert isinstance(binding, AgentSessionBinding)
        assert binding.agent_id == "agent-z"


class TestMarkerPayload:
    def test_carries_the_epoch_and_both_sides(self):
        """Amendment m6: the record and its tap event must be
        informationally identical, so the epoch rides the payload."""
        payload = agent_marker_payload(
            from_binding={"kind": "agent", "agent_id": "agent-a"},
            to_binding={"kind": "agent", "agent_id": "agent-b"},
            actor="user",
            binding_epoch=1,
        )
        assert payload["binding_epoch"] == 1
        assert payload["from_binding"]["agent_id"] == "agent-a"
        assert payload["to_binding"]["agent_id"] == "agent-b"
        assert payload["actor"] == "user"


class TestApply:
    async def test_bumps_epoch_writes_marker_and_clears_the_queue(self):
        row = _row(
            binding_epoch=3,
            pending_binding_switch={
                "kind": "agent", "agent_id": "agent-b", "actor": "agent",
            },
        )
        sessions, io = _Sessions(row), _IO()
        updated = await apply_binding_switch(
            sessions=sessions, workspace_io=io, row=row,
            request=row.pending_binding_switch, actor="agent",
            resolve_snapshot=_no_snapshot,
        )
        assert updated.binding.agent_id == "agent-b"
        assert updated.binding_epoch == 4
        assert updated.pending_binding_switch is None
        assert updated.last_seq == 7
        # The marker is a closed structural record, so the cursor may
        # pass it: leaving it behind would hand route_steer a record it
        # cannot classify.
        assert updated.next_unprocessed_seq == 8

        marker = json.loads(io.lines[0].decode())
        assert marker["kind"] == "agent_marker"
        assert marker["seq"] == 7
        assert marker["payload"]["binding_epoch"] == 4

    async def test_noop_without_a_request(self):
        row = _row()
        sessions, io = _Sessions(row), _IO()
        updated = await apply_binding_switch(
            sessions=sessions, workspace_io=io, row=row, request=None,
            actor="user", resolve_snapshot=_no_snapshot,
        )
        assert updated.binding_epoch == 0
        assert io.lines == []

    async def test_resnapshots_the_incoming_target(self):
        """The session must run the CURRENT definition of what it just
        switched to, not a stale snapshot of what it left."""
        captured = {}

        async def _resolve(binding):
            # The resolver is handed the INCOMING binding, which is what
            # makes the snapshot a snapshot of the new target.
            captured["kind"] = getattr(binding, "kind", None)
            captured["id"] = getattr(binding, "agent_id", None)
            return None

        row = _row(pending_binding_switch={
            "kind": "agent", "agent_id": "agent-b", "actor": "user",
        })
        updated = await apply_binding_switch(
            sessions=_Sessions(row), workspace_io=_IO(), row=row,
            request=row.pending_binding_switch, actor="user",
            resolve_snapshot=_resolve,
        )
        assert captured == {"kind": "agent", "id": "agent-b"}
        # A missing target degrades to a snapshot-less binding the
        # executor builder resolves live, rather than failing the switch.
        assert updated.binding.agent_snapshot is None
        assert updated.binding.agent_id == "agent-b"


def test_switch_is_applied_before_the_queue_drains():
    """Ordering guard for the checkpoint.

    Realizing a queued steer first would run the user's follow-up under
    the OUTGOING binding, which is what next-turn switch semantics
    forbid. Pinned structurally so a later edit cannot quietly reorder
    the two calls.
    """
    import inspect

    from primer.session.dispatch import run_one_session_turn

    body = inspect.getsource(run_one_session_turn)
    calls = [
        line.strip()
        for line in body.splitlines()
        if "_at_checkpoint(deps, session)" in line
    ]
    switch = "await _apply_pending_switch_at_checkpoint(deps, session)"
    realize = "await _realize_pending_at_checkpoint(deps, session)"
    assert calls.count(switch) == 3
    assert calls.count(realize) == 3
    # They must alternate, switch first, at all three terminal exits.
    assert calls == [switch, realize] * 3, calls
