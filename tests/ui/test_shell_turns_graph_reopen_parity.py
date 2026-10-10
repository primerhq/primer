"""The console numbers a graph session's windows across a REOPEN exactly as the server does, at every cut (ticket 01a11f35, round 3 of #701).

Rule (c): an ``invocation_divider`` closes a graph run that is still open (a record with a ``node_id`` since the last close) and restarts the failure fold, in ``TurnWindowScanner`` and in
``SH_newWindowScanner``. The logs below are written by the REAL writers (``tests/session/graph_turn_paths.py``: a failed graph restarted with no message, the old writers' log followed by new invocations, a cancelled
parked graph, a plain successful reopen); the hand-built shapes cover the divider's edges. For each: the verdict of every record, and for every cut of the log the window each record is filed in and the ordinal of the
open window, compared with the server's ``turn_windows``.
"""

from __future__ import annotations

import json

import pytest

from primer.session.terminals import TurnWindowScanner
from tests.session.graph_turn_paths import (
    SID, OK, TurnLogs, failed_then_restarted_without_input, legacy_then_new, open_turn, parked_cancelled_then_reopened, records, release, run_graph,
)
from tests.session.test_dispatch import fake_event_bus, fake_storage_provider, fake_workspace_io  # noqa: F401  (fixtures)
from tests.ui.test_shell_turns import _ctx, _server_windows
from tests.ui.test_shell_turns_graph_windows import _console_ordinals


def _console_verdicts(recs: list[dict]) -> list[str]:
    return json.loads(_ctx().eval(
        "(function () { var scanner = SH_newWindowScanner(); return JSON.stringify(" + json.dumps(recs) + ".map(function (rec) { return scanner.feed(rec); })); })()"
    ))


def _assert_alike(recs: list[dict]) -> None:
    scanner = TurnWindowScanner()
    assert _console_verdicts(recs) == [scanner.feed(rec) for rec in recs]
    for cut in range(1, len(recs) + 1):
        part = recs[:cut]
        assert _console_ordinals(part) == {str(seq): window for seq, window in _server_windows(part).items()}, f"cut at record {cut}"


# ---- the real writers --------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["worker", "iterations", "raised"])
async def test_a_failed_graph_restarted_with_no_message(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider, kind: str) -> None:
    await failed_then_restarted_without_input(kind, tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus)

    _assert_alike(records(fake_workspace_io))


