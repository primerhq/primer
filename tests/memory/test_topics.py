"""Capture topics (agent memory T8): derived from the agent's prompt and tools, kept apart from what an owner edited."""

from __future__ import annotations

from datetime import timedelta

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.memory.topics import PERSONAL_DATA_CLASSES, derive_topics, schedule_topics_on_change
from primer.model.agent import Agent
from primer.model.memory import AgentMemory, MemoryJob
from tests.memory.test_reflect import AGENT, NOW, FakeLLM, _deps, _setup, llm, sp  # noqa: F401 - fixtures of that module


async def _agent_with_prompt(sp, **settings):
    agent, mem, _job = await _setup(sp, **settings)
    agent = agent.model_copy(update={"system_prompt": ["You triage refunds."], "tools": ["web__search", "system__list_files"]})
    await sp.get_storage(Agent).update(agent)
    return agent, await sp.get_storage(AgentMemory).get(AGENT)


async def test_first_derivation_fills_topics(sp, llm) -> None:
    agent, mem = await _agent_with_prompt(sp)
    llm.answers = [{"capture_topics": ["refund triage", "ticket api"], "do_not_capture": ["order totals"]}]
    saved = await derive_topics(_deps(sp, llm), agent, mem)
    assert saved.capture_topics == ["refund triage", "ticket api"] and saved.topics_suggestion is None
    assert "order totals" in saved.do_not_capture
    assert (await sp.get_storage(AgentMemory).get(AGENT)).capture_topics == ["refund triage", "ticket api"]
    sent = llm.calls[0]["user_message"]
    assert "You triage refunds." in sent and "web__search" in sent and llm.calls[0]["model"] == "m1"


async def test_edited_topics_only_get_a_suggestion(sp, llm) -> None:
    agent, mem = await _agent_with_prompt(sp, capture_topics=["mine"], do_not_capture=["secret sauce"], topics_edited=True)
    llm.answers = [{"capture_topics": ["new area"], "do_not_capture": ["other"]}]
    saved = await derive_topics(_deps(sp, llm), agent, mem)
    assert saved.capture_topics == ["mine"] and saved.do_not_capture == ["secret sauce"]
    assert saved.topics_suggestion == ["new area"]
    stored = await sp.get_storage(AgentMemory).get(AGENT)
    assert (stored.capture_topics, stored.topics_suggestion, stored.topics_edited) == (["mine"], ["new area"], True)


async def test_prompt_change_schedules_topics_job(sp) -> None:
    agent, mem = await _agent_with_prompt(sp)
    engine = InMemoryClaimEngine(adapters={}, clock=lambda: NOW)
    unchanged = agent.model_copy(update={"description": "other words"})
    await schedule_topics_on_change(sp, engine, unchanged, agent, NOW)
    assert await sp.get_storage(MemoryJob).get(f"topics:{AGENT}") is None, "a description change is not a prompt change"
    for change in ({"system_prompt": ["New prompt."]}, {"tools": ["web__search"]}):
        await schedule_topics_on_change(sp, engine, agent.model_copy(update=change), agent, NOW)
        job = await sp.get_storage(MemoryJob).get(f"topics:{AGENT}")
        assert job is not None and job.kind == "topics"
        due = engine._leases[(ClaimKind.MEMORY, job.id)].next_attempt_at
        assert NOW < due <= NOW + timedelta(minutes=5), "a moment later, so the edit has been written"
        await sp.get_storage(MemoryJob).delete(job.id)
        await engine.delete_lease(ClaimKind.MEMORY, job.id)
    await sp.get_storage(AgentMemory).patch_if(AGENT, {"enabled": False}, where={"enabled": [True]})
    await schedule_topics_on_change(sp, engine, agent.model_copy(update={"system_prompt": ["x"]}), agent, NOW)
    assert await sp.get_storage(MemoryJob).get(f"topics:{AGENT}") is None, "memory off: nothing to derive"


async def test_do_not_capture_always_has_personal_data_classes(sp, llm) -> None:
    agent, mem = await _agent_with_prompt(sp)
    llm.answers = [{"capture_topics": ["a"], "do_not_capture": []}]
    saved = await derive_topics(_deps(sp, llm), agent, mem)
    assert PERSONAL_DATA_CLASSES and set(PERSONAL_DATA_CLASSES) <= set(saved.do_not_capture)
    llm.answers = [{"capture_topics": ["a"], "do_not_capture": [PERSONAL_DATA_CLASSES[0].upper(), "extra"]}]
    again = await derive_topics(_deps(sp, llm), agent, saved)
    assert [x.lower() for x in again.do_not_capture].count(PERSONAL_DATA_CLASSES[0]) == 1 and "extra" in again.do_not_capture, "no duplicates, case aside"


async def test_the_rest_and_tool_hooks_queue_the_job(sp) -> None:
    from primer.agent.agent_checks import agent_pre_checks

    agent, mem = await _agent_with_prompt(sp)
    _create, pre_update = agent_pre_checks(sp)
    await pre_update(agent.model_copy(update={"system_prompt": ["Changed."]}), agent, person=None)
    job = await sp.get_storage(MemoryJob).get(f"topics:{AGENT}")
    assert job is not None, "the system update_agent tool queues the row even with no claim engine"
