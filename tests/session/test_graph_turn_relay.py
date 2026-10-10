"""What the final-result relay posts for a graph run, with the records the real writers leave (ticket 01a11f35, round 3 of #701, B3 and the end-drop pin).

``derive_session_final_text`` takes the graph's own end as the VERDICT of the turn and not as a text boundary, so the text is the End node's output written before it, or, for a pass-through End (one with no output),
the last node's answer. After dropping the end nothing re-checked the NEW last boundary: with ``on_failure: collect`` and an End with no output, a last-finishing worker that FAILED left its partial text as the
"last node's answer", and the relay posted it to the channel and the webhook hold as the graph's result (on main the answer was ``None``). The relay now posts an End output found after a failed (or non-``done``) last
boundary, and otherwise nothing.
"""

from __future__ import annotations

import pytest

from primer.channel.session_relay import _lines_from_the_end, _parse_tail, derive_session_final_text
from tests.session.graph_turn_paths import FAIL, OK, SID, TurnLogs, fanout, one_worker, open_turn, records, run_graph
from tests.session.test_dispatch import fake_event_bus, fake_storage_provider, fake_workspace_io  # noqa: F401  (fixtures)


async def _relay(tmp_path, io, bus, sp, *, scripts, graph):
    await open_turn(sp, io)
    await run_graph(tmp_path, sp, io, bus, TurnLogs(), scripts=scripts, graph=graph)
    recs = records(io)
    tail = _parse_tail(_lines_from_the_end("\n".join(io.read_lines(SID)) + "\n"))
    return derive_session_final_text(recs), derive_session_final_text(tail)


@pytest.mark.asyncio
async def test_a_pass_through_end_whose_last_worker_failed_relays_nothing(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    """``on_failure: collect``, an End with no output, the last worker to finish fails: ``half`` (its partial text) was posted as the result."""
    whole, tail = await _relay(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider, scripts=[OK, FAIL], graph=fanout("collect", end_template=""))

    assert whole is None and tail is None


@pytest.mark.asyncio
async def test_a_pass_through_end_whose_last_worker_succeeded_relays_its_answer(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    whole, tail = await _relay(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider, scripts=[FAIL, OK], graph=fanout("collect", end_template=""))

    assert whole == tail == "worker done"


@pytest.mark.asyncio
async def test_an_end_output_is_relayed_even_when_a_worker_failed(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    """The End node rendered a result (it names what it needs of its workers): that is the graph's output, whichever worker finished last."""
    whole, tail = await _relay(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider, scripts=[OK, FAIL], graph=fanout("collect", end_template="result: {{ nodes.agg.text }}"))

    assert whole == tail and whole is not None and whole.startswith("result: ")


@pytest.mark.asyncio
async def test_a_one_worker_pass_through_relays_the_workers_answer(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    """The pin of the end drop with the real writers: without it the window between the worker's ``done`` and the graph's end holds no text and the answer would be ``None``."""
    whole, tail = await _relay(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider, scripts=[OK], graph=one_worker(end_template=""))

    assert whole == tail == "worker done"


@pytest.mark.asyncio
async def test_a_failed_one_worker_graph_relays_nothing(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    whole, tail = await _relay(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider, scripts=[FAIL], graph=one_worker(end_template=""))

    assert whole is None and tail is None
