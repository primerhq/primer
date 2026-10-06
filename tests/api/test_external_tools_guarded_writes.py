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
from primer.model.workspace_session import WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider
from tests._support.held_write import hold_write

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
    assert session.parked_state["resume_event_payload"] == {"result": RESULT, "is_error": False}
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
        assert [(i["id"], i["status"], i["result"]) for i in items] == [(ROW_ID, "completed", RESULT)]
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

    pending = await client.get("/v1/sessions/sess-1/external_tools/pending")
    assert pending.status_code == 200, pending.text
    assert [i["tool_call_id"] for i in pending.json()["items"]] == ["tc-live"]

    listed = await client.get("/v1/external_tool_calls?session_id=sess-1")
    assert listed.status_code == 200, listed.text
    by_id = {i["id"]: i for i in listed.json()["items"]}
    assert by_id[ROW_ID]["status"] == "timed_out"
    assert by_id[ROW_ID]["result"] == {"timed_out": True}
    assert by_id[ROW_ID]["is_error"] is True
    assert by_id["etool-live"]["status"] == "pending"
    row = await calls.get(ROW_ID)
    assert (row.status, row.result, row.is_error) == ("timed_out", {"timed_out": True}, True)
    assert row.resolved_at is not None
