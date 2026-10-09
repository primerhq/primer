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

import logging
from datetime import UTC, datetime, timedelta

import pytest

import primer.observability.metrics as metrics
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import with_wake_entry
from primer.session.yields import durably_mark_session_resumable, flip_sessions_parked_on
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
async def test_two_sibling_entries_sharing_a_key_each_accept_only_their_own_subscription() -> None:
    """One graph session, two nodes subscribed to one trigger: the key is shared (it carries the session), the subscription ids are not."""
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
