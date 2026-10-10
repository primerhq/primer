"""One window per invocation across a REOPEN, with the records the real writers leave (ticket 01a11f35, round 3 of #701; rule (c) of the lead's ruling on the old logs).

The graph's own end closes a graph turn, but not every path that ends a graph session writes it (a log written before the end was a record; a parked graph that is cancelled, whose row is ended inline; the
resume coordinators' early exits; the tool_wait coordinators), and a failed graph restarted with no message writes a divider and NO user_input, so the failure state of the old invocation survived into the
new one and its end was filed as a copy of the old failure (the second invocation was swallowed: one window, ``usage.turns`` 1, ``/turns/1`` a 404).

The rule (``TurnWindowScanner``, and ``SH_newWindowScanner`` in the console): an ``invocation_divider`` is written ONLY to an ENDED session, so an invocation that is still open when a divider arrives ended without its
end record. A graph run is "open" when a record with a ``node_id`` came after the last close; the divider then CLOSES it (and is its last record), and in every case it restarts the failure fold (nothing before a
reopen is a copy of anything after it). After a closed turn the divider stays INSIDE, the first record of the next window, as before. The residual, accepted and documented: the LAST invocation of a session that
ended with no end record stays one open window until a reopen.

Every log here is written by the real writers (``tests/session/graph_turn_paths.py``): ``run_one_session_turn`` with a real ``WorkspaceGraphExecutor``, a real park, the real ``cancel_session``, the real turn log.
"""

from __future__ import annotations

import json

import pytest

from primer.session.timeline import build_turn_timeline, turn_windows
from primer.session.turns import count_turn_state
from primer.session.usage import session_usage
from tests.session.graph_turn_paths import (
    SID, LegacyWriters, TurnLogs, cancel_parked, failed_then_restarted_without_input, legacy_then_new, open_turn, park, parked_cancelled_then_reopened, records, release, run_graph,
)
from tests.session.test_dispatch import fake_event_bus, fake_storage_provider, fake_workspace_io  # noqa: F401  (fixtures)


def _closed(lines: list[str]) -> list[int | None]:
    return [w["terminal_seq"] for w in turn_windows(lines)]


def _timeline(lines: list[str], tl: TurnLogs, n: int):
    return build_turn_timeline(message_lines=lines, turn_log_lines=tl.lines(), turn_no=n)


# ---- B1: a failed graph restarted with no message ---------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["worker", "iterations", "raised"], ids=["a worker failed", "the iteration limit", "the executor raised"])
async def test_a_failed_graph_restarted_with_no_message_is_a_second_closed_window(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider, kind: str) -> None:
    """``restart_session(input=None)`` writes a divider and no ``user_input``: the failure fold of the old invocation must not swallow the new one's end."""
    tl = await failed_then_restarted_without_input(kind, tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus)

    lines = fake_workspace_io.read_lines(SID)

    closed = _closed(lines)
    assert len(closed) == 2 and None not in closed, f"two closed windows expected, got terminal_seqs {closed}"
    assert session_usage(lines).turns == 2
    second = _timeline(lines, tl, 1)
    assert second is not None, "/turns/1 resolves"
    assert second["terminal_seq"] == closed[1] and second["started_at"] == tl.own_started()[1], "/turns/1 is the second invocation's envelope"


# ---- B2: a session whose earlier invocation ended with no end record -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reopen_closes_a_graph_invocation_that_wrote_no_end(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    """The log of a graph run as the OLD writers wrote it (no end record), then a reopen with a message and a run by the current writers: the divider closes the old window."""
    tl = await legacy_then_new(1, tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus)

    lines = fake_workspace_io.read_lines(SID)
    recs = records(fake_workspace_io)

    divider = next(r["seq"] for r in recs if r["kind"] == "invocation_divider")
    closed = _closed(lines)
    assert closed[0] == divider, "window 0 ends at the divider (it ended without its end record)"
    assert len(closed) == 2 and closed[1] is not None and closed[1] > divider
    assert session_usage(lines).turns == 2
    assert _timeline(lines, tl, 1)["started_at"] == tl.own_started()[1]


