"""The memory job queue and its gates (agent memory T6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import primer.memory.jobs as jobs
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, Lease
from primer.memory.jobs import (
    MemoryDeps,
    budget_allows,
    consume_budget,
    curate_job_id,
    on_session_ended,
    on_turn_completed,
    reflect_job_id,
    run_memory_job,
    schedule_job,
    topics_job_id,
)
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Message, TextPart, ToolCallPart
from primer.model.memory import AgentMemory, MemoryJob
from primer.model.workspace_session import AgentSessionBinding, GraphSessionBinding, SessionStatus, WorkspaceSession

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _session(*, agent_id="a1", turns=1, binding=None, parent=None, sid="s1") -> WorkspaceSession:
    return WorkspaceSession(
        id=sid, workspace_id="w1", binding=binding or AgentSessionBinding(agent_id=agent_id), created_at=NOW, turn_no=turns,
        completed_turn_no=turns, parent_session_id=parent, status=SessionStatus.RUNNING,
    )


async def _agent(sp, agent_id="a1", *, enabled=True, **settings) -> None:
    await sp.get_storage(Agent).create(Agent(id=agent_id, description="d", model=AgentModel(profile_id="p")))
    await sp.get_storage(AgentMemory).create(AgentMemory(id=agent_id, enabled=enabled, **settings))


def _deps(sp, engine, *, now=NOW, load_messages=None) -> MemoryDeps:
    return MemoryDeps(
        sp=sp, provider_registry=None, semantic_search_registry=None, engine=engine, recorder=None, store=None,
        now=lambda: now, load_messages=load_messages,
    )


def _lease(job_id: str) -> Lease:
    return Lease(ClaimKind.MEMORY, job_id, "w1", NOW, NOW + timedelta(minutes=1), 0, None)


def _due(engine, job_id) -> datetime:
    return engine._leases[(ClaimKind.MEMORY, job_id)].next_attempt_at


@pytest.fixture
def engine() -> InMemoryClaimEngine:
    return InMemoryClaimEngine(adapters={}, clock=lambda: NOW)


@pytest.fixture
def ran(monkeypatch) -> list[str]:
    """Replaces the three runners (Tasks 7 to 9) with recorders: a gate that holds never reaches them."""
    calls: list[str] = []

    def runner(kind):
        async def run(*args, **kwargs):
            calls.append(kind)
        return run

    for kind in ("reflect", "curate", "topics"):
        monkeypatch.setattr(jobs, f"run_{kind}", runner(kind))
    return calls


def test_job_ids():
    assert reflect_job_id("a1", "s1") == "reflect:a1:s1"
    assert curate_job_id("a1") == "curate:a1"
    assert topics_job_id("a1") == "topics:a1"


async def test_turn_completed_enqueues_debounced_reflect(fake_storage_provider, engine):
    await _agent(fake_storage_provider)
    await on_turn_completed(fake_storage_provider, engine, _session(turns=1), NOW)
    second = NOW + timedelta(minutes=3)
    await on_turn_completed(fake_storage_provider, engine, _session(turns=2), second)
    (job,) = (await fake_storage_provider.get_storage(MemoryJob).list(_page())).items
    assert (job.id, job.kind, job.agent_id, job.session_id, job.target_turn) == ("reflect:a1:s1", "reflect", "a1", "s1", 2)
    assert _due(engine, job.id) == second + timedelta(minutes=10), "the later turn re-arms the lease: the debounce"


async def test_session_ended_makes_job_due_now(fake_storage_provider, engine):
    await _agent(fake_storage_provider)
    await on_turn_completed(fake_storage_provider, engine, _session(turns=2), NOW)
    ended = NOW + timedelta(minutes=1)
    await on_session_ended(fake_storage_provider, engine, _session(turns=2), ended)
    assert _due(engine, "reflect:a1:s1") == ended


@pytest.mark.parametrize("settings", [{"enabled": False}, None])
async def test_no_job_when_memory_disabled(fake_storage_provider, engine, settings):
    if settings is None:  # no AgentMemory row at all
        await fake_storage_provider.get_storage(Agent).create(Agent(id="a1", description="d", model=AgentModel(profile_id="p")))
    else:
        await _agent(fake_storage_provider, **settings)
    await on_turn_completed(fake_storage_provider, engine, _session(), NOW)
    await on_session_ended(fake_storage_provider, engine, _session(), NOW)
    assert (await fake_storage_provider.get_storage(MemoryJob).list(_page())).items == []
    assert not await engine.has_lease(ClaimKind.MEMORY, "reflect:a1:s1")


async def test_graph_and_subagent_sessions_enqueue_nothing(fake_storage_provider, engine):
    await _agent(fake_storage_provider)
    for session in (
        _session(binding=GraphSessionBinding(graph_id="g1")),
        _session(parent="s0"),
        WorkspaceSession(id="s9", workspace_id="w1", binding=AgentSessionBinding(agent_id="a1"), created_at=NOW, status=SessionStatus.RUNNING),  # no completed turn
    ):
        await on_turn_completed(fake_storage_provider, engine, session, NOW)
        await on_session_ended(fake_storage_provider, engine, session, NOW)
    assert (await fake_storage_provider.get_storage(MemoryJob).list(_page())).items == []


async def _queued(sp, engine, **job_fields) -> str:
    """A reflect job for session s1 as the hooks write it, then adjusted."""
    await on_turn_completed(sp, engine, _session(turns=3), NOW)
    if job_fields:
        store = sp.get_storage(MemoryJob)
        await store.update((await store.get("reflect:a1:s1")).model_copy(update=job_fields))
    return "reflect:a1:s1"


async def test_over_budget_requeues_to_next_utc_midnight(fake_storage_provider, engine, ran):
    await _agent(fake_storage_provider, daily_reflections=2, reflections_day=NOW.date(), reflections_count=2)
    job_id = await _queued(fake_storage_provider, engine)
    out = await run_memory_job(_deps(fake_storage_provider, engine), _lease(job_id))
    assert out.drop_lease is False and out.requeue_after == datetime(2026, 11, 2, tzinfo=UTC) - NOW
    assert ran == []


async def test_budget_is_day_scoped_in_utc(fake_storage_provider):
    await _agent(fake_storage_provider, daily_reflections=1, reflections_day=NOW.date() - timedelta(days=1), reflections_count=1)
    settings = await fake_storage_provider.get_storage(AgentMemory).get("a1")
    assert budget_allows(settings, NOW) is True, "yesterday's count does not count today"
    after = await consume_budget(fake_storage_provider, settings, NOW)
    assert (after.reflections_day, after.reflections_count) == (NOW.date(), 1)
    assert budget_allows(after, NOW) is False
    assert budget_allows(after, NOW + timedelta(hours=13)) is True, "the next UTC day"


async def test_expired_job_is_dropped(fake_storage_provider, engine, ran):
    await _agent(fake_storage_provider)
    job_id = await _queued(fake_storage_provider, engine, expires_at=NOW - timedelta(seconds=1))
    out = await run_memory_job(_deps(fake_storage_provider, engine), _lease(job_id))
    assert out.drop_lease is True and ran == []


@pytest.mark.parametrize("what", ["agent_deleted", "memory_disabled", "job_missing"])
async def test_agent_deleted_or_memory_disabled_drops_lease(fake_storage_provider, engine, ran, what):
    await _agent(fake_storage_provider)
    job_id = await _queued(fake_storage_provider, engine)
    if what == "agent_deleted":
        await fake_storage_provider.get_storage(Agent).delete("a1")
    elif what == "memory_disabled":
        store = fake_storage_provider.get_storage(AgentMemory)
        await store.update((await store.get("a1")).model_copy(update={"enabled": False}))
    else:
        await fake_storage_provider.get_storage(MemoryJob).delete(job_id)
    out = await run_memory_job(_deps(fake_storage_provider, engine), _lease(job_id))
    assert out.drop_lease is True and ran == []


async def test_trivial_session_skipped_without_model_call(fake_storage_provider, engine, ran):
    await _agent(fake_storage_provider)
    # fewer than two turns since the last reflection
    await on_turn_completed(fake_storage_provider, engine, _session(turns=1), NOW)
    out = await run_memory_job(_deps(fake_storage_provider, engine), _lease("reflect:a1:s1"))
    job = await fake_storage_provider.get_storage(MemoryJob).get("reflect:a1:s1")
    assert out.drop_lease is True and job.reflected_turn == 1 and ran == []

    # enough turns, but nothing after the first user message: no tool call, no further user message
    async def only_chat(job):
        return [Message(role="user", parts=[TextPart(text="hi")]), Message(role="assistant", parts=[TextPart(text="hello")])]

    job_id = await _queued(fake_storage_provider, engine)
    out = await run_memory_job(_deps(fake_storage_provider, engine, load_messages=only_chat), _lease(job_id))
    job = await fake_storage_provider.get_storage(MemoryJob).get(job_id)
    assert out.drop_lease is True and job.reflected_turn == 3 and ran == []


async def test_a_substantial_session_reaches_the_reflect_runner(fake_storage_provider, engine, ran):
    await _agent(fake_storage_provider)

    async def worked(job):
        return [
            Message(role="user", parts=[TextPart(text="do it")]),
            Message(role="assistant", parts=[ToolCallPart(id="c1", name="web__fetch", arguments={})]),
            Message(role="user", parts=[TextPart(text="again")]),
        ]

    job_id = await _queued(fake_storage_provider, engine)
    await run_memory_job(_deps(fake_storage_provider, engine, load_messages=worked), _lease(job_id))
    assert ran == ["reflect"]
    settings = await fake_storage_provider.get_storage(AgentMemory).get("a1")
    assert settings.reflections_count == 1, "the budget is spent as the model call is about to happen"


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type, *, payload=None, **_kw) -> None:
        self.events.append((event_type, payload or {}))


@pytest.mark.parametrize("attempts, retried", [(0, True), (1, True), (2, False)])
async def test_an_unreachable_model_is_retried_twice_then_skipped(fake_storage_provider, engine, monkeypatch, attempts, retried):
    from primer.memory.reflect import ReflectionTransient

    await _agent(fake_storage_provider)

    async def worked(job):
        return [Message(role="user", parts=[TextPart(text="a")]), Message(role="user", parts=[TextPart(text="b")])]

    async def down(*args, **kwargs):
        raise ReflectionTransient("connection refused")

    monkeypatch.setattr(jobs, "run_reflect", down)
    deps = _deps(fake_storage_provider, engine, load_messages=worked)
    deps.recorder = _Recorder()
    job_id = await _queued(fake_storage_provider, engine)
    lease = Lease(ClaimKind.MEMORY, job_id, "w1", NOW, NOW + timedelta(minutes=1), attempts, None)
    out = await run_memory_job(deps, lease)
    if retried:
        assert out.success is False and not out.drop_lease and "refused" in out.last_error and deps.recorder.events == []
    else:
        assert out.drop_lease and [e[0] for e in deps.recorder.events] == ["memory.reflection.skipped"]
        assert deps.recorder.events[0][1]["reason"] == "llm_unavailable"


async def test_schedule_job_makes_a_curate_or_topics_job_with_its_lease(fake_storage_provider, engine, ran):
    await _agent(fake_storage_provider)
    later = NOW + timedelta(hours=24)
    await schedule_job(fake_storage_provider, engine, kind="curate", agent_id="a1", next_attempt_at=later)
    await schedule_job(fake_storage_provider, engine, kind="topics", agent_id="a1", next_attempt_at=NOW)
    assert _due(engine, "curate:a1") == later and _due(engine, "topics:a1") == NOW
    await run_memory_job(_deps(fake_storage_provider, engine), _lease("topics:a1"))
    assert ran == ["topics"]


async def test_the_runners_are_stubs_until_their_tasks_land(fake_storage_provider, engine):
    await _agent(fake_storage_provider)
    await schedule_job(fake_storage_provider, engine, kind="topics", agent_id="a1", next_attempt_at=NOW)
    with pytest.raises(NotImplementedError):
        await run_memory_job(_deps(fake_storage_provider, engine), _lease("topics:a1"))


def _page():
    from primer.model.storage import OffsetPage

    return OffsetPage(offset=0, length=100)


class _Storage:
    """Just the ``patch_if`` of ``_mark_turn_completed``: answers the session as the write leaves it."""

    def __init__(self, written):
        self.written = written

    async def patch_if(self, *_a, **_k):
        return self.written


async def test_the_turn_completed_write_hands_the_session_to_the_queue(fake_storage_provider, engine):
    from types import SimpleNamespace

    from primer.session.dispatch import _mark_turn_completed

    await _agent(fake_storage_provider)
    deps = SimpleNamespace(claim_engine=engine, storage_provider=fake_storage_provider)
    await _mark_turn_completed(_Storage(_session(turns=2)), "s1", 2, deps)
    assert await engine.has_lease(ClaimKind.MEMORY, "reflect:a1:s1")
    # no deps, no engine, a rejected fence, or a failing hook: the turn is never failed by memory
    await _mark_turn_completed(_Storage(_session(turns=2)), "s1", 2)
    await _mark_turn_completed(_Storage(_session(turns=2)), "s1", 2, SimpleNamespace(claim_engine=None, storage_provider=fake_storage_provider))
    await _mark_turn_completed(_Storage(None), "s1", 2, deps)
    await _mark_turn_completed(_Storage(_session(turns=2)), "s1", 2, SimpleNamespace(claim_engine=engine, storage_provider=None))


async def test_an_ended_session_is_queued_due_now(fake_storage_provider, engine, monkeypatch):
    from types import SimpleNamespace

    from primer.session import dispatch

    class _Recorder:
        async def emit(self, *a, **k):
            return None

    monkeypatch.setattr(dispatch, "_event_recorder", lambda deps: _Recorder())
    await _agent(fake_storage_provider)
    deps = SimpleNamespace(claim_engine=engine, storage_provider=fake_storage_provider, event_bus=None)
    await dispatch._publish_terminal(deps, _session(turns=2), SessionStatus.ENDED, "done")
    assert await engine.has_lease(ClaimKind.MEMORY, "reflect:a1:s1")
