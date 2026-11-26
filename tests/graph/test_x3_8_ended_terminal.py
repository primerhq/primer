"""X3.8: an ENDED persisted state counts as a finished run only when it
carries a real outcome reason. An ENDED state with NO reason (a torn terminal
write) must not be exposed as a clean stop -- the resume answers
``resume_state_incomplete`` instead of handing the status mapper a ``None``
outcome it would read as a successful stop. An ENDED state with a real reason
still exposes the outcome exactly as before.
"""

from pathlib import Path

import pytest

from primer.graph.base import _GraphErrorEvent
from primer.model.workspace_session import SessionStatus
from tests.graph.test_review_6_2_reclaim_resume_shapes import _StubStateRepo
from tests.graph.test_review_k2_round1_resume import _build, _linear


def _ended_state(ended_reason: str | None) -> dict:
    return {
        "run_id": "turn-1",
        "iteration": 0,
        "status": SessionStatus.ENDED.value,
        "ended_reason": ended_reason,
        "ended_detail": None,
        "node_states": {},
        "ready_set": [],
        "initial_input": "x",
        "pending_ended_reason": None,
        "pending_ended_detail": None,
        "max_iter_routed": False,
    }


@pytest.mark.asyncio
async def test_an_ended_state_with_no_reason_answers_resume_state_incomplete(tmp_path: Path) -> None:
    repo = _StubStateRepo(tmp_path / ".state", "ws")
    await repo.initialize()
    ex = await _build(repo, {}, "gs", _linear(0), owns=False)

    async def _load_persisted_state():
        return _ended_state(None)

    ex._load_persisted_state = _load_persisted_state

    codes: list[str] = []
    async for ev in ex.invoke("x"):
        if isinstance(ev, _GraphErrorEvent):
            codes.append(ev.code)
            break
    assert codes == ["resume_state_incomplete"]


@pytest.mark.asyncio
async def test_an_ended_state_with_a_real_reason_still_exposes_the_outcome(tmp_path: Path) -> None:
    repo = _StubStateRepo(tmp_path / ".state", "ws")
    await repo.initialize()
    ex = await _build(repo, {}, "gs", _linear(0), owns=False)

    async def _load_persisted_state():
        return _ended_state("completed")

    ex._load_persisted_state = _load_persisted_state

    events = [ev async for ev in ex.invoke("x")]
    assert not any(isinstance(ev, _GraphErrorEvent) for ev in events)
    assert ex._last_ended_reason == "completed"