@pytest.mark.asyncio
async def test_the_last_invocation_with_no_end_stays_one_open_window_until_a_reopen(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    """The accepted residual, pinned: two invocations by the old writers. Window 0 is closed by the divider; window 1 has nothing to close it and stays open (``usage.turns`` one short)."""
    tl = TurnLogs()
    await open_turn(fake_storage_provider, fake_workspace_io)
    with LegacyWriters():
        outcome, _ = await run_graph(tmp_path / "a", fake_storage_provider, fake_workspace_io, fake_event_bus, tl)
        await release(fake_storage_provider, outcome)
        await open_turn(fake_storage_provider, fake_workspace_io, reopen=True)
        await run_graph(tmp_path / "b", fake_storage_provider, fake_workspace_io, fake_event_bus, tl)

    lines = fake_workspace_io.read_lines(SID)

    divider = next(r["seq"] for r in records(fake_workspace_io) if r["kind"] == "invocation_divider")
    assert _closed(lines) == [divider, None]
    assert session_usage(lines).turns == 1
    assert _timeline(lines, tl, 1)["started_at"] == tl.own_started()[1], "the envelope join still lines up: /turns/1 is the second invocation"


@pytest.mark.asyncio
async def test_old_then_new_then_new_lines_up_every_envelope(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    tl = await legacy_then_new(2, tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus)

    lines = fake_workspace_io.read_lines(SID)

    closed = _closed(lines)
    assert len(closed) == 3 and None not in closed
    assert session_usage(lines).turns == 3
    for n in range(3):
        timeline = _timeline(lines, tl, n)
        assert timeline is not None and timeline["started_at"] == tl.own_started()[n], f"/turns/{n} is the {n}-th envelope"
        assert timeline["terminal_seq"] == closed[n]


@pytest.mark.asyncio
async def test_a_parked_graph_cancelled_while_parked_is_closed_by_the_reopen(tmp_path, monkeypatch, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    """The pool ends a cancelled parked row inline and writes no record (a gap agents have too: ticket). The next reopen closes the window, so the next invocation is a window of its own."""
    tl = await parked_cancelled_then_reopened(tmp_path, monkeypatch=monkeypatch, sp=fake_storage_provider, io=fake_workspace_io, bus=fake_event_bus)

    lines = fake_workspace_io.read_lines(SID)

    divider = next(r["seq"] for r in records(fake_workspace_io) if r["kind"] == "invocation_divider")
    closed = _closed(lines)
    assert len(closed) == 2 and closed[0] == divider and closed[1] is not None
    assert session_usage(lines).turns == 2


@pytest.mark.asyncio
async def test_the_cancel_of_a_parked_graph_writes_no_record_which_is_why_the_divider_closes_it(tmp_path, monkeypatch, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    """Pins the premise of the test above with the real ``cancel_session``: the log after the cancel has no node-less terminal at all."""
    tl = TurnLogs()
    await open_turn(fake_storage_provider, fake_workspace_io)
    await park(tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, tl, monkeypatch)
    before = len(fake_workspace_io.read_lines(SID))

    row = await cancel_parked(fake_storage_provider)

    assert row.status.value == "ended"
    after = records(fake_workspace_io)
    assert len(after) == before and not [r for r in after if r["kind"] in ("done", "error", "cancelled") and not r.get("node_id")]


# ---- (6) the open-turn count: the divider closes the old input --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_divider_closes_the_open_input_of_an_invocation_that_wrote_no_end(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    tl = await legacy_then_new(1, tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus)
    lines = fake_workspace_io.read_lines(SID)

    state = count_turn_state(lines, cursor=0)

    assert (state.open_user_inputs, state.terminals, state.open_turns) == (2, 2, 0), "two inputs, two closes (the divider's and the end's), nothing open"
    assert json.loads(lines[0])["kind"] == "user_input" and tl.lines()
