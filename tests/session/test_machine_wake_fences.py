"""The machine-wake fences judge the pending entry's identity, and the flip is atomic (ticket 01a1223f, from the #702 review: N4, N6, N9).

* N4: the park stamp fences a SINGLE park; a GRAPH park re-parks with a fresh ``parked_at`` whenever a sibling is resolved, so it is exempt, and a redelivered
  trigger fire or external result had no protection there. Both producers know the identity of the pending entry they answer (``resume_metadata.subscription_id``
  for a trigger park, ``external_call_row_id`` for an external park), so the wake names it (``__yield_entry__``) and the flip compares it with the entry that waits on
  the event key, as ``gate_id`` does for a human gate. It judges a single park and a graph park alike.
* N6: the ``wait_for_event`` sink checked the park by key only. A subscription whose park timed out still waits for its event, and delivered into a LATER evwait park
  under the same tool_call_id that another subscription created. The sink names its subscription and delivers only into the park that subscription created.
* N9: every wake fence judged the row ``find()`` returned, and the flip then wrote the WHOLE document guarded on ``status`` only, so a wake that read the old park and
  wrote after a re-park landed and overwrote the new park (and any field another writer changed in the gap). The flip is one ``patch_if`` of the two fields it owns,
  guarded on the park it read (``parked_at``), its ``parked_status`` and a status that is not ENDED.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pydantic_core import to_jsonable_python

import primer.observability.metrics as metrics
from primer.model.external_tool import ExternalToolCall
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import AgentSessionBinding, GraphSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import WAKE_ENTRY_KEY, with_wake_entry
from primer.session.yields import durably_mark_session_resumable, flip_sessions_parked_on
from primer.storage import raw_generation
from primer.storage.sqlite import SqliteStorageProvider
from tests.conftest import _FakeStorageProvider
from tests.session.test_machine_wake_replay_round2 import _row


@pytest.fixture(autouse=True)
def _fresh_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


def _refused() -> float:
    return metrics.session_wake_stale_refused_total._value.get()


async def _flip(row: WorkspaceSession, key: str, payload: dict) -> tuple[int, WorkspaceSession]:
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(row)
    flipped = await flip_sessions_parked_on(key, payload, session_storage=storage, engine=None)
    return flipped, await storage.get(row.id)


# ---- N4: a wake names the entry it answers ----------------------------------------------------------------------------------------------------

TRIGGER_KEY = "trigger:s:T"
EXTERNAL_KEY = "external_tool:s:call_0"


@pytest.mark.asyncio
@pytest.mark.parametrize("graph", [False, True], ids=["single", "graph"])
async def test_a_trigger_fire_for_another_subscription_does_not_wake_the_pending_one(graph) -> None:
    row = _row("s", TRIGGER_KEY, until=timedelta(hours=1), tool="subscribe_to_trigger", meta={"subscription_id": "sb-NEW", "trigger_id": "T"}, graph=graph)

    flipped, after = await _flip(row, TRIGGER_KEY, with_wake_entry({"ok": True, "payload": {"for": "OLD"}}, "sb-OLD"))

    assert (flipped, after.parked_status) == (0, "parked")
    assert "resume_event_payload" not in (after.parked_state or {})
    assert _refused() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("graph", [False, True], ids=["single", "graph"])
async def test_a_trigger_fire_for_the_pending_subscription_wakes_it(graph) -> None:
    row = _row("s", TRIGGER_KEY, until=timedelta(hours=1), tool="subscribe_to_trigger", meta={"subscription_id": "sb-NEW", "trigger_id": "T"}, graph=graph)

    flipped, after = await _flip(row, TRIGGER_KEY, with_wake_entry({"ok": True}, "sb-NEW"))

    assert (flipped, after.parked_status) == (1, "resumable")
    assert _refused() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("graph", [False, True], ids=["single", "graph"])
async def test_an_external_result_for_another_call_row_does_not_complete_the_pending_call(graph) -> None:
    row = _row("s", EXTERNAL_KEY, until=timedelta(hours=1), tool="_external", meta={"external_call_row_id": "etool-NEW"}, graph=graph)

    flipped, after = await _flip(row, EXTERNAL_KEY, with_wake_entry({"result": "stale", "is_error": False}, "etool-OLD"))

    assert (flipped, after.parked_status) == (0, "parked")
    assert _refused() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("graph", [False, True], ids=["single", "graph"])
async def test_an_external_result_for_the_pending_call_row_completes_it(graph) -> None:
    row = _row("s", EXTERNAL_KEY, until=timedelta(hours=1), tool="_external", meta={"external_call_row_id": "etool-NEW"}, graph=graph)

    flipped, after = await _flip(row, EXTERNAL_KEY, with_wake_entry({"result": "ok", "is_error": False}, "etool-NEW"))

    assert (flipped, after.parked_status) == (1, "resumable")


@pytest.mark.asyncio
async def test_a_wake_that_names_no_entry_is_judged_as_before() -> None:
    """A producer from before this release (an older build in a rolling deploy) stamps nothing."""
    row = _row("s", TRIGGER_KEY, until=timedelta(hours=1), tool="subscribe_to_trigger", meta={"subscription_id": "sb-NEW"}, graph=True)

    flipped, _after = await _flip(row, TRIGGER_KEY, {"ok": True})

    assert flipped == 1


@pytest.mark.asyncio
async def test_a_pending_entry_with_no_identity_is_judged_by_the_key_alone() -> None:
    """A park written before the entry carried an id: the wake names one, there is nothing to compare it with."""
    row = _row("s", TRIGGER_KEY, until=timedelta(hours=1), tool="subscribe_to_trigger", meta={}, graph=True)

    flipped, _after = await _flip(row, TRIGGER_KEY, with_wake_entry({"ok": True}, "sb-ANY"))

    assert flipped == 1


@pytest.mark.asyncio
async def test_a_key_two_sibling_entries_share_admits_a_wake_that_names_either_and_refuses_one_that_names_neither() -> None:
    """One graph session, two nodes subscribed to one trigger: the key is shared (it carries the session), the subscription ids are not. The flip admits a
    wake that names either sibling, so it does not tell them apart: the resume then takes every entry on the fired key (ticket 01a122cc-e699)."""
    key = TRIGGER_KEY
    entries = [
        {"node_id": "n1", "tool_call_id": "call_0", "event_key": key, "resume_metadata": {"subscription_id": "sb-1"}},
        {"node_id": "n2", "tool_call_id": "call_1", "event_key": key, "resume_metadata": {"subscription_id": "sb-2"}},
    ]

    def row() -> WorkspaceSession:
        r = _row("s", key, until=timedelta(hours=1), tool="subscribe_to_trigger", meta={"subscription_id": "sb-1"}, graph=True)
        state = dict(r.parked_state)
        state["graph_checkpoint"] = {"pending_toolcalls": [], "pending_agent_yields": entries, "pending_dispatch": []}
        return r.model_copy(update={"parked_state": state})

    assert (await _flip(row(), key, with_wake_entry({"ok": True}, "sb-2")))[0] == 1
    assert (await _flip(row(), key, with_wake_entry({"ok": True}, "sb-3")))[0] == 0


@pytest.mark.asyncio
async def test_the_refusal_says_the_entry_is_not_the_pending_one(caplog) -> None:
    row = _row("s", TRIGGER_KEY, until=timedelta(hours=1), tool="subscribe_to_trigger", meta={"subscription_id": "sb-NEW"})
    with caplog.at_level(logging.WARNING):
        await _flip(row, TRIGGER_KEY, with_wake_entry({"ok": True}, "sb-OLD"))

    [message] = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert "another entry" in message and "re-parked" in message


# ---- N9: the flip is one guarded patch of the fields it owns ----------------------------------------------------------------------------------


def _parked(sid: str = "s", *, key: str = "ask_user:s:call_0", parked_at: datetime | None = None, multi: bool = False) -> WorkspaceSession:
    row = _row(sid, key, until=timedelta(hours=1), tool="ask_user", parked_at=parked_at or datetime.now(UTC) - timedelta(minutes=3))
    return row.model_copy(update={"parked_event_keys": [key]} if multi else {})


@pytest.mark.asyncio
async def test_a_wake_that_read_the_old_park_does_not_overwrite_the_new_one() -> None:
    """The wake read the row (park 1); in the gap the session resumed and PARKED AGAIN under the same key (park 2); the write must not land on park 2."""
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    old = _parked(parked_at=datetime.now(UTC) - timedelta(minutes=3))
    await storage.create(old)
    snapshot = await storage.get("s")
    new = snapshot.model_copy(update={
        "parked_at": datetime.now(UTC), "parked_state": {**snapshot.parked_state, "yielded": {**snapshot.parked_state["yielded"], "resume_metadata": {"gate": "NEW"}}},
    })
    await storage.update(new)

    landed = await durably_mark_session_resumable(snapshot, event_key="ask_user:s:call_0", payload={"response": "for the old park"}, session_storage=storage, engine=None)

    after = await storage.get("s")
    assert landed is False
    assert after.parked_status == "parked" and after.parked_at == new.parked_at
    assert after.parked_state == new.parked_state, "the new park is untouched: no resume payload, no old yielded"


@pytest.mark.asyncio
async def test_a_field_another_writer_changed_in_the_gap_survives_the_flip() -> None:
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(_parked())
    snapshot = await storage.get("s")
    await storage.patch_if("s", {"last_seq": 41, "cancel_requested": True}, where={"parked_status": ["parked"]})

    landed = await durably_mark_session_resumable(snapshot, event_key="ask_user:s:call_0", payload={"response": "x"}, session_storage=storage, engine=None)

    after = await storage.get("s")
    assert landed is True and after.parked_status == "resumable"
    assert after.last_seq == 41 and after.cancel_requested is True, "the flip wrote its two fields, not the whole document from a stale snapshot"
    assert after.parked_state["resume_event_payload"] == {"response": "x"} and after.parked_state["resume_event_key"] == "ask_user:s:call_0"


@pytest.mark.asyncio
async def test_a_row_that_ended_in_the_gap_is_not_flipped() -> None:
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(_parked())
    snapshot = await storage.get("s")
    await storage.patch_if("s", {"status": SessionStatus.ENDED.value}, where={"parked_status": ["parked"]})

    landed = await durably_mark_session_resumable(snapshot, event_key="ask_user:s:call_0", payload={"response": "x"}, session_storage=storage, engine=None)

    after = await storage.get("s")
    assert landed is False and after.parked_status == "parked" and after.status == SessionStatus.ENDED


@pytest.mark.asyncio
async def test_a_row_another_wake_already_flipped_is_not_flipped_twice() -> None:
    """A single-event park advances from ``parked`` only: the second of two racing wakes is refused (the first one's payload stands)."""
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(_parked())
    first = await storage.get("s")
    second = await storage.get("s")

    assert await durably_mark_session_resumable(first, event_key="ask_user:s:call_0", payload={"response": "first"}, session_storage=storage, engine=None) is True
    assert await durably_mark_session_resumable(second, event_key="ask_user:s:call_0", payload={"response": "second"}, session_storage=storage, engine=None) is False

    assert (await storage.get("s")).parked_state["resume_event_payload"] == {"response": "first"}


@pytest.mark.asyncio
async def test_a_multi_event_park_still_accepts_a_second_wake_after_the_first_flipped_it() -> None:
    """A multi-event park advances from ``parked`` OR ``resumable`` (a second concurrent reply accumulates): the guard allows both."""
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(_parked(multi=True))
    first = await storage.get("s")
    second = await storage.get("s")

    assert await durably_mark_session_resumable(first, event_key="ask_user:s:call_0", payload={"response": "a"}, session_storage=storage, engine=None) is True
    assert await durably_mark_session_resumable(second, event_key="ask_user:s:call_0", payload={"response": "b"}, session_storage=storage, engine=None) is True

    assert (await storage.get("s")).parked_status == "resumable"


@pytest.mark.asyncio
async def test_a_flip_on_the_current_snapshot_lands_and_writes_a_non_finite_number_as_null() -> None:
    """The control, and the contract the whole-document write kept: the stored payload is the model's canonical dump (``NaN`` becomes ``null``)."""
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(_parked())
    snapshot = await storage.get("s")

    landed = await durably_mark_session_resumable(
        snapshot, event_key="ask_user:s:call_0", payload={"response": float("nan"), "n": 1}, session_storage=storage, engine=None,
    )

    assert landed is True
    assert (await storage.get("s")).parked_state["resume_event_payload"] == {"response": None, "n": 1}


@pytest.mark.asyncio
async def test_a_missing_row_still_raises_not_found() -> None:
    from primer.model.except_ import NotFoundError

    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    ghost = _parked("ghost")

    with pytest.raises(NotFoundError):
        await durably_mark_session_resumable(ghost, event_key="ask_user:s:call_0", payload={}, session_storage=storage, engine=None)


# ---- #707 review, round 2 ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_entry_fence_counts_only_the_entries_that_wait_on_the_fired_key() -> None:
    """N5 (M3). The entry on the fired key carries no id (a park from before); a sibling on ANOTHER key carries one. The wake is judged by the fired key's
    entries alone, so it lands: counting the sibling would refuse it."""
    key, other = "external_tool:s:n1:call_0", "trigger:s:T"
    entries = [
        {"node_id": "n1", "tool_call_id": "call_0", "event_key": key, "tool_name": "_external", "resume_metadata": {}},
        {"node_id": "n2", "tool_call_id": "call_1", "event_key": other, "tool_name": "subscribe_to_trigger", "resume_metadata": {"subscription_id": "sb-2"}},
    ]
    row = _row("s", key, until=timedelta(hours=1), tool="_external", graph=True)
    row = row.model_copy(update={"parked_state": {**row.parked_state, "graph_checkpoint": {
        "pending_toolcalls": [], "pending_agent_yields": entries, "pending_dispatch": [],
    }}})

    flipped, after = await _flip(row, key, with_wake_entry({"result": "ok", "is_error": False}, "etool-1"))

    assert (flipped, after.parked_status) == (1, "resumable")
    assert _refused() == 0


@pytest_asyncio.fixture
async def sp(tmp_path):
    """A REAL SQLite storage: a row read there is a copy, so a write from a stale snapshot can happen (the in-memory fake hands back the stored object)."""
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "fences.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


class _HoldBus:
    """Records every publish and delivers none: the test hands a copy to the flip itself, as a bus that redelivers it late would."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict | None = None) -> None:
        self.published.append((key, dict(payload or {})))


def _graph_park(sid: str, *, parked_at: datetime, toolcalls: tuple[dict, ...] = (), agent_yields: tuple[dict, ...] = ()) -> WorkspaceSession:
    """A graph park shaped like the park writer's: the top-level ``yielded`` is the ``_approval`` projection of the primary (a ToolCall node's entry
    first, whose metadata the projection rebuilds from ``original_call`` only), and every entry's key is in ``parked_event_keys``."""
    keys = [t["parked_event_key"] for t in toolcalls] + [e["event_key"] for e in agent_yields]
    if toolcalls:
        first = toolcalls[0]
        primary_key, primary_tcid = first["parked_event_key"], first["tool_call_id"]
        meta = {"original_call": {"id": primary_tcid, "name": first["tool_name"], "arguments": {}}}
    else:
        first = agent_yields[0]
        primary_key, primary_tcid, meta = first["event_key"], first["tool_call_id"], dict(first["resume_metadata"])
    state = {
        "schema_version": 1, "tool_call_id": primary_tcid,
        "yielded": {"tool_name": "_approval", "event_key": primary_key, "resume_metadata": meta, "event_keys": keys},
        "llm_messages": [], "turn_no": 0, "started_at": parked_at.isoformat(), "resume_event_payload": None,
        "graph_checkpoint": {"pending_toolcalls": list(toolcalls), "pending_agent_yields": list(agent_yields), "pending_dispatch": []},
    }
    return WorkspaceSession(
        id=sid, workspace_id="ws", binding=GraphSessionBinding(graph_id="g"), status=SessionStatus.RUNNING, created_at=parked_at, parked_status="parked",
        parked_at=parked_at, parked_until=parked_at + timedelta(hours=1), parked_event_key=primary_key, parked_event_keys=keys, parked_state=state,
    )


