"""A wake of a multi-event park writes only its own leaves of ``parked_state`` (ticket 01a122cc-effa, item 1; C6 of the #707 review).

The durable flip (``durably_mark_session_resumable``) is one ``patch_if`` guarded on the park it read (#707). It wrote the WHOLE ``parked_state`` it built
from its snapshot: the snapshot's state plus its own ``resume_event_payloads`` leaf. Two wakes of one multi-event park on two keys (replies to two gates of
one graph superstep, two external results, a result and a cancel) that read the same row each wrote their own state, so the second write replaced the
first wake's leaf: that reply was silently lost (its node waits for its deadline, and its call row says answered, so it cannot be sent again). The flip now
sets only its own leaves with nested ``set_paths``, which the backend applies to the row's CURRENT document: ``parked_state.resume_event_payload``,
``parked_state.resume_event_key`` and, on a multi-event park, ``parked_state.resume_event_payloads.<leaf key>`` (the dispatch key through
``leaf_key_for``). The guard (#707's park fence) is unchanged.

These run on a REAL SQLite storage: a row read there is a copy, so the later wake really holds a stale snapshot (the in-memory fake hands back the stored
object itself). The interleave is deterministic: every snapshot is read first, then the wakes are applied one after the other. The Postgres twin, with a
real row lock, is ``test_park_flip_writes_leaves_live.py``; the producers' own races are in ``test_external_row_guarded_writes.py`` (results) and
``tests/api/test_external_tools_guarded_writes.py`` (the steer's cancels).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

import primer.observability.metrics as metrics
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import WorkspaceSession
from primer.session.yields import durably_mark_session_resumable
from primer.storage.sqlite import SqliteStorageProvider
from tests.session.test_machine_wake_fences import _graph_park
from tests.session.test_machine_wake_replay_round2 import _row

#: Every test body is bounded: a wake that never returns fails the test instead of hanging the lane.
_BOUND_S = 30.0


@pytest.fixture(autouse=True)
def _fresh_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


@pytest_asyncio.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "park-flip-leaves.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


def _drift() -> float:
    return sum(s.value for m in metrics.storage_cas_drift_total.collect() for s in m.samples if s.name.endswith("_total"))


def _gate(sid: str, node: str, tcid: str) -> dict:
    """A graph agent node's pending ``ask_user`` gate, keyed by node and call like the graph's capture sites."""
    return {"node_id": node, "tool_call_id": tcid, "event_key": f"ask_user:{sid}:{node}:{tcid}", "tool_name": "ask_user", "resume_metadata": {"prompt": node}}


def _leaves(row: WorkspaceSession) -> dict:
    """The accumulated replies by the key each one answers (the resume reads each entry's own ``event_key``, never the dict key)."""
    return {entry["event_key"]: entry["payload"] for entry in ((row.parked_state or {}).get("resume_event_payloads") or {}).values()}


async def _wake_each_from_one_snapshot(sessions, sid: str, keys: list[str]) -> list[bool]:
    """Every wake reads the park BEFORE any of them writes, then they land one after the other, each from its own (stale) snapshot."""
    snapshots = [await sessions.get(sid) for _ in keys]
    return [
        await durably_mark_session_resumable(snapshot, event_key=key, payload={"response": key}, session_storage=sessions, engine=None)
        for snapshot, key in zip(snapshots, keys)
    ]


@pytest.mark.parametrize("gates", [2, 3])
async def test_wakes_of_one_multi_event_park_that_read_one_snapshot_keep_every_leaf(sp, gates: int) -> None:
    """Probe C6 (/var/tmp/review707/r3/probe_707_r3_pg_c6.py, on SQLite): replies to the gates of one graph superstep, each wake read the park before
    any of them wrote. Every wake lands, and every reply stays in ``resume_event_payloads``: the whole-``parked_state`` write kept only the last one."""
    sessions = sp.get_storage(WorkspaceSession)
    entries = tuple(_gate("gs", f"n{i}", f"call_{i}") for i in range(gates))
    keys = [entry["event_key"] for entry in entries]
    async with asyncio.timeout(_BOUND_S):
        await sessions.create(_graph_park("gs", parked_at=datetime.now(UTC) - timedelta(minutes=1), agent_yields=entries))

        landed = await _wake_each_from_one_snapshot(sessions, "gs", keys)
        after = await sessions.get("gs")

    assert {"landed": landed, "parked_status": after.parked_status, "leaves": _leaves(after), "drift": _drift()} == {
        "landed": [True] * gates, "parked_status": "resumable", "leaves": {key: {"response": key} for key in keys}, "drift": 0,
    }, "a later wake from the same snapshot dropped an earlier wake's reply"


@pytest.mark.parametrize(
    "nodes",
    [('n"1', "n\\2"), ('a"b', "a%22b"), ("n\t1", "n%1")],
    ids=["quote-and-backslash", "a-quote-and-its-literal-encoding", "control-character-and-percent"],
)
async def test_a_node_id_patch_if_cannot_take_as_a_path_element_keeps_its_leaf(sp, nodes: tuple[str, str]) -> None:
    """The leaf's dict key ends in the node id, which can hold any character, and ``patch_if`` refuses a path element with a quote, a backslash or a
    control character: the key goes through ``leaf_key_for`` (percent-encoding, injective), so such a park stays wakeable and two node ids that differ
    only by that encoding keep two leaves. The entries keep their original event keys."""
    sessions = sp.get_storage(WorkspaceSession)
    entries = tuple(_gate("gs", node, "call_0") for node in nodes)
    keys = [entry["event_key"] for entry in entries]
    async with asyncio.timeout(_BOUND_S):
        await sessions.create(_graph_park("gs", parked_at=datetime.now(UTC) - timedelta(minutes=1), agent_yields=entries))

        landed = await _wake_each_from_one_snapshot(sessions, "gs", keys)
        after = await sessions.get("gs")

    assert {"landed": landed, "leaves": _leaves(after)} == {"landed": [True, True], "leaves": {key: {"response": key} for key in keys}}
    assert len((after.parked_state or {})["resume_event_payloads"]) == 2


async def test_a_key_with_an_empty_tail_still_wakes_a_multi_event_park(sp) -> None:
    """The dispatch key is the event key past its ``<kind>:<session>:`` prefix, so a key with nothing after the prefix (an approval of a call whose
    provider id was empty) has an empty dispatch key, which ``patch_if`` refuses as a path element. Such a park stays wakeable and the reply is kept."""
    sessions = sp.get_storage(WorkspaceSession)
    key, batch = "tool_approval:s:", "tool_wait:s:0:x"
    row = _row("s", key, until=timedelta(hours=1), tool="tool_approval", parked_at=datetime.now(UTC) - timedelta(minutes=1))
    async with asyncio.timeout(_BOUND_S):
        await sessions.create(row.model_copy(update={"parked_event_keys": [key, batch]}))

        landed = await durably_mark_session_resumable(
            await sessions.get("s"), event_key=key, payload={"decision": "approved"}, session_storage=sessions, engine=None,
        )
        after = await sessions.get("s")

    assert {"landed": landed, "leaves": _leaves(after)} == {"landed": True, "leaves": {key: {"decision": "approved"}}}
