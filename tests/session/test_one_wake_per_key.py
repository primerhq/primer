"""The durable flip lands at most one wake per key of a park (board ticket 01a12606).

A single-event park already took one: the flip advances it from ``parked`` only. A multi-event park (a graph superstep, ``parked_event_keys`` set) is
also admitted from ``resumable``, so a reply to ANOTHER of its gates accumulates, and that admitted a second wake on the SAME key too: it rewrote the
key's leaf, so a second decision on one gate replaced the first one the worker would run (and the audit kept the first). The flip now refuses a wake whose
key already holds a leaf, as a condition the backend evaluates in the same statement as the write (``patch_if``), so two wakes that read the same row
cannot both land. A redelivery of the wake that DID land (the same payload: the bus listener and the event log's replay deliver every wake again, and a
client retries) is answered as landed and re-arms the lease, as a landed flip does.

Real SQLite storage: a row read there is a copy, so two wakes can hold the same snapshot.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from primer.int.claim import ClaimKind
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.model.yield_ import with_wake_gate
from primer.session.yields import durably_mark_session_resumable, flip_sessions_parked_on
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker.yield_runtime import make_cancelled_payload
from tests.session.test_machine_wake_fences import _graph_park, _parked

G1 = "1" * 32
N1 = {"node_id": "n1", "tool_call_id": "call_1", "event_key": "tool_approval:gs:n1:call_1", "tool_name": "_approval", "resume_metadata": {"gate_id": G1}}
N2 = {"node_id": "n2", "tool_call_id": "call_2", "event_key": "ask_user:gs:n2:call_2", "tool_name": "ask_user", "resume_metadata": {"prompt": "?"}}
APPROVED = with_wake_gate({"decision": "approved", "reason": None, "decided_by": "alice"}, G1)
REJECTED = with_wake_gate({"decision": "rejected", "reason": "no", "decided_by": "alice"}, G1)


class _Engine:
    """Records the leases the flip arms."""

    def __init__(self) -> None:
        self.armed: list[tuple[ClaimKind, str]] = []

    async def mark_resumable(self, kind: ClaimKind, entity_id: str, *, priority: int = 50) -> None:
        self.armed.append((kind, entity_id))


@pytest_asyncio.fixture
async def sessions(tmp_path) -> AsyncIterator:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "one-wake.sqlite")))
    await provider.initialize()
    try:
        yield provider.get_storage(WorkspaceSession)
    finally:
        await provider.aclose()


def _leaves(row: WorkspaceSession) -> dict:
    return {entry["event_key"]: entry["payload"] for entry in ((row.parked_state or {}).get("resume_event_payloads") or {}).values()}


async def _flip(sessions, row: WorkspaceSession, key: str, payload: dict, engine: _Engine | None = None) -> bool:
    return await durably_mark_session_resumable(row, event_key=key, payload=payload, session_storage=sessions, engine=engine)


@pytest.mark.asyncio
async def test_a_second_decision_on_one_gate_of_a_graph_park_is_refused_and_the_first_stands(sessions) -> None:
    """Two decisions on gate n1 that read the same parked row (the window before the first lands), then one more from a fresh read: only the
    first lands. A reply to the OTHER gate, n2, is still admitted from ``resumable`` and accumulates."""
    await sessions.create(_graph_park("gs", parked_at=datetime.now(UTC) - timedelta(minutes=1), agent_yields=(N1, N2)))
    first_read, second_read = await sessions.get("gs"), await sessions.get("gs")

    landed = [
        await _flip(sessions, first_read, N1["event_key"], APPROVED),
        await _flip(sessions, second_read, N1["event_key"], REJECTED),
        await _flip(sessions, await sessions.get("gs"), N1["event_key"], REJECTED),
    ]
    after_n1 = await sessions.get("gs")
    other = await _flip(sessions, after_n1, N2["event_key"], {"response": "blue"})
    after = await sessions.get("gs")

    assert {
        "landed": landed, "singular": (after_n1.parked_state["resume_event_key"], after_n1.parked_state["resume_event_payload"]),
        "other gate": other, "leaves": _leaves(after), "parked_status": after.parked_status,
    } == {
        "landed": [True, False, False], "singular": (N1["event_key"], APPROVED),
        "other gate": True, "leaves": {N1["event_key"]: APPROVED, N2["event_key"]: {"response": "blue"}}, "parked_status": "resumable",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("multi", [True, False], ids=["graph-park", "single-park"])
async def test_a_redelivery_of_the_wake_that_landed_is_answered_as_landed_and_re_arms_the_lease(sessions, multi: bool) -> None:
    """The bus listener and the event log's replay deliver every wake the route already flipped, and a client retries a decision whose answer it lost
    (or whose first attempt half-applied: the row stamped, the lease lost). The same payload again is the wake that landed, not a second one."""
    key = "tool_approval:s:call_0"
    await sessions.create(_parked(key=key, multi=multi))
    engine = _Engine()
    stale = await sessions.get("s")
    assert await _flip(sessions, stale, key, APPROVED, engine) is True
    stamped = await sessions.get("s")

    again = [await _flip(sessions, stale, key, APPROVED, engine), await _flip(sessions, await sessions.get("s"), key, APPROVED, engine)]

    after = await sessions.get("s")
    assert (again, engine.armed, after.parked_state) == ([True, True], [(ClaimKind.SESSION, "s")] * 3, stamped.parked_state)


@pytest.mark.asyncio
@pytest.mark.parametrize("multi", [True, False], ids=["graph-park", "single-park"])
async def test_a_different_decision_after_the_first_is_refused_and_arms_nothing(sessions, multi: bool) -> None:
    key = "tool_approval:s:call_0"
    await sessions.create(_parked(key=key, multi=multi))
    engine = _Engine()
    assert await _flip(sessions, await sessions.get("s"), key, APPROVED, engine) is True
    stamped = await sessions.get("s")

    refused = await _flip(sessions, await sessions.get("s"), key, REJECTED, engine)

    assert (refused, engine.armed, (await sessions.get("s")).parked_state) == (False, [(ClaimKind.SESSION, "s")], stamped.parked_state)


@pytest.mark.asyncio
async def test_a_redelivery_after_the_park_moved_on_or_ended_is_refused_and_arms_nothing(sessions) -> None:
    """The same payload is a redelivery only while the row still holds it on the park it was read from: once the session re-parked (a new
    ``parked_at``, a fresh state) or ended, it is refused like any wake for an earlier park, and no lease is armed."""
    key = "tool_approval:s:call_0"
    await sessions.create(_parked(key=key, multi=True))
    engine = _Engine()
    old = await sessions.get("s")
    assert await _flip(sessions, old, key, APPROVED, engine) is True
    stamped = await sessions.get("s")
    await sessions.update(stamped.model_copy(update={"parked_status": "parked", "parked_at": datetime.now(UTC), "parked_state": _parked(key=key).parked_state}))
    reparked = await sessions.get("s")

    after_repark = await _flip(sessions, old, key, APPROVED, engine)
    new_park_untouched = (await sessions.get("s")).parked_state == reparked.parked_state
    await sessions.update(stamped.model_copy(update={"status": SessionStatus.ENDED}))
    after_end = await _flip(sessions, stamped, key, APPROVED, engine)

    assert {"after the re-park": after_repark, "the new park untouched": new_park_untouched, "after the end": after_end, "armed": engine.armed} == {
        "after the re-park": False, "the new park untouched": True, "after the end": False, "armed": [(ClaimKind.SESSION, "s")],
    }


@pytest.mark.asyncio
async def test_a_bus_delivered_cancel_does_not_replace_the_decision_the_route_stamped_on_a_graph_gate(sessions) -> None:
    """The cancel route, a channel reply and the timeout sweeper publish onto the bus and leave the flip to the listener: on a graph park the cancel
    of a gate already approved (a cancel of an approval is classified as a rejection) used to replace the approval."""
    await sessions.create(_graph_park("gs", parked_at=datetime.now(UTC) - timedelta(minutes=1), agent_yields=(N1, N2)))
    assert await _flip(sessions, await sessions.get("gs"), N1["event_key"], APPROVED) is True

    flipped = await flip_sessions_parked_on(
        N1["event_key"], with_wake_gate(make_cancelled_payload(reason="skip"), G1), session_storage=sessions, engine=None,
    )

    assert (flipped, _leaves(await sessions.get("gs"))) == (0, {N1["event_key"]: APPROVED})