_ASK_N2 = {"node_id": "n2", "tool_call_id": "call_1", "event_key": "ask_user:gs:n2:call_1", "tool_name": "ask_user", "resume_metadata": {"prompt": "?"}}


@pytest.mark.asyncio
async def test_the_cancel_of_a_tool_call_nodes_trigger_wait_names_the_subscription_of_the_entry_it_resolved(sp) -> None:
    """N1 (probe A4). A graph park whose primary is a ToolCall node's ``subscribe_to_trigger``: the top-level projection carries ``original_call`` only, so
    the cancel names the subscription of the pending entry it resolved by tool_call_id. The node then subscribes again under the same key (a trigger key
    carries no tool_call_id) with a new subscription and a fresh ``parked_at``; the cancel redelivered after that must not end the new wait."""
    from primer.api.routers.yields import CancelYieldedToolBody, post_cancel_yielded_tool

    sessions = sp.get_storage(WorkspaceSession)
    key = "trigger:gs:T"
    wait = {"node_id": "n1", "tool_call_id": "uuid-1", "parked_event_key": key, "arguments": {}, "tool_name": "subscribe_to_trigger",
            "resume_metadata": {"subscription_id": "sb-1", "trigger_id": "T"}}
    await sessions.create(_graph_park("gs", parked_at=datetime.now(UTC) - timedelta(minutes=5), toolcalls=(wait,), agent_yields=(_ASK_N2,)))
    bus = _HoldBus()

    await post_cancel_yielded_tool(
        session_id="gs", tool_call_id="uuid-1", body=CancelYieldedToolBody(reason="skip"), session_storage=sessions, event_bus=bus,
        call_storage=sp.get_storage(ExternalToolCall), user=None,
    )

    [(published_key, copy)] = bus.published
    assert published_key == key
    assert copy.get(WAKE_ENTRY_KEY) == "sb-1", "the cancel names the subscription of the entry it cancels"
    again = {**wait, "tool_call_id": "uuid-2", "resume_metadata": {"subscription_id": "sb-2", "trigger_id": "T"}}
    await sessions.update(_graph_park("gs", parked_at=datetime.now(UTC), toolcalls=(again,), agent_yields=(_ASK_N2,)))

    flipped = await flip_sessions_parked_on(published_key, copy, session_storage=sessions, engine=None)

    assert flipped == 0 and (await sessions.get("gs")).parked_status == "parked", "the redelivered cancel ended the node's NEW wait"
    assert _refused() == 1


