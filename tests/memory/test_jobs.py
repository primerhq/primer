"""The memory job queue and its gates (agent memory T6)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import primer.memory.jobs as jobs
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, Lease
from primer.memory.jobs import (
    MIN_TURNS,
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
        completed_turn_no=turns - 1, parent_session_id=parent, status=SessionStatus.RUNNING,
    )


async def _agent(sp, agent_id="a1", *, enabled=True, **settings) -> None:
    await sp.get_storage(Agent).create(Agent(id=agent_id, description="d", model=AgentModel(profile_id="p")))
    await sp.get_storage(AgentMemory).create(AgentMemory(id=agent_id, enabled=enabled, **settings))


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type, *, payload=None, **_kw) -> None:
        self.events.append((event_type, payload or {}))


def _deps(sp, engine, *, now=NOW, load_messages=None) -> MemoryDeps:
    return MemoryDeps(
        sp=sp, provider_registry=None, semantic_search_registry=None, engine=engine, recorder=_Recorder(), store=None,
        now=lambda: now, load_messages=load_messages,
    )


async def _lease(engine, job_id: str, *, attempts: int = 0) -> Lease:
    lease, = await engine.claim_due("w1", max_count=1)
    assert lease.entity_id == job_id
    return replace(lease, attempt_count=attempts)


def _due(engine, job_id) -> datetime:
    return engine._leases[(ClaimKind.MEMORY, job_id)].next_attempt_at


@pytest.fixture
def engine() -> InMemoryClaimEngine:
    return InMemoryClaimEngine(adapters={}, clock=lambda: NOW + timedelta(minutes=10))


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


async def test_the_first_turn_is_turn_zero_and_counts_as_one(fake_storage_provider, engine):
    """``completed_turn_no`` is 0-based: a session that finished turns 0 and 1 has two turns, enough to reflect on (a 1-based reading dropped it)."""
    await _agent(fake_storage_provider)
    first = _session(turns=1)
    assert first.completed_turn_no == 0
    await on_turn_completed(fake_storage_provider, engine, first, NOW)
    second = _session(turns=2)
    assert second.completed_turn_no == 1
    await on_turn_completed(fake_storage_provider, engine, second, NOW)
    job = await fake_storage_provider.get_storage(MemoryJob).get("reflect:a1:s1")
    assert job.target_turn - job.reflected_turn >= MIN_TURNS


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
    deps = _deps(fake_storage_provider, engine)
    out = await run_memory_job(deps, await _lease(engine, job_id))
    assert out.drop_lease is False and out.requeue_after == datetime(2026, 11, 2, tzinfo=UTC) - NOW
    assert ran == []
    assert [(e[0], e[1]["reason"], e[1]["retrying"]) for e in deps.recorder.events] == [("memory.reflection.skipped", "budget", True)]


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
    deps = _deps(fake_storage_provider, engine)
    out = await run_memory_job(deps, await _lease(engine, job_id))
    assert out.drop_lease is True and ran == []
    assert [e[1]["reason"] for e in deps.recorder.events] == ["expired"]


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
    out = await run_memory_job(_deps(fake_storage_provider, engine), await _lease(engine, job_id))
    assert out.drop_lease is True and ran == []


async def test_trivial_session_skipped_without_model_call(fake_storage_provider, engine, ran):
    await _agent(fake_storage_provider)
    await fake_storage_provider.get_storage(WorkspaceSession).create(_session(turns=1))  # a live session keeps its row
    # fewer than two turns since the last reflection
    await on_turn_completed(fake_storage_provider, engine, _session(turns=1), NOW)
    deps = _deps(fake_storage_provider, engine)
    lease = await _lease(engine, "reflect:a1:s1")
    out = await run_memory_job(deps, lease)
    await engine.release(lease, outcome=out)
    job = await fake_storage_provider.get_storage(MemoryJob).get("reflect:a1:s1")
    assert out.drop_lease is True and job.reflected_turn == 1 and ran == []
    assert [(e[1]["reason"], "fewer than" in e[1]["detail"]) for e in deps.recorder.events] == [("too_small", True)]

    # The job requests three turns but the log only contains one. Missing
    # evidence is not a trivial completed range and must not advance progress.
    async def only_chat(job):
        return [Message(role="user", parts=[TextPart(text="hi")]), Message(role="assistant", parts=[TextPart(text="hello")])]

    job_id = await _queued(fake_storage_provider, engine)
    deps = _deps(fake_storage_provider, engine, load_messages=only_chat)
    out = await run_memory_job(deps, await _lease(engine, job_id))
    job = await fake_storage_provider.get_storage(MemoryJob).get(job_id)
    assert out.drop_lease is True and job.reflected_turn == 1 and ran == []
    assert deps.recorder.events[0][1]["reason"] == "input_incomplete"


async def test_a_substantial_session_reaches_the_reflect_runner(fake_storage_provider, engine, ran):
    await _agent(fake_storage_provider)

    async def worked(job):
        return [
            Message(role="user", parts=[TextPart(text="do it")]),
            Message(role="assistant", parts=[ToolCallPart(id="c1", name="web__fetch", arguments={})]),
            Message(role="user", parts=[TextPart(text="again")]),
            Message(role="assistant", parts=[TextPart(text="done again")]),
            Message(role="user", parts=[TextPart(text="third request")]),
        ]

    job_id = await _queued(fake_storage_provider, engine)
    await run_memory_job(_deps(fake_storage_provider, engine, load_messages=worked), await _lease(engine, job_id))
    assert ran == ["reflect"]
    settings = await fake_storage_provider.get_storage(AgentMemory).get("a1")
    assert settings.reflections_count == 1, "the budget is spent as the model call is about to happen"


@pytest.mark.parametrize("attempts, retried", [(0, True), (1, True), (2, False)])
async def test_an_unreachable_model_is_retried_twice_then_skipped(fake_storage_provider, engine, monkeypatch, attempts, retried):
    from primer.memory.reflect import ReflectionTransient

    await _agent(fake_storage_provider)

    async def worked(job):
        return [Message(role="user", parts=[TextPart(text="a")]), Message(role="assistant", parts=[TextPart(text="x")]),
                Message(role="user", parts=[TextPart(text="b")]), Message(role="assistant", parts=[TextPart(text="y")]),
                Message(role="user", parts=[TextPart(text="c")])]

    async def down(*args, **kwargs):
        raise ReflectionTransient("connection refused")

    monkeypatch.setattr(jobs, "run_reflect", down)
    deps = _deps(fake_storage_provider, engine, load_messages=worked)
    job_id = await _queued(fake_storage_provider, engine)
    lease = await _lease(engine, job_id, attempts=attempts)
    out = await run_memory_job(deps, lease)
    if retried:
        assert out.success is False and not out.drop_lease and "refused" in out.last_error
    else:
        assert out.drop_lease
    assert [e[0] for e in deps.recorder.events] == ["memory.reflection.skipped"], "a retry says so too, so the activity feed is never quiet"
    assert deps.recorder.events[0][1]["reason"] == "llm_unavailable" and deps.recorder.events[0][1]["retrying"] is retried


async def test_schedule_job_makes_a_curate_or_topics_job_with_its_lease(fake_storage_provider, engine, ran):
    await _agent(fake_storage_provider)
    later = NOW + timedelta(hours=24)
    await schedule_job(fake_storage_provider, engine, kind="curate", agent_id="a1", next_attempt_at=later)
    await schedule_job(fake_storage_provider, engine, kind="topics", agent_id="a1", next_attempt_at=NOW)
    assert _due(engine, "curate:a1") == later and _due(engine, "topics:a1") == NOW
    await run_memory_job(_deps(fake_storage_provider, engine), await _lease(engine, "topics:a1"))
    assert ran == ["topics"]


async def test_a_topics_job_derives_the_topics_and_leaves_no_row(fake_storage_provider, engine, monkeypatch):
    import primer.memory.topics as topics

    derived = []

    async def derive(deps, agent, settings):
        derived.append(agent.id)
        return settings

    monkeypatch.setattr(topics, "derive_topics", derive)
    await _agent(fake_storage_provider)
    await schedule_job(fake_storage_provider, engine, kind="topics", agent_id="a1", next_attempt_at=NOW)
    out = await run_memory_job(_deps(fake_storage_provider, engine), await _lease(engine, "topics:a1"))
    assert out.drop_lease and derived == ["a1"]
    assert await fake_storage_provider.get_storage(MemoryJob).get("topics:a1") is None


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


async def test_a_job_that_raises_reports_it_instead_of_going_quiet(fake_storage_provider, engine):
    await _agent(fake_storage_provider)
    job_id = await _queued(fake_storage_provider, engine)
    deps = _deps(fake_storage_provider, engine)
    await jobs.report_job_error(deps, await _lease(engine, job_id), KeyError("boom"))
    [(event, payload)] = deps.recorder.events
    assert event == "memory.reflection.skipped" and payload["reason"] == "error" and payload["detail"].startswith("KeyError")


# --- 2.8: the log is read and parsed once per job -------------------------------------------------------------------------------------------------


async def test_the_gate_and_the_reflection_share_one_load_of_the_log(fake_storage_provider, engine, monkeypatch):
    await _agent(fake_storage_provider)
    loads = []

    async def worked(job):
        loads.append(job.id)
        return [
            Message(role="user", parts=[TextPart(text="do it")]),
            Message(role="assistant", parts=[ToolCallPart(id="c1", name="web__fetch", arguments={})]),
            Message(role="user", parts=[TextPart(text="again")]),
            Message(role="assistant", parts=[TextPart(text="done again")]),
            Message(role="user", parts=[TextPart(text="third request")]),
        ]

    seen = []

    async def reflect_reads_the_log(deps, job, agent, settings):
        seen.append(await deps.load_messages(job))

    monkeypatch.setattr(jobs, "run_reflect", reflect_reads_the_log)
    job_id = await _queued(fake_storage_provider, engine)
    await run_memory_job(_deps(fake_storage_provider, engine, load_messages=worked), await _lease(engine, job_id))
    assert len(loads) == 1 and len(seen) == 1 and len(seen[0]) == 5


# --- 2.9: finished reflect jobs do not accumulate -------------------------------------------------------------------------------------------------


async def _finished(sp, engine, *, status=SessionStatus.RUNNING, session_row=True) -> str:
    await _agent(sp)
    if session_row:
        await sp.get_storage(WorkspaceSession).create(_session(turns=1).model_copy(update={"status": status}))
    await on_turn_completed(sp, engine, _session(turns=1), NOW)
    lease = await _lease(engine, "reflect:a1:s1")
    out = await run_memory_job(_deps(sp, engine), lease)  # one turn: too small, retired
    await engine.release(lease, outcome=out)
    return "reflect:a1:s1"


async def test_a_retired_reflect_of_a_deleted_session_is_deleted(fake_storage_provider, engine, ran):
    job_id = await _finished(fake_storage_provider, engine, session_row=False)
    assert await fake_storage_provider.get_storage(MemoryJob).get(job_id) is None


async def test_a_retired_reflect_of_a_live_session_keeps_its_watermark(fake_storage_provider, engine, ran):
    job_id = await _finished(fake_storage_provider, engine)
    job = await fake_storage_provider.get_storage(MemoryJob).get(job_id)
    assert job is not None and job.reflected_turn == 1 and job.requested_due_at is None


async def test_an_ended_session_keeps_its_watermark_for_a_reopen(fake_storage_provider, engine, monkeypatch):
    """ENDED is not terminal: a person's message reopens the session and its next turns must not re-reflect turns already learned from."""
    from primer.memory.scheduling import patch_job_progress

    sp, ranges = fake_storage_provider, []

    async def fake_reflect(deps, job, agent, settings):
        ranges.append((job.reflected_turn + 1, job.target_turn))
        await patch_job_progress(deps, job, {"reflected_turn": job.target_turn})

    monkeypatch.setattr(jobs, "run_reflect", fake_reflect)
    await _agent(sp)
    ended = _session(turns=3).model_copy(update={"status": SessionStatus.ENDED})
    await sp.get_storage(WorkspaceSession).create(ended)
    await on_session_ended(sp, engine, ended, NOW)
    lease = await _lease(engine, "reflect:a1:s1")
    out = await run_memory_job(_deps(sp, engine), lease)
    await engine.release(lease, outcome=out)
    assert ranges == [(1, 3)]
    reopened = _session(turns=5)
    await sp.get_storage(WorkspaceSession).update(reopened)
    await on_turn_completed(sp, engine, reopened, NOW)
    out = await run_memory_job(_deps(sp, engine), await _lease(engine, "reflect:a1:s1"))
    assert ranges == [(1, 3), (4, 5)]


