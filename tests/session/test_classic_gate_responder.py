"""Authenticated classic gate decisions join their wake's single row CAS."""

from datetime import UTC, datetime, timedelta

import pytest

from primer.model.external_tool import ExternalToolCall, ExternalToolResultIn
from primer.model.principal import PrincipalRef
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import with_wake_gate
from primer.session.external_tools import apply_tool_results
from primer.session.responder_attribution import classic_gate_responder_decision
from primer.session.yields import durably_wake_session
from tests.conftest import _FakeStorageProvider

NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
ACCEPTED = datetime(2026, 1, 2, 3, 5, tzinfo=UTC)
CREATOR = PrincipalRef(
    type="user", id="u1", display="one", role="user", source="local",
    user_id="u1",
)
OTHER = PrincipalRef(
    type="user", id="u2", display="two", role="user", source="local",
    user_id="u2",
)


def _park(*, tool: str = "ask_user", gate_id: str = "1" * 32) -> WorkspaceSession:
    key = f"{'tool_approval' if tool == '_approval' else 'ask_user'}:s:call_0"
    return WorkspaceSession(
        id="s",
        workspace_id="w",
        binding=AgentSessionBinding(agent_id="a"),
        status=SessionStatus.RUNNING,
        created_at=NOW,
        parked_status="parked",
        parked_at=NOW,
        parked_until=NOW + timedelta(hours=1),
        parked_event_key=key,
        parked_state={
            "tool_call_id": "call_0",
            "yielded": {
                "tool_name": tool,
                "event_key": key,
                "resume_metadata": {"gate_id": gate_id},
            },
        },
        responder_epoch="a" * 32,
        responder_seq=0,
        responder_reservation_version=1,
        responder_reservations={},
        turn_person=CREATOR,
    )


async def _store(row: WorkspaceSession):
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(row)
    return storage


def _decision(row, *, domain, payload, person=CREATOR, event_key=None, gate_id="1" * 32, accepted_at=ACCEPTED):
    return classic_gate_responder_decision(
        row,
        domain=domain,
        event_key=event_key or row.parked_event_key,
        gate_id=gate_id,
        payload=payload,
        person=person,
        accepted_at=accepted_at,
    )


class _Engine:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = 0

    async def mark_resumable(self, kind, entity_id, *, priority=50):
        self.calls += 1
        if self.fail:
            raise RuntimeError("lease unavailable")


@pytest.mark.parametrize(
    ("tool", "domain", "payload"),
    [
        ("ask_user", "ask_user", with_wake_gate({"response": "secret answer"}, "1" * 32)),
        ("_approval", "tool_approval", with_wake_gate({"decision": "approved", "reason": "secret reason"}, "1" * 32)),
        ("_approval", "tool_approval", with_wake_gate({"decision": "rejected", "reason": "secret reason"}, "1" * 32)),
    ],
    ids=["answer", "approve", "reject"],
)
async def test_authenticated_decision_is_reserved_and_projected_atomically(tool, domain, payload):
    row = _park(tool=tool)
    storage = await _store(row)
    decision = _decision(row, domain=domain, payload=payload)

    assert await durably_wake_session(
        row, event_key=row.parked_event_key, payload=payload,
        session_storage=storage, engine=None, responder_decision=decision,
    )

    after = await storage.get("s")
    reservation = after.responder_reservations[decision.source.key]
    assert after.parked_status == "resumable"
    assert (after.responder_seq, after.responder_reservation_version) == (1, 2)
    assert (after.turn_person, after.turn_person_seq) == (CREATOR, 1)
    assert after.turn_person_epoch == after.responder_epoch
    assert after.turn_person_source_key == decision.source.key
    assert reservation.accepted_at == ACCEPTED
    assert reservation.person == CREATOR
    assert "secret" not in reservation.source_identity


async def test_exact_retry_repairs_the_lease_without_bumping_or_restamping():
    row = _park()
    storage = await _store(row)
    payload = with_wake_gate({"response": "yes"}, "1" * 32)
    first = _decision(row, domain="ask_user", payload=payload)
    broken = _Engine(fail=True)

    with pytest.raises(RuntimeError, match="lease unavailable"):
        await durably_wake_session(
            row, event_key=row.parked_event_key, payload=payload,
            session_storage=storage, engine=broken, responder_decision=first,
        )

    repair = _Engine()
    retry = _decision(
        row, domain="ask_user", payload=payload,
        accepted_at=ACCEPTED + timedelta(minutes=1),
    )
    assert await durably_wake_session(
        row, event_key=row.parked_event_key, payload=payload,
        session_storage=storage, engine=repair, responder_decision=retry,
    )
    after = await storage.get("s")
    assert repair.calls == 1
    assert (after.responder_seq, after.responder_reservation_version) == (1, 2)
    assert after.responder_reservations[first.source.key].accepted_at == ACCEPTED