def _ask_park(sid: str = "s") -> WorkspaceSession:
    return _row(sid, f"ask_user:{sid}:call_0", until=timedelta(hours=1), tool="ask_user", parked_at=datetime.now(UTC) - timedelta(minutes=3))


def _drift() -> float:
    return sum(s.value for m in metrics.storage_cas_drift_total.collect() for s in m.samples if s.name.endswith("_total"))


class _HeldFlip:
    """Holds the flip at its write: the first ``patch_if`` that writes ``parked_status="resumable"`` waits, after every fence ran on the snapshot and
    before the backend sees the write, until the test has let another writer land."""

    def __init__(self, monkeypatch, storage) -> None:
        self.arrived, self.go = asyncio.Event(), asyncio.Event()
        self._armed = True
        real = storage.patch_if

        async def patch_if(row_id, patch=None, *args, **kwargs):
            if self._armed and isinstance(patch, dict) and patch.get("parked_status") == "resumable":
                self._armed = False
                self.arrived.set()
                await self.go.wait()
            return await real(row_id, patch, *args, **kwargs)

        monkeypatch.setattr(storage, "patch_if", patch_if)


async def _held_flip(storage, monkeypatch, payload: dict):
    """Start a flip of the park at its current snapshot and return it held at its write."""
    snapshot = await storage.get("s")
    held = _HeldFlip(monkeypatch, storage)
    task = asyncio.create_task(durably_mark_session_resumable(
        snapshot, event_key="ask_user:s:call_0", payload=payload, session_storage=storage, engine=None,
    ))
    await asyncio.wait_for(held.arrived.wait(), 5)
    return held, task