async def test_the_sweep_keeps_ended_sessions_and_only_counts_deletions(fake_storage_provider, engine, ran, monkeypatch):
    sp = fake_storage_provider
    monkeypatch.setattr(jobs, "SWEEP_LIMIT", 2)
    await _agent(sp)
    store, sessions = sp.get_storage(MemoryJob), sp.get_storage(WorkspaceSession)
    # more kept rows (ended and live sessions) than the limit, sorted before the rows of the deleted sessions
    for sid in ("a1", "a2", "a3"):
        status = SessionStatus.ENDED if sid == "a1" else SessionStatus.RUNNING
        await sessions.create(_session(sid=sid).model_copy(update={"status": status}))
    for sid in ("a1", "a2", "a3", "z1", "z2"):
        await on_turn_completed(sp, engine, _session(sid=sid), NOW)
        await store.update((await store.get(f"reflect:a1:{sid}")).model_copy(update={"requested_due_at": None}))
    # A legacy row (no occurrence) of a session that still exists keeps staying (the pre-intent claim path may hold it
    # pending); legacy rows of gone sessions are swept, but the cap stops this pass at the two gone-session rows above.
    legacy = (await store.get("reflect:a1:z1")).model_copy(
        update={"id": "reflect:a1:z9", "session_id": "a2", "occurrence_id": None, "incarnation_id": None, "requested_due_at": None})
    await store.create(legacy)
    assert await jobs.sweep_retired_reflects(_deps(sp, engine), "a1") == 2
    assert [await store.get(f"reflect:a1:{sid}") is None for sid in ("a1", "a2", "a3", "z1", "z2", "z9")] == [False, False, False, True, True, False]


