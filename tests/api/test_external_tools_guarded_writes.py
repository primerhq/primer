"""The routes that write an ``ExternalToolCall`` status cannot overwrite a terminal row.

Two writers here: ``POST /v1/sessions/{sid}/yields/{tcid}/cancel`` (``flip_external_row``, ``cancelled``) and the read
surface's lazy timeout (``sweep_expired``, ``timed_out``, run by every GET list). Each used to write the whole row
from a snapshot it had read, so losing a race to a steer result overwrote ``completed``. Each race holds the LOSER at
its write (``tests/_support/held_write.py``), runs the winner (the steer carrying the result) to completion, then
releases it.

The app runs on a REAL SQLite storage: the in-memory fakes hand back the stored object itself, so a writer's
mutation of its snapshot would be visible to the other side before any write and the race could not happen. The
function-level races and the helper's own tests are in ``tests/session/test_external_row_guarded_writes.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from primer.model.external_tool import ExternalToolCall
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import GraphSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import WAKE_ENTRY_KEY, WAKE_PARK_KEY
from primer.storage.sqlite import SqliteStorageProvider
from tests._support.held_write import hold_write

# The graph suite's seeding helpers (a graph park over two external calls, tc-g1 on node n1 and tc-g2 on node n2).
from tests.api.test_external_tools_graph import _seed_graph, _seed_graph_calls, _seed_graph_session

# The steer suite's fixture stack (fake workspace backend + app + client) and seeding helpers. Its ``sp`` fixture
# is NOT imported: the one below replaces it with a SQLite provider, and ``pr``, ``wsr`` and ``app`` take that one.
from tests.api.test_external_tools_steer import (  # noqa: F401
    _parked_over,
    _seed_agent,
    _seed_call,
    _seed_session,
    _setup_ws,
    app,
    client,
    pr,
    wsr,
)

RESULT = {"customer": "c1"}
ROW_ID = "etool-fixed-1"  # the row id _seed_call writes and _parked_over's resume_metadata names


@pytest_asyncio.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "external-routes.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


class _RecordingBus:
    """Records every publish and delivers none (no listener runs): stands for the bus listener's LATE delivery,
    which on a single-event park is a no-op anyway because its guard admits ``parked`` only."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict) -> None:
        self.published.append((key, payload))


async def _parked_external_session(client, wsr, sp) -> str:
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_session(sp, wid, **_parked_over("sess-1"))
    return wid


async def _steer_result(client, wid: str):
    return await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={"tool_results": [{"tool_call_id": "tc-1", "result": RESULT}]},
    )


async def test_the_yields_cancel_route_racing_a_steer_result_leaves_completed(
    app, client, wsr, sp, monkeypatch,
) -> None:
    """N53d. A single-event external park on ``tc-1``, its row ``pending``. The yields-cancel route (R_c) publishes
    the cancelled marker on a bus that delivers nothing, calls ``flip_external_row`` and is held at that function's
    ``cancelled`` write; the steer carrying the result (R_s) runs to completion meanwhile; then R_c is released.
    R_s's ``completed`` stands, the park carries R_s's payload, and R_c still answers 202 (its row write is best
    effort, so a rejected write is silent)."""
    bus = _RecordingBus()
    app.state.event_bus = bus
    wid = await _parked_external_session(client, wsr, sp)
    await _seed_call(sp, "sess-1")
    calls = sp.get_storage(ExternalToolCall)
    held = hold_write(monkeypatch, calls, row_id=ROW_ID, status="cancelled")
    key = "external_tool:sess-1:tc-1"

    cancel_request = asyncio.create_task(
        client.post("/v1/sessions/sess-1/yields/tc-1/cancel", json={"reason": "operator"}),
    )
    await held.wait_arrived()
    r_s = await _steer_result(client, wid)
    await held.release()
    r_c = await cancel_request

    row = await calls.get(ROW_ID)
    assert row.status == "completed"
    assert row.result == RESULT
    assert row.is_error is False
    session = await sp.get_storage(WorkspaceSession).get("sess-1")
    assert session.parked_status == "resumable"
    assert session.parked_state["resume_event_payload"] == {"result": RESULT, "is_error": False, WAKE_PARK_KEY: session.parked_at.isoformat(), WAKE_ENTRY_KEY: ROW_ID}
    assert 200 <= r_s.status_code < 300, r_s.text
    assert r_c.status_code == 202, r_c.text
    # R_c did publish its cancelled marker; nothing delivered it
    assert any(k == key and p.get("__yield_cancelled__") for k, p in bus.published)