@pytest.mark.asyncio
async def test_two_flips_of_one_single_park_on_sqlite_exactly_one_lands(sp) -> None:
    """N5 (probe C1): two wakes that read the same park; only one may advance it, with its own payload."""
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(_ask_park())
    first, second = await sessions.get("s"), await sessions.get("s")

    landed = await asyncio.gather(*(
        durably_mark_session_resumable(snap, event_key="ask_user:s:call_0", payload={"response": name}, session_storage=sessions, engine=None)
        for snap, name in ((first, "first"), (second, "second"))
    ))

    assert sorted(landed) == [False, True]
    assert (await sessions.get("s")).parked_state["resume_event_payload"] == {"response": "first" if landed[0] else "second"}
    assert _drift() == 0


@pytest.mark.asyncio
async def test_a_flip_held_at_its_write_while_the_session_parks_again_is_refused_on_sqlite(sp, monkeypatch) -> None:
    """N5 (probe C2): the flip is held at its write while the release writes a NEW park (the release's own shape: one ``patch_if`` of the park columns,
    a fresh ``parked_at``); released, the flip must not land on the new park."""
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(_ask_park())
    held, task = await _held_flip(sessions, monkeypatch, {"response": "for the old park"})
    snapshot = await sessions.get("s")
    new_state = {"tool_call_id": "call_0", "yielded": {"tool_name": "ask_user", "event_key": "ask_user:s:call_0", "resume_metadata": {"prompt": "NEW"}}}
    assert await sessions.patch_if(
        "s", to_jsonable_python({"parked_status": "parked", "parked_at": datetime.now(UTC), "parked_state": new_state}),
        where={"workspace_id": [raw_generation(snapshot, "workspace_id")]},
    ) is not None

    held.go.set()

    assert await task is False
    after = await sessions.get("s")
    assert after.parked_status == "parked" and after.parked_state == new_state
    assert _drift() == 0