@pytest.mark.parametrize("conflict", ["person", "payload"])
async def test_conflicting_retry_is_refused_without_ledger_mutation(conflict):
    row = _park()
    storage = await _store(row)
    payload = with_wake_gate({"response": "first"}, "1" * 32)
    assert await durably_wake_session(
        row, event_key=row.parked_event_key, payload=payload,
        session_storage=storage, engine=None,
        responder_decision=_decision(row, domain="ask_user", payload=payload),
    )
    before = await storage.get("s")
    other_payload = (
        with_wake_gate({"response": "other"}, "1" * 32)
        if conflict == "payload" else payload
    )
    other_person = OTHER if conflict == "person" else CREATOR

    assert not await durably_wake_session(
        row, event_key=row.parked_event_key, payload=other_payload,
        session_storage=storage, engine=None,
        responder_decision=_decision(
            row, domain="ask_user", payload=other_payload, person=other_person,
        ),
    )
    after = await storage.get("s")
    assert after.responder_seq == before.responder_seq == 1
    assert after.responder_reservation_version == before.responder_reservation_version == 2
    assert after.responder_reservations == before.responder_reservations


async def test_moved_park_refuses_without_ledger_mutation():
    row = _park()
    storage = await _store(row)
    payload = with_wake_gate({"response": "old"}, "1" * 32)
    decision = _decision(row, domain="ask_user", payload=payload)
    await storage.patch_if(
        "s", {"parked_at": (NOW + timedelta(minutes=1)).isoformat()},
        where={"parked_at": [NOW.isoformat().replace("+00:00", "Z")]},
    )

    assert not await durably_wake_session(
        row, event_key=row.parked_event_key, payload=payload,
        session_storage=storage, engine=None, responder_decision=decision,
    )
    after = await storage.get("s")
    assert after.parked_status == "parked"
    assert (after.responder_seq, after.responder_reservation_version) == (0, 1)
    assert after.responder_reservations == {}


async def test_durable_generation_race_is_not_retried_as_a_gate_leaf_race():
    row = _park()
    storage = await _store(row)
    payload = with_wake_gate({"response": "old"}, "1" * 32)
    decision = _decision(row, domain="ask_user", payload=payload)
    assert await storage.patch_if(
        "s", {"last_seq": 1}, where={"last_seq": [0]},
    ) is not None

    assert not await durably_wake_session(
        row, event_key=row.parked_event_key, payload=payload,
        session_storage=storage, engine=None, responder_decision=decision,
    )
    after = await storage.get("s")
    assert after.last_seq == 1
    assert after.parked_status == "parked"
    assert (after.responder_seq, after.responder_reservation_version) == (0, 1)


async def test_nondecision_sibling_preserves_the_projected_gate_responder():
    gate_a = "1" * 32
    key_a, key_b = "ask_user:s:n1:call_0", "external_tool:s:n2:ext_0"
    entries = [
        {"node_id": "n1", "tool_call_id": "call_0", "event_key": key_a, "tool_name": "ask_user", "resume_metadata": {"gate_id": gate_a}},
        {"node_id": "n2", "tool_call_id": "ext_0", "event_key": key_b, "tool_name": "_external", "resume_metadata": {"external_call_row_id": "ext-row"}},
    ]
    row = WorkspaceSession(
        id="s", workspace_id="w", binding=GraphSessionBinding(graph_id="g"),
        status=SessionStatus.RUNNING, created_at=NOW, parked_status="parked",
        parked_at=NOW, parked_until=NOW + timedelta(hours=1),
        parked_event_key=key_a, parked_event_keys=[key_a, key_b],
        parked_state={
            "tool_call_id": "call_0",
            "yielded": {"tool_name": "_approval", "event_key": key_a, "resume_metadata": {"gate_id": gate_a}, "event_keys": [key_a, key_b]},
            "graph_checkpoint": {"pending_toolcalls": [], "pending_agent_yields": entries, "pending_dispatch": []},
        },
        responder_epoch="a" * 32, responder_seq=0,
        responder_reservation_version=1, responder_reservations={},
        turn_person=CREATOR,
    )
    provider = _FakeStorageProvider()
    storage = provider.get_storage(WorkspaceSession)
    await storage.create(row)
    calls = provider.get_storage(ExternalToolCall)
    await calls.create(ExternalToolCall(
        id="ext-row", session_id="s", tool_call_id="ext_0",
        tool_name="lookup", arguments={}, created_at=NOW,
    ))
    snapshots = [await storage.get("s"), await storage.get("s")]
    payload_a = with_wake_gate({"response": "a"}, gate_a)
    engine = _Engine()

    assert await durably_wake_session(
        snapshots[0], event_key=key_a, payload=payload_a,
        session_storage=storage, engine=engine,
        responder_decision=_decision(
            snapshots[0], domain="ask_user", event_key=key_a,
            gate_id=gate_a, payload=payload_a, person=CREATOR,
        ),
    )
    assert await apply_tool_results(
        snapshots[1],
        [ExternalToolResultIn(tool_call_id="ext_0", result={"value": "b"})],
        call_storage=calls, session_storage=storage, engine=engine,
        event_bus=None, by=OTHER,
    ) == 1

    after = await storage.get("s")
    assert engine.calls == 2
    assert after.parked_status == "resumable"
    assert (after.responder_seq, after.responder_reservation_version) == (1, 2)
    assert len(after.responder_reservations) == 1
    assert after.turn_person == CREATOR
    assert after.turn_person_seq == 1
    assert after.turn_person_source_key in after.responder_reservations
    leaves = {
        entry["event_key"]: entry["payload"]
        for entry in after.parked_state["resume_event_payloads"].values()
    }
    assert leaves[key_a] == payload_a
    assert leaves[key_b]["result"] == {"value": "b"}
    assert leaves[key_b]["is_error"] is False
    assert (await calls.get("ext-row")).status == "completed"


