"""Memory models (agent memory Task 1): the defaults and bounds are contracts that later tasks rely on."""

import pytest
from pydantic import ValidationError

from primer.model.memory import AgentMemory, MemoryAdd, MemoryCredit, MemoryJob, MemoryPatch, ReflectionOutput


def test_agent_memory_defaults_match_spec() -> None:
    m = AgentMemory(id="agent-1")
    assert (m.enabled, m.promotion, m.teachers, m.idle_minutes, m.auto_inject) == (False, "auto", "owner", 10, False)
    assert (m.inject_top_k, m.inject_min_score, m.max_index_entries, m.max_active) == (2, None, 40, 200)
    assert (m.archive_after_days, m.daily_reflections, m.search) == (60, 50, None)
    assert (m.capture_topics, m.do_not_capture, m.topics_edited, m.topics_suggestion) == ([], [], False, None)
    assert (m.reflection_prompt, m.reflections_day, m.reflections_count, m.last_curated_at) == (None, None, 0, None)


@pytest.mark.parametrize(("field", "low", "high"), [
    ("idle_minutes", 1, 1440), ("inject_top_k", 1, 3), ("max_index_entries", 1, 200), ("max_active", 1, 2000),
    ("archive_after_days", 1, 3650), ("daily_reflections", 1, 1000),
])
def test_agent_memory_bounds(field, low, high) -> None:
    AgentMemory(id="a", **{field: low})
    AgentMemory(id="a", **{field: high})
    for bad in (low - 1, high + 1):
        with pytest.raises(ValidationError):
            AgentMemory(id="a", **{field: bad})


def test_the_id_is_required_and_literals_are_closed() -> None:
    with pytest.raises(Exception):
        AgentMemory()
    with pytest.raises(ValidationError):
        AgentMemory(id="a", promotion="never")
    with pytest.raises(ValidationError):
        AgentMemory(id="a", teachers="nobody")


def _add(name: str = "n") -> dict:
    return {"op": "add", "name": name, "description": "d", "steps": ["s"], "pitfalls": [], "example": None, "topics": ["t"]}


def test_reflection_output_takes_the_three_delta_kinds() -> None:
    out = ReflectionOutput.model_validate({
        "run_succeeded": True,
        "deltas": [_add(), {"op": "patch", "name": "n", "edits": [{"action": "remove", "section": "steps", "item_id": "s1", "text": None}]},
                   {"op": "credit", "name": "n", "verdict": "helpful", "reason": "worked"}],
    })
    assert [type(d) for d in out.deltas] == [MemoryAdd, MemoryPatch, MemoryCredit]


def test_reflection_output_refuses_more_than_three_deltas() -> None:
    with pytest.raises(ValidationError):
        ReflectionOutput.model_validate({"run_succeeded": True, "deltas": [_add(f"n{i}") for i in range(4)]})


def test_an_unknown_delta_op_is_refused() -> None:
    with pytest.raises(ValidationError):
        ReflectionOutput.model_validate({"run_succeeded": True, "deltas": [{"op": "erase", "name": "n"}]})


def test_memory_job_kinds_and_defaults() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    job = MemoryJob(kind="reflect", agent_id="a", created_at=now, expires_at=now + timedelta(days=7))
    assert (job.session_id, job.reflected_turn, job.target_turn) == (None, 0, 0) and job.id
    with pytest.raises(ValidationError):
        MemoryJob(kind="sweep", agent_id="a", created_at=now, expires_at=now)