@pytest.mark.parametrize("listing", ["global", "session_pending"])
async def test_a_sweep_racing_a_steer_result_leaves_completed(
    app, client, wsr, sp, monkeypatch, listing: str,
) -> None:
    """N53c. A GET list sweeps a ``pending`` row whose ``timeout_at`` passed and is held at its ``timed_out`` write
    while the steer carrying the result completes the call. The row stays ``completed``, and the GET reports the
    real status: the global list says ``completed`` (not the ``pending`` it read, not the ``timed_out`` it tried to
    write), and the session's pending list does not list the call."""
    app.state.event_bus = _RecordingBus()
    wid = await _parked_external_session(client, wsr, sp)
    calls = sp.get_storage(ExternalToolCall)
    now = datetime.now(UTC)
    await calls.create(ExternalToolCall(
        id=ROW_ID, session_id="sess-1", tool_call_id="tc-1", tool_name="lookup_customer",
        arguments={}, created_at=now - timedelta(minutes=2), timeout_at=now - timedelta(seconds=1),
    ))
    held = hold_write(monkeypatch, calls, row_id=ROW_ID, status="timed_out")
    url = (
        "/v1/external_tool_calls?session_id=sess-1" if listing == "global"
        else "/v1/sessions/sess-1/external_tools/pending"
    )

    reader = asyncio.create_task(client.get(url))
    await held.wait_arrived()
    r_s = await _steer_result(client, wid)
    await held.release()
    listed = await reader

    assert 200 <= r_s.status_code < 300, r_s.text
    row = await calls.get(ROW_ID)
    assert (row.status, row.result, row.is_error) == ("completed", RESULT, False)
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    if listing == "global":
        # the whole stored row, resolved_at included (the sweep refreshes every field of the row it read)
        assert items == [row.model_dump(mode="json")]
        assert items[0]["status"] == "completed" and items[0]["resolved_at"] is not None
    else:
        assert items == []


async def test_a_sweep_times_out_a_pending_row_past_its_deadline(app, client, wsr, sp) -> None:
    """The lazy timeout itself, through the guarded write: a ``pending`` row whose ``timeout_at`` passed is stored
    and reported ``timed_out``, and drops out of the session's pending list."""
    app.state.event_bus = _RecordingBus()
    await _parked_external_session(client, wsr, sp)
    calls = sp.get_storage(ExternalToolCall)
    now = datetime.now(UTC)
    await calls.create(ExternalToolCall(
        id=ROW_ID, session_id="sess-1", tool_call_id="tc-1", tool_name="lookup_customer",
        arguments={}, created_at=now - timedelta(minutes=2), timeout_at=now - timedelta(seconds=1),
    ))
    await calls.create(ExternalToolCall(
        id="etool-live", session_id="sess-1", tool_call_id="tc-live", tool_name="lookup_customer",
        arguments={}, created_at=now, timeout_at=now + timedelta(minutes=10),
    ))

    listed = await client.get("/v1/external_tool_calls?session_id=sess-1")
    assert listed.status_code == 200, listed.text
    row = await calls.get(ROW_ID)
    assert (row.status, row.result, row.is_error) == ("timed_out", {"timed_out": True}, True)
    assert row.resolved_at is not None
    by_id = {i["id"]: i for i in listed.json()["items"]}
    # the list that swept the row reports it as stored, resolved_at included
    assert by_id[ROW_ID] == row.model_dump(mode="json")
    assert by_id["etool-live"]["status"] == "pending"

    pending = await client.get("/v1/sessions/sess-1/external_tools/pending")
    assert pending.status_code == 200, pending.text
    assert [i["tool_call_id"] for i in pending.json()["items"]] == ["tc-live"]