@pytest.mark.asyncio
async def test_a_flip_held_at_its_write_keeps_the_fields_another_writer_changed_on_sqlite(sp, monkeypatch) -> None:
    """N5 (probe C3): ``cancel_requested`` and ``last_seq`` written while the flip is held survive it (the flip writes only the two fields it owns)."""
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(_ask_park())
    held, task = await _held_flip(sessions, monkeypatch, {"response": "x"})
    await sessions.patch_if("s", {"cancel_requested": True, "last_seq": 41}, where={"parked_status": ["parked"]})

    held.go.set()

    assert await task is True
    after = await sessions.get("s")
    assert (after.parked_status, after.cancel_requested, after.last_seq) == ("resumable", True, 41)


@pytest.mark.asyncio
async def test_a_flip_held_at_its_write_while_the_session_ends_is_refused_on_sqlite(sp, monkeypatch) -> None:
    """N5 (probe C4): the session ENDS while the flip is held; released, the flip must not advance an ended row."""
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(_ask_park())
    held, task = await _held_flip(sessions, monkeypatch, {"response": "x"})
    await sessions.patch_if("s", {"status": SessionStatus.ENDED.value}, where={"parked_status": ["parked"]})

    held.go.set()

    assert await task is False
    after = await sessions.get("s")
    assert (after.parked_status, after.status) == ("parked", SessionStatus.ENDED)
    assert _drift() == 0