async def test_a_legacy_reflect_of_a_gone_session_is_swept(fake_storage_provider, engine):
    # A legacy (no-occurrence) reflect row is swept once its session is gone (review 2026-11-23 X2.2); rows of
    # sessions that still exist keep staying (the pre-intent claim path may hold them pending).
    sp = fake_storage_provider
    await _agent(sp)
    await sp.get_storage(WorkspaceSession).create(_session(sid="live"))
    store = sp.get_storage(MemoryJob)
    for sid in ("gone1", "gone2", "live"):
        await store.create(MemoryJob(
            id=f"reflect:a1:{sid}", kind="reflect", agent_id="a1", session_id=sid,
            reflected_turn=1, target_turn=2, created_at=NOW, expires_at=NOW,
        ))
    assert await jobs.sweep_retired_reflects(_deps(sp, engine), "a1") == 2
    assert await store.get("reflect:a1:gone1") is None
    assert await store.get("reflect:a1:gone2") is None
    assert await store.get("reflect:a1:live") is not None


async def test_the_session_is_captured_before_the_log_is_read(fake_storage_provider, engine, monkeypatch):
    """The reflection must judge the log against the session as it was before the gate's read, not as it is by the time the reflection starts."""
    sp = fake_storage_provider
    await _agent(sp)
    store = sp.get_storage(WorkspaceSession)
    await store.create(_session(turns=3))
    seen = []

    async def worked(job):
        row = await store.get("s1")  # the session changes while the log is being read
        await store.update(row.model_copy(update={"binding_epoch": row.binding_epoch + 1}))
        return [Message(role="user", parts=[TextPart(text="do it")]), Message(role="assistant", parts=[ToolCallPart(id="c1", name="x__y", arguments={})]),
                Message(role="user", parts=[TextPart(text="again")]), Message(role="assistant", parts=[TextPart(text="ok")]),
                Message(role="user", parts=[TextPart(text="third")])]

    async def reflect(deps, job, agent, settings):
        seen.append(deps.session.binding_epoch)

    monkeypatch.setattr(jobs, "run_reflect", reflect)
    job_id = await _queued(sp, engine)
    await run_memory_job(_deps(sp, engine, load_messages=worked), await _lease(engine, job_id))
    assert seen == [0], "the captured session is the one from before the read"