async def test_the_sweep_moves_on_past_a_rejected_row_that_is_gone_before_its_re_read(sp, monkeypatch) -> None:
    """Two expired rows read ``pending``. The first one's guarded write is rejected (it had already left
    ``pending``) and the row is deleted before the sweep's re-read: the sweep has nothing to refresh it from and
    goes on to the second row, which it times out."""
    from primer.api.routers.external_tools import sweep_expired

    calls = sp.get_storage(ExternalToolCall)
    now = datetime.now(UTC)
    expired = {"tool_name": "lookup_customer", "created_at": now - timedelta(minutes=2),
               "timeout_at": now - timedelta(seconds=1), "session_id": "sess-1"}
    gone = ExternalToolCall(id="etool-gone", tool_call_id="tc-gone", **expired)
    live = ExternalToolCall(id="etool-live", tool_call_id="tc-live", **expired)
    await calls.create(gone.model_copy(update={"status": "completed", "result": RESULT}))
    await calls.create(live)
    real_patch_if = calls.patch_if

    async def patch_if(row_id, patch=None, **kwargs):
        out = await real_patch_if(row_id, patch, **kwargs)
        if row_id == "etool-gone":
            assert out is None  # the guard rejected it: the row had already left pending
            await calls.delete("etool-gone")
        return out

    monkeypatch.setattr(calls, "patch_if", patch_if)

    await sweep_expired(calls, [gone, live])

    stored = await calls.get("etool-live")
    assert (stored.status, stored.result, stored.is_error) == ("timed_out", {"timed_out": True}, True)
    assert live == stored


async def test_an_instruction_steer_whose_cancel_lost_its_race_does_not_wake_the_park(
    app, client, wsr, sp, monkeypatch,
) -> None:
    """The steer's supersede wake runs only when one of its cancels LANDED. The instruction steer (R_i) reads the
    session while it is parked; its cancel of ``tc-1`` is held at its write while the steer carrying the result (R_s)
    completes the call; released, that cancel is rejected, so R_i cancelled nothing and must wake nothing. A wake
    from R_i's pre-R_s snapshot would replace R_s's result in the park with the cancelled marker (the durable flip
    refuses only an ENDED row). PR-6c reorders this loop wake-first; its d6 race replaces this test."""
    bus = _RecordingBus()
    app.state.event_bus = bus
    wid = await _parked_external_session(client, wsr, sp)
    await _seed_call(sp, "sess-1")
    calls = sp.get_storage(ExternalToolCall)
    held = hold_write(monkeypatch, calls, row_id=ROW_ID, status="cancelled")
    key = "external_tool:sess-1:tc-1"

    instruction = asyncio.create_task(client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer", json={"instruction": "actually, do something else"},
    ))
    await held.wait_arrived()
    r_s = await _steer_result(client, wid)
    await held.release()
    r_i = await instruction

    assert 200 <= r_s.status_code < 300, r_s.text
    assert r_i.status_code == 200, r_i.text
    row = await calls.get(ROW_ID)
    assert (row.status, row.result, row.is_error) == ("completed", RESULT, False)
    session = await sp.get_storage(WorkspaceSession).get("sess-1")
    assert session.parked_status == "resumable"
    assert session.parked_state["resume_event_payload"] == {"result": RESULT, "is_error": False, WAKE_PARK_KEY: session.parked_at.isoformat(), WAKE_ENTRY_KEY: ROW_ID}
    assert not [p for k, p in bus.published if k == key and p.get("__yield_cancelled__")]


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("NaN", None),
        ("Infinity", None),
        ("-Infinity", None),
        ('{"score": NaN, "xs": [1.5, Infinity, -Infinity]}', {"score": None, "xs": [1.5, None, None]}),
    ],
    ids=["nan", "infinity", "minus-infinity", "nested"],
)
async def test_a_steer_result_with_a_non_finite_number_completes_the_call_as_null(
    app, client, wsr, sp, raw: str, stored,
) -> None:
    """Python's json module writes NaN and the infinities by default, and the steer parses them. Such a result
    completes the call: the park receives null in their place (the session row is written whole), and the call's
    row stores the same null, so the two agree and the steer answers 200."""
    app.state.event_bus = _RecordingBus()
    wid = await _parked_external_session(client, wsr, sp)
    await _seed_call(sp, "sess-1")

    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        content='{"tool_results": [{"tool_call_id": "tc-1", "result": ' + raw + "}]}",
        headers={"content-type": "application/json"},
    )

    assert r.status_code == 200, r.text
    row = await sp.get_storage(ExternalToolCall).get(ROW_ID)
    assert (row.status, row.result, row.is_error) == ("completed", stored, False)
    session = await sp.get_storage(WorkspaceSession).get("sess-1")
    assert session.parked_state["resume_event_payload"] == {"result": stored, "is_error": False, WAKE_PARK_KEY: session.parked_at.isoformat(), WAKE_ENTRY_KEY: ROW_ID}