async def test_two_gate_leaves_from_one_snapshot_both_reserve_in_sequence():
    gate_a, gate_b = "1" * 32, "2" * 32
    key_a, key_b = "ask_user:s:n1:call_0", "ask_user:s:n2:call_0"
    entries = [
        {"node_id": "n1", "tool_call_id": "call_0", "event_key": key_a, "tool_name": "ask_user", "resume_metadata": {"gate_id": gate_a}},
        {"node_id": "n2", "tool_call_id": "call_0", "event_key": key_b, "tool_name": "ask_user", "resume_metadata": {"gate_id": gate_b}},
    ]
    row = WorkspaceSession(
        id="s", workspace_id="w", binding=GraphSessionBinding(graph_id="g"),
        status=SessionStatus.RUNNING, created_at=NOW, parked_status="parked",
        parked_at=NOW, parked_until=NOW + timedelta(hours=1),
        parked_event_key=key_a, parked_event_keys=[key_a, key_b],
        parked_state={
            "tool_call_id": "call_0",
            "yielded": {"tool_name": "_approval", "event_key": key_a, "resume_metadata": {"gate_id": gate_a}, "event_keys": [key_a, key_b]},
            "graph_checkpoint": {"pending_toolcalls": [], "pending_agent_yields": entries, "pending_dispatch": []},
        },
        responder_epoch="a" * 32, responder_seq=0,
        responder_reservation_version=1, responder_reservations={},
        turn_person=CREATOR,
    )
    storage = await _store(row)
    snapshots = [await storage.get("s"), await storage.get("s")]
    payload_a = with_wake_gate({"response": "a"}, gate_a)
    payload_b = with_wake_gate({"response": "b"}, gate_b)

    assert await durably_wake_session(
        snapshots[0], event_key=key_a, payload=payload_a,
        session_storage=storage, engine=None,
        responder_decision=_decision(
            snapshots[0], domain="ask_user", event_key=key_a,
            gate_id=gate_a, payload=payload_a, person=CREATOR,
        ),
    )
    assert await durably_wake_session(
        snapshots[1], event_key=key_b, payload=payload_b,
        session_storage=storage, engine=None,
        responder_decision=_decision(
            snapshots[1], domain="ask_user", event_key=key_b,
            gate_id=gate_b, payload=payload_b, person=OTHER,
        ),
    )

    after = await storage.get("s")
    assert (after.responder_seq, after.responder_reservation_version) == (2, 3)
    assert len(after.responder_reservations) == 2
    assert after.turn_person == OTHER
    assert sorted(
        entry["payload"]["response"]
        for entry in after.parked_state["resume_event_payloads"].values()
    ) == ["a", "b"]


@pytest.mark.parametrize(
    ("by", "expected_person"),
    [(None, CREATOR), (OTHER, OTHER)],
    ids=["machine", "legacy-channel"],
)
async def test_ineligible_wake_does_not_create_a_reservation(by, expected_person):
    row = _park()
    storage = await _store(row)
    assert await durably_wake_session(
        row, event_key=row.parked_event_key, payload={"__yield_timeout__": True},
        session_storage=storage, engine=None, by=by,
    )
    after = await storage.get("s")
    assert (after.responder_seq, after.responder_reservation_version) == (0, 1)
    assert after.responder_reservations == {}
    assert after.turn_person == expected_person