async def test_the_rearm_after_a_give_up_is_too_small_and_spends_no_budget(fake_storage_provider, engine, monkeypatch):
    """A range the reflection gave up on moves the watermark to the target, so the next turn's re-arm is a one-turn range: too_small, no model, no slot."""
    from primer.memory.scheduling import patch_job_progress

    sp = fake_storage_provider
    await _agent(sp)
    await sp.get_storage(WorkspaceSession).create(_session(turns=3))
    runs = []

    async def gives_up(deps, job, agent, settings):
        runs.append((job.reflected_turn, job.target_turn))
        await patch_job_progress(deps, job, {"reflected_turn": job.target_turn})  # what reflect does on an input_too_large_skipped

    monkeypatch.setattr(jobs, "run_reflect", gives_up)

    async def worked(job):
        out = []
        for i in range(job.target_turn):
            out += [Message(role="user", parts=[TextPart(text=f"ask {i}")]), Message(role="assistant", parts=[ToolCallPart(id=f"c{i}", name="x__y", arguments={})])]
        return out

    job_id = await _queued(sp, engine)
    deps = _deps(sp, engine, load_messages=worked)
    lease = await _lease(engine, job_id)
    await engine.release(lease, outcome=await run_memory_job(deps, lease))
    used = (await sp.get_storage(AgentMemory).get("a1")).reflections_count
    await sp.get_storage(WorkspaceSession).update(_session(turns=4))
    await on_turn_completed(sp, engine, _session(turns=4), NOW)
    deps = _deps(sp, engine, load_messages=worked)
    await run_memory_job(deps, await _lease(engine, job_id))
    assert runs == [(0, 3)] and (await sp.get_storage(AgentMemory).get("a1")).reflections_count == used
    assert [e[1]["reason"] for e in deps.recorder.events] == ["too_small"]