@pytest.mark.asyncio
async def test_the_old_writers_log_followed_by_new_invocations(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    await legacy_then_new(2, tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus)

    _assert_alike(records(fake_workspace_io))


@pytest.mark.asyncio
async def test_a_parked_graph_cancelled_and_reopened(tmp_path, monkeypatch, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    await parked_cancelled_then_reopened(tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch)

    _assert_alike(records(fake_workspace_io))


@pytest.mark.asyncio
async def test_two_ordinary_invocations_through_the_reopen_path(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    tl = TurnLogs()
    await open_turn(fake_storage_provider, fake_workspace_io)
    outcome, _ = await run_graph(tmp_path / "a", fake_storage_provider, fake_workspace_io, fake_event_bus, tl, scripts=[OK])
    await release(fake_storage_provider, outcome)
    await open_turn(fake_storage_provider, fake_workspace_io, reopen=True)
    await run_graph(tmp_path / "b", fake_storage_provider, fake_workspace_io, fake_event_bus, tl, scripts=[OK])

    _assert_alike(records(fake_workspace_io))


# ---- hand-built shapes: the divider's edges and the string flag (review nit N5) -------------------------------------------------------------------------


_END_OK = {"kind": "done", "payload": {"stop_reason": "stop", "raw_reason": "graph_ended", "graph_end": True}}
_END_FAILED = {"kind": "done", "payload": {"stop_reason": "error", "raw_reason": "graph_failed", "graph_end": True}}
_NODE_DONE = {"kind": "done", "node_id": "w0", "payload": {"stop_reason": "stop"}}
_GRAPH_ERROR = {"kind": "error", "payload": {"code": "max_iterations_exceeded", "message": "graph ran for 1 iterations"}}
_STRING_FLAG_DONE = {"kind": "done", "payload": {"stop_reason": "stop", "graph_end": "true"}}
_AGENT_DONE = {"kind": "done", "payload": {"stop_reason": "stop"}}
_AGENT_ERROR = {"kind": "error", "payload": {"message": "boom", "code": "server_error", "fatal": True}}
_DISPATCH_FAILURE = {"kind": "error", "payload": {"message": "boom", "code": "/errors/internal", "title": "Internal error", "status": 500}}
_MARKER = {"kind": "error", "payload": {"reason": "unknown", "terminal": True}}
_AGENT_MARKER = {"kind": "agent_marker", "payload": {"agent_id": "a"}}
_USER = {"kind": "user_input", "payload": {"text": "go"}}
_DIVIDER = {"kind": "invocation_divider", "payload": {"invocation": 2}}


@pytest.mark.parametrize(
    "shapes",
    [
        pytest.param([_USER, _NODE_DONE, _DIVIDER, _USER, _NODE_DONE, _END_OK], id="a divider closes a graph run that wrote no end"),
        pytest.param([_USER, _NODE_DONE, _END_FAILED, _DIVIDER, _NODE_DONE, _END_OK], id="a restart with no message after a failed end"),
        pytest.param([_USER, _NODE_DONE, _GRAPH_ERROR, _END_FAILED, _DIVIDER, _NODE_DONE, _END_FAILED], id="two graph-level failures with a restart between"),
        pytest.param([_USER, _NODE_DONE, _GRAPH_ERROR, _NODE_DONE, _END_FAILED, _DIVIDER, _NODE_DONE, _END_OK], id="a late node record between a graph-level error and its copy, then a divider"),
        pytest.param([_USER, _AGENT_DONE, _DIVIDER, _USER, _AGENT_DONE], id="a divider after a closed agent turn"),
        pytest.param([_USER, _AGENT_ERROR, _DISPATCH_FAILURE, _MARKER, _DIVIDER, _USER, _AGENT_ERROR, _DISPATCH_FAILURE, _MARKER], id="a divider after a failure and its copies"),
        pytest.param([_USER, _AGENT_DONE, _AGENT_MARKER, _DIVIDER, _USER, _AGENT_DONE], id="a divider after an agent_marker"),
        pytest.param([_USER, _DIVIDER, _USER, _AGENT_DONE], id="an agent input with no node record before a divider"),
        pytest.param([_USER, _NODE_DONE, _DIVIDER, _DIVIDER, _USER, _NODE_DONE, _END_OK], id="two dividers in a row"),
        pytest.param([_USER, _GRAPH_ERROR, _STRING_FLAG_DONE], id="a graph-level error and then a done whose graph_end flag is a string (an ordinary done)"),
        pytest.param([_USER, _NODE_DONE, _STRING_FLAG_DONE], id="a string flag does not close a graph run"),
    ],
)
def test_hand_built_shapes(shapes: list[dict]) -> None:
    _assert_alike([dict(shape, seq=i + 1) for i, shape in enumerate(shapes)])


def test_the_divider_closes_a_graph_run_on_both_sides() -> None:
    recs = [dict(shape, seq=i + 1) for i, shape in enumerate([_USER, _NODE_DONE, _DIVIDER, _USER, _NODE_DONE, _END_OK])]

    scanner = TurnWindowScanner()
    assert [scanner.feed(r) for r in recs] == ["inside", "inside", "closes", "inside", "inside", "closes"]
    assert _console_verdicts(recs) == ["inside", "inside", "closes", "inside", "inside", "closes"]
    assert sorted(set(_server_windows(recs).values())) == [0, 1]