async def test_an_instruction_steer_wakes_only_the_calls_it_cancelled(app, client, wsr, sp) -> None:
    """A graph park over two external calls. The result of tc-g1 lands first (its row is ``completed``, the park
    carries its reply). An instruction-only steer then cancels the calls that are still pending: only tc-g2. It must
    wake only tc-g2 with the cancelled payload: waking tc-g1 too (every key of the park, as it did) replaces the
    reply that LANDED with the cancelled marker in the park and publishes a cancel for a call that was answered,
    while its row says ``completed``."""
    bus = _RecordingBus()
    app.state.event_bus = bus
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_graph(sp)
    await _seed_graph_session(sp, wid)
    await _seed_graph_calls(sp)
    k1, k2 = "external_tool:sess-1:tc-g1", "external_tool:sess-1:tc-g2"

    first = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={"tool_results": [{"tool_call_id": "tc-g1", "result": "ok"}]},
    )
    assert first.status_code == 200, first.text
    bus.published.clear()

    second = await client.post(f"/v1/workspaces/{wid}/sessions/sess-1/steer", json={"instruction": "stop"})

    assert second.status_code == 200, second.text
    calls = sp.get_storage(ExternalToolCall)
    assert (await calls.get("etool-g1")).status == "completed", "the answered call was cancelled"
    assert (await calls.get("etool-g2")).status == "cancelled"
    cancel_wakes = [k for k, p in bus.published if p.get("__yield_cancelled__")]
    assert cancel_wakes == [k2], f"the cancel was published for {cancel_wakes}"
    park = (await sp.get_storage(WorkspaceSession).get("sess-1")).parked_state
    leaves = {e["event_key"]: e["payload"] for e in park["resume_event_payloads"].values()}
    assert leaves[k1].pop(WAKE_PARK_KEY, None) is not None, "a reply names the park its producer read"
    assert leaves[k1].pop(WAKE_ENTRY_KEY, None) == "etool-g1", "and the call row it answers"
    assert leaves[k1] == {"result": "ok", "is_error": False}, "the landed reply was replaced by the cancelled marker"
    assert leaves[k2].get("__yield_cancelled__") is True


async def test_an_instruction_steer_that_cancels_both_calls_of_a_graph_park_keeps_both_cancelled_leaves(app, client, wsr, sp) -> None:
    """#707 review N2. The instruction cancels BOTH pending calls of one graph park and wakes each with the cancelled payload. Each wake is its own
    ``resume_event_payloads`` leaf, and the flip writes ``parked_state`` whole, so the second wake must start from the row the first one wrote: from
    the steer's one snapshot it dropped the first call's leaf while both rows say ``cancelled`` (that node waits for its deadline)."""
    bus = _RecordingBus()
    app.state.event_bus = bus
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_graph(sp)
    await _seed_graph_session(sp, wid)
    await _seed_graph_calls(sp)

    r = await client.post(f"/v1/workspaces/{wid}/sessions/sess-1/steer", json={"instruction": "stop"})

    assert r.status_code == 200, r.text
    calls = sp.get_storage(ExternalToolCall)
    assert [(await calls.get(row_id)).status for row_id in ("etool-g1", "etool-g2")] == ["cancelled", "cancelled"]
    park = (await sp.get_storage(WorkspaceSession).get("sess-1")).parked_state
    leaves = {e["event_key"]: e["payload"] for e in park["resume_event_payloads"].values()}
    assert sorted(leaves) == ["external_tool:sess-1:tc-g1", "external_tool:sess-1:tc-g2"], "the first cancelled call's leaf was dropped"
    assert all(payload.get("__yield_cancelled__") is True for payload in leaves.values())


