"""Reflection-only strict log reads and real job disposition over SQLite."""
import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.memory.jobs import run_memory_job
from primer.memory.reflect import reflect, session_message_loader
from primer.memory.reflection_input import InputRefused
from primer.model.memory import MemoryJob
from primer.model.workspace_session import WorkspaceSession
from tests.memory.test_reflect import (
    NOW, FakeLLM, _deps, _entries, _setup, _transcript, sp,  # noqa: F401 (fixture)
)


@pytest.mark.parametrize("tail", [b'{"role":"assistant",', b'\xff', b'[]', b'{"role":"invalid","parts":[]}'])
@pytest.mark.parametrize("through_job", [False, True])
async def test_torn_log_after_enough_user_starts_preserves_progress(sp, tail, through_job):
    agent, settings, job = await _setup(sp)
    raw = ("\n".join(m.model_dump_json() for m in _transcript()) + "\n").encode() + tail
    async def read_log(workspace, slot):
        assert (workspace, slot) == ("w1", "sessions/s1")
        return raw
    llm = FakeLLM()
    deps = _deps(sp, llm)
    deps.load_messages = session_message_loader(sp, read_log)
    if through_job:
        engine = InMemoryClaimEngine(adapters={}, clock=lambda: NOW)
        await engine.upsert(ClaimKind.MEMORY, job.id)
        lease, = await engine.claim_due("offline", max_count=1)
        deps.engine = engine
        outcome = await run_memory_job(deps, lease)
        assert outcome.drop_lease
    else:
        report = await reflect(deps, job, agent, settings)
        assert report.skipped_reason == "input_incomplete"
    assert llm.calls == [] and await _entries(deps) == []
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 0
    events = await sp.get_event_store().read_after(0, event_type_prefix="memory.reflection.skipped")
    assert events[-1].payload["reason"] == "input_incomplete"


async def test_loader_retains_original_compacted_evidence_and_honors_rewinds(sp):
    from tests._support.segmented_log import full_run
    _, _, job = await _setup(sp)
    raw = full_run()
    async def read_log(*args):
        return raw
    messages = await session_message_loader(sp, read_log)(job)
    text = "\n".join(getattr(p, "text", "") for m in messages for p in m.parts)
    assert "first question" in text and "first answer" in text and "sixth answer" in text
    assert "fourth question" not in text and "SUMMARY of the first turn" not in text
    assert raw == full_run()


async def test_loader_rejects_identity_changed_during_actual_read(sp):
    _, _, job = await _setup(sp)
    async def read_log(*args):
        assert await sp.get_storage(WorkspaceSession).patch_if("s1", {"workspace_id": "other"}, where={"workspace_id": ["w1"]})
        return ("\n".join(m.model_dump_json() for m in _transcript()) + "\n").encode()
    with pytest.raises(InputRefused, match="input_superseded"):
        await session_message_loader(sp, read_log)(job)