# ---- #707 review, round 3 ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_park_whose_parked_at_is_stored_in_the_isoformat_spelling_is_still_woken(sp) -> None:
    """B1 (probe P8, on SQLite). A park written outside the storage layer (a test fixture's raw SQL, an older build) can hold ``parked_at`` in the
    ``isoformat()`` spelling (``+00:00``), not the canonical ``Z`` one ``raw_generation`` gives. Both spell the SAME instant, so both name the same
    park: the flip lands and the drift tripwire stays quiet. A guard on the canonical spelling alone refused every wake of such a park for ever."""
    sessions = sp.get_storage(WorkspaceSession)
    parked_at = datetime.now(UTC) - timedelta(minutes=1)
    await sessions.create(_row("s", "ask_user:s:call_0", until=timedelta(hours=1), tool="ask_user", parked_at=parked_at))
    await sp.connection.execute("UPDATE sessions SET data = json_set(data, '$.parked_at', ?) WHERE id = ?", (parked_at.isoformat(), "s"))
    await sp.connection.commit()
    cursor = await sp.connection.execute("SELECT json_extract(data, '$.parked_at') FROM sessions WHERE id = ?", ("s",))
    [(stored,)] = await cursor.fetchall()
    snapshot = await sessions.get("s")
    assert stored == parked_at.isoformat() != raw_generation(snapshot, "parked_at"), "precondition: the stored spelling is not the canonical one"

    landed = await durably_mark_session_resumable(
        snapshot, event_key="ask_user:s:call_0", payload={"response": "x"}, session_storage=sessions, engine=None,
    )

    assert landed is True and (await sessions.get("s")).parked_status == "resumable", "a park whose parked_at is not canonically spelled is refused"
    assert _drift() == 0