async def test_two_instruction_steers_that_read_one_graph_park_and_each_cancel_one_call_keep_both_cancelled_leaves(
    app, client, wsr, sp, monkeypatch,
) -> None:
    """Ticket 01a122cc-effa (C6 through the steer's cancel loop). Two instruction steers read the same graph park. Steer A lands the cancel of tc-g1's row
    and is held at its write of tc-g2's (the rows are cancelled in id order). Steer B, which read the park before A woke anything, lands the cancel of tc-g2
    and wakes it. Released, A's tc-g2 write is refused and A wakes tc-g1 from ITS snapshot, which has no tc-g2 leaf: the flip wrote the whole
    ``parked_state`` from that snapshot and dropped B's cancel while both rows say ``cancelled`` (that node waits for its deadline). Each cancel is its
    own leaf, so both stay."""
    bus = _RecordingBus()
    app.state.event_bus = bus
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_graph(sp)
    await _seed_graph_session(sp, wid)
    await _seed_graph_calls(sp)
    calls = sp.get_storage(ExternalToolCall)
    sessions = sp.get_storage(WorkspaceSession)
    held = hold_write(monkeypatch, calls, row_id="etool-g2", status="cancelled")
    url = f"/v1/workspaces/{wid}/sessions/sess-1/steer"
    k1, k2 = "external_tool:sess-1:tc-g1", "external_tool:sess-1:tc-g2"

    async with asyncio.timeout(30):
        steer_a = asyncio.create_task(client.post(url, json={"instruction": "stop"}))
        await held.wait_arrived()
        before_b = ((await calls.get("etool-g1")).status, (await calls.get("etool-g2")).status, (await sessions.get("sess-1")).parked_status)
        steer_b = await client.post(url, json={"instruction": "stop as well"})
        await held.release()
        steer_a = await steer_a
        after = await sessions.get("sess-1")
        statuses = [(await calls.get(row_id)).status for row_id in ("etool-g1", "etool-g2")]

    assert before_b == ("cancelled", "pending", "parked"), "precondition: A landed tc-g1's cancel and has woken nothing when B reads the park"
    assert (steer_a.status_code, steer_b.status_code) == (200, 200), (steer_a.text, steer_b.text)
    assert statuses == ["cancelled", "cancelled"]
    assert sorted(k for k, p in bus.published if p.get("__yield_cancelled__")) == [k1, k2], "each steer woke the call whose cancel it landed"
    leaves = {e["event_key"]: e["payload"] for e in ((after.parked_state or {}).get("resume_event_payloads") or {}).values()}
    assert sorted(leaves) == [k1, k2], "a steer's cancel wake from its own snapshot dropped the other steer's cancelled leaf"
    assert all(payload.get("__yield_cancelled__") is True for payload in leaves.values())


def _graph_external_park(wid: str, waits: list[tuple[str, str, str]], *, parked_at: datetime) -> WorkspaceSession:
    """A graph park of ``sess-1`` over external calls ``(node, tool_call_id, row id)``, shaped like the graph suite's."""
    entries = [
        {"node_id": node, "tool_call_id": tcid, "event_key": f"external_tool:sess-1:{tcid}", "tool_name": "_external",
         "resume_metadata": {"original_call": {"id": tcid, "name": "lookup", "arguments": {}}, "external_call_row_id": row_id},
         "llm_messages": [], "iteration": 1, "frames": [], "leaf": None}
        for node, tcid, row_id in waits
    ]
    keys = [entry["event_key"] for entry in entries]
    return WorkspaceSession(
        id="sess-1", workspace_id=wid, binding=GraphSessionBinding(graph_id="gr-ext"), status=SessionStatus.RUNNING, created_at=parked_at,
        started_at=parked_at, parked_status="parked", parked_event_key=keys[0], parked_event_keys=keys, parked_until=parked_at + timedelta(seconds=600),
        parked_at=parked_at,
        parked_state={
            "schema_version": 1, "tool_call_id": None, "yielded": {"tool_name": "_approval", "event_key": "graph:sess-1", "resume_metadata": {}},
            "graph_checkpoint": {"pending_agent_yields": entries, "pending_toolcalls": [], "pending_dispatch": []},
        },
    )


async def _seed_external_park(sp, wid: str, waits: list[tuple[str, str, str]], *, parked_at: datetime) -> None:
    await sp.get_storage(WorkspaceSession).create(_graph_external_park(wid, waits, parked_at=parked_at))
    for node, tcid, row_id in waits:
        await sp.get_storage(ExternalToolCall).create(ExternalToolCall(
            id=row_id, session_id="sess-1", node_id=node, tool_call_id=tcid, tool_name="lookup", arguments={}, created_at=parked_at,
        ))


