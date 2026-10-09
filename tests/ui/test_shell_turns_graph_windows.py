"""The console numbers a graph turn's windows exactly as the server does (ticket 01a11f35).

The trace is asked for under the ordinal the console counts (``SH_turnOfSeq`` over ``SH_windowsOfSeq``), and the server resolves that ordinal over ITS windows (``turn_windows``), so the two
scanners must agree record for record. A graph turn is one window on the server (a record with a ``node_id`` is inside it); the console must say the same, or every trace of a graph session asks
for the wrong turn. The logs are the ones the real writers produce (a real ``GraphExecutor`` fan-out: ``tests/session/graph_turn_shapes.py``), compared against the REAL server windows.
"""

from __future__ import annotations

import json

import pytest

from primer.session.terminals import TurnWindowScanner
from tests.session.graph_turn_shapes import FailingWorkerLLM, graph_turn
from tests.ui.test_shell_turns import _ctx, _server_windows

SHAPES = {
    "success": lambda: {},
    "both-nodes-fail": lambda: {"llm": FailingWorkerLLM(), "finish": "failed"},
    "one-node-fails-and-the-run-halts": lambda: {"llm": FailingWorkerLLM((2,)), "finish": "failed"},
    "one-node-fails-and-the-fan-out-continues": lambda: {"llm": FailingWorkerLLM((2,)), "on_failure": "collect"},
    "nested-two-levels": lambda: {"depth": 2},
}


@pytest.fixture(params=sorted(SHAPES))
async def records(request, monkeypatch):
    return request.param, await graph_turn(monkeypatch=monkeypatch, **SHAPES[request.param]())


def _console_ordinals(recs: list[dict]) -> dict[str, int]:
    return json.loads(_ctx().eval("JSON.stringify(SH_turnOfSeq(" + json.dumps(recs) + ", SH_windowsOfSeq(" + json.dumps(recs) + ")))"))


def test_the_console_files_a_graph_turn_in_one_window_like_the_server(records) -> None:
    name, recs = records

    expected = {str(seq): window for seq, window in _server_windows(recs).items()}

    assert set(expected.values()) == {0}, f"{name}: premise, the server files a graph turn in one window"
    assert _console_ordinals(recs) == expected, name


def test_the_console_scanner_gives_the_servers_verdict_for_every_record(records) -> None:
    name, recs = records
    scanner = TurnWindowScanner()
    server = [scanner.feed(rec) for rec in recs]

    console = json.loads(_ctx().eval(
        "(function () { var scanner = SH_newWindowScanner(); return JSON.stringify(" + json.dumps(recs) + ".map(function (rec) { return scanner.feed(rec); })); })()"
    ))

    assert console == server, name


@pytest.mark.asyncio
async def test_a_running_graph_is_numbered_alike_at_every_cut(monkeypatch) -> None:
    """Cut after every record: the open window is the first one on both sides, however many nodes have finished."""
    recs = await graph_turn(monkeypatch=monkeypatch)

    for cut in range(1, len(recs)):
        part = recs[:cut]
        expected = {str(seq): window for seq, window in _server_windows(part).items()}
        assert _console_ordinals(part) == expected, cut
        assert set(expected.values()) == {0}, cut


@pytest.mark.asyncio
async def test_a_two_turn_graph_session_is_numbered_alike(monkeypatch) -> None:
    first = await graph_turn(monkeypatch=monkeypatch)
    second = await graph_turn(monkeypatch=monkeypatch)
    recs = first + [dict(r, seq=r["seq"] + len(first)) for r in second]

    expected = {str(seq): window for seq, window in _server_windows(recs).items()}

    assert sorted(set(expected.values())) == [0, 1]
    assert _console_ordinals(recs) == expected


@pytest.mark.asyncio
async def test_a_log_from_before_records_carried_a_node_id_is_numbered_alike(monkeypatch) -> None:
    """Both sides keep the old (over-windowed) numbering of a log with no node ids: they must still agree."""
    recs = [{k: v for k, v in r.items() if k != "node_id"} for r in await graph_turn(monkeypatch=monkeypatch)]

    expected = {str(seq): window for seq, window in _server_windows(recs).items()}

    assert sorted(set(expected.values())) == [0, 1, 2]
    assert _console_ordinals(recs) == expected


def test_a_node_record_with_a_falsy_but_present_node_id_is_judged_like_the_server() -> None:
    """``[]`` and ``{}`` are falsy in Python and truthy in JavaScript: ``SH_pyTruthy`` makes the console say what the server says (a node id is a non-empty value)."""
    recs = [
        {"seq": 1, "kind": "user_input", "payload": {}},
        {"seq": 2, "kind": "done", "node_id": [], "payload": {"stop_reason": "stop"}},
        {"seq": 3, "kind": "done", "node_id": {}, "payload": {"stop_reason": "stop"}},
        {"seq": 4, "kind": "done", "node_id": "", "payload": {"stop_reason": "stop"}},
        {"seq": 5, "kind": "done", "node_id": "worker[0]", "payload": {"stop_reason": "stop"}},
        {"seq": 6, "kind": "done", "payload": {"stop_reason": "stop"}},
    ]

    expected = {str(seq): window for seq, window in _server_windows(recs).items()}

    assert _console_ordinals(recs) == expected
    assert expected["5"] == expected["6"] != expected["4"], "the named node's done is inside the window the next own done ends"
