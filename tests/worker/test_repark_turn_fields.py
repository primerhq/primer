"""GR-10: the three agent re-park builders must carry the parked turn's own decisions.

``repark_parked_round`` carried both; ``repark_resumed_yield_outcome`` (phase 2 of an
approved yielding tool) and ``repark_continuation`` (a nested frame re-yielded) rebuild
``ParkedState`` without ``client_tools_attached`` and ``binding_epoch``, so the blob says
``client_tools_attached: false`` and ``binding_epoch: null``: the final resume rebuilds
the tool manager WITHOUT the client toolset the turn started with (a remaining
``client__*`` call of the round fails), and the binding-epoch void check in
``resume_engine_session`` is switched off. ``repark_continuation`` also stored the new
leaf without ``stamp_parked_at``, so a nested ``sleep`` or ``ask_user`` reports
``elapsed_seconds`` 0.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.continuation import Repark
from primer.worker.session_resume_coordinator import (
    repark_continuation,
    repark_parked_round,
    repark_resumed_yield_outcome,
)
from primer.worker.yield_runtime import ParkedState


def _parked() -> ParkedState:
    return ParkedState(
        yielded=Yielded(tool_name="_approval", event_key="tool_approval:s:tc-1", resume_metadata={}),
        llm_messages=[], turn_no=2, started_at=datetime.now(timezone.utc), tool_call_id="tc-1",
        client_tools_attached=True, binding_epoch=4,
    )


def _phase2() -> YieldToWorker:
    return YieldToWorker(Yielded(tool_name="sleep", event_key="timer:s:tc-1", timeout=30, resume_metadata={}), tool_call_id="tc-1")


def test_all_repark_builders_carry_the_parked_turn_fields():
    """GR-10: every agent re-park keeps the parked turn's own decisions: the
    client toolset flag and the binding epoch (the final resume rebuilds the
    tool manager with them and the binding-epoch void check stays armed), and
    ``repark_continuation`` stamps its new leaf so the resume hook can compute
    how long the call waited."""
    session = SimpleNamespace(id="s", turn_no=2)
    kept = repark_parked_round(None, session, _parked(), _phase2()).park.parked_state
    assert kept["client_tools_attached"] is True and kept["binding_epoch"] == 4

    phase2 = repark_resumed_yield_outcome(None, session, _parked(), _phase2()).park.parked_state
    nested = repark_continuation(None, session, _parked(), Repark(frames=[], leaf=_phase2().yielded, tool_call_id="tc-1")).park.parked_state
    for blob in (phase2, nested):
        assert blob["client_tools_attached"] is True
        assert blob["binding_epoch"] == 4
    # the new leaf carries parked_at_iso, as every park writes it
    assert nested["yielded"]["resume_metadata"].get("parked_at_iso")