async def test_an_instruction_steer_that_cancels_three_calls_of_a_graph_park_keeps_three_cancelled_leaves(app, client, wsr, sp) -> None:
    """#707 review round 2, N3. THREE pending calls: each further cancel wake re-reads the row, so the third keeps the second's leaf as the second kept
    the first's, and every cancel names the park the route read."""
    bus = _RecordingBus()
    app.state.event_bus = bus
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_graph(sp)
    parked_at = datetime.now(UTC)
    await _seed_external_park(sp, wid, [(f"n{i}", f"tc-{i}", f"etool-{i}") for i in (1, 2, 3)], parked_at=parked_at)

    r = await client.post(f"/v1/workspaces/{wid}/sessions/sess-1/steer", json={"instruction": "stop"})

    assert r.status_code == 200, r.text
    calls = sp.get_storage(ExternalToolCall)
    assert [(await calls.get(f"etool-{i}")).status for i in (1, 2, 3)] == ["cancelled"] * 3
    park = (await sp.get_storage(WorkspaceSession).get("sess-1")).parked_state
    leaves = {e["event_key"]: e["payload"] for e in park["resume_event_payloads"].values()}
    assert sorted(leaves) == [f"external_tool:sess-1:tc-{i}" for i in (1, 2, 3)], "a cancelled call's leaf was dropped"
    assert all(payload.get("__yield_cancelled__") is True for payload in leaves.values())
    assert {payload[WAKE_PARK_KEY] for payload in leaves.values()} == {parked_at.isoformat()}


async def test_an_instruction_steer_does_not_wake_a_park_that_no_longer_waits_on_a_cancelled_call_after_the_re_read(app, client, wsr, sp) -> None:
    """#707 review round 2, N1 (the steer variant of probe N2g). After the first cancel's wake and publish, the graph re-parked on ANOTHER entry that
    shares the second call's raw id under another key (n3's ask_user under ``call_0``). The re-read park does not wait on the second call any more, so
    its cancel is not woken or published: landing there would have the resume answer n3's question with the cancel (a key that names no pending
    entry selects by the raw id)."""
    sessions = sp.get_storage(WorkspaceSession)
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_graph(sp)
    await _seed_external_park(sp, wid, [("n1", "tc-1", "etool-1"), ("n2", "call_0", "etool-2")], parked_at=datetime.now(UTC) - timedelta(minutes=1))
    question = {"node_id": "n3", "tool_call_id": "call_0", "event_key": "ask_user:sess-1:n3:call_0", "tool_name": "ask_user",
                "resume_metadata": {"prompt": "approve?", "gate_id": "d" * 32}, "llm_messages": [], "iteration": 1, "frames": [], "leaf": None}
    new_park = _graph_external_park(wid, [("n2", "call_0", "etool-2")], parked_at=datetime.now(UTC)).model_copy(update={
        "parked_event_key": question["event_key"], "parked_event_keys": [question["event_key"]],
        "parked_state": {
            "schema_version": 1, "tool_call_id": None, "yielded": {"tool_name": "_approval", "event_key": "graph:sess-1", "resume_metadata": {}},
            "graph_checkpoint": {"pending_agent_yields": [question], "pending_toolcalls": [], "pending_dispatch": []},
        },
    })

    class _ReparkAfterTheFirstCancel(_RecordingBus):
        async def publish(self, key: str, payload: dict) -> None:
            await super().publish(key, payload)
            if payload.get("__yield_cancelled__") and len([p for _k, p in self.published if p.get("__yield_cancelled__")]) == 1:
                await sessions.update(new_park)

    bus = _ReparkAfterTheFirstCancel()
    app.state.event_bus = bus

    r = await client.post(f"/v1/workspaces/{wid}/sessions/sess-1/steer", json={"instruction": "stop"})

    assert r.status_code == 200, r.text
    after = await sessions.get("sess-1")
    assert after.parked_status == "parked", "a cancel for a call the re-read park does not wait on flipped it"
    assert after.parked_state == new_park.parked_state, "the new park is untouched"
    assert [k for k, p in bus.published if p.get("__yield_cancelled__")] == ["external_tool:sess-1:tc-1"], "the skipped cancel was published"
