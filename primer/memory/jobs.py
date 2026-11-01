"""The memory job queue (agent memory, spec 6.1, 6.2 and 10): the hooks that enqueue, the budget, and the gates every job passes.

A job is a ``MemoryJob`` row (the claim entity) plus its lease on the ``memory`` claim kind. Reflection is debounced by the lease: every completed
turn re-arms ``next_attempt_at`` to ``idle_minutes`` later, and an ended session makes it due at once. The jobs themselves (reflect, curate, topics)
are the runners of Tasks 7, 8 and 9; until they land each is a stub that raises ``NotImplementedError`` behind the gates.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from pydantic_core import to_jsonable_python

from primer.int.claim import ClaimKind, Lease, ReleaseOutcome
from primer.model.agent import Agent
from primer.model.chat import Message, ToolCallPart
from primer.model.except_ import ConflictError
from primer.memory.reflect import ReflectionTransient
from primer.model.memory import AgentMemory, MemoryJob

logger = logging.getLogger(__name__)

#: A job nobody ran within this long is dropped, and a session that goes on re-arms it.
JOB_LIFETIME = timedelta(days=7)
#: A reflection needs at least this many turns since the last one.
MIN_TURNS = 2
#: A reflection whose model cannot be reached is tried this many times, then given up on.
MAX_ATTEMPTS = 3
#: The curator runs at most once in this long.
CURATE_EVERY = timedelta(hours=24)


@dataclass
class MemoryDeps:
    """What every job runner is handed; built once by the worker from its own registries.

    ``store`` is the ``MemoryStore`` of Task 3. ``load_messages`` reads a reflect job's session messages (the triviality gate and the reflection); without it
    that half of the gate does not apply and a reflection is skipped as having no transcript.
    """

    sp: Any
    provider_registry: Any
    semantic_search_registry: Any
    engine: Any
    recorder: Any
    store: Any
    now: Callable[[], datetime]
    load_messages: Callable[[MemoryJob], Awaitable[list[Message] | None]] | None = None


def reflect_job_id(agent_id: str, session_id: str) -> str:
    return f"reflect:{agent_id}:{session_id}"


def curate_job_id(agent_id: str) -> str:
    return f"curate:{agent_id}"


def topics_job_id(agent_id: str) -> str:
    return f"topics:{agent_id}"


# --- enqueueing --------------------------------------------------------------------------------------------------------------------------------


async def _memory_agent(sp: Any, session: Any) -> tuple[str, AgentMemory] | None:
    """The agent of a session that memory learns from, with its settings: an agent session of its own (not a graph's, not a sub-agent's) whose
    agent has memory on and which has completed a turn. ``completed_turn_no`` is 0-based (the first turn is 0); a job counts turns from 1."""
    binding = session.binding
    if getattr(binding, "kind", None) != "agent" or session.parent_session_id is not None or session.completed_turn_no is None:
        return None
    settings = await sp.get_storage(AgentMemory).get(binding.agent_id)
    if settings is None or not settings.enabled:
        return None
    return binding.agent_id, settings


async def _upsert_job(sp: Any, job_id: str, *, kind: str, agent_id: str, session_id: str | None, target_turn: int, now: datetime) -> None:
    """The job row: created, or its target turn raised and its lifetime renewed (never lowered: a late write of an older turn changes nothing)."""
    store = sp.get_storage(MemoryJob)
    expires = now + JOB_LIFETIME
    existing = await store.get(job_id)
    if existing is None:
        try:
            await store.create(MemoryJob(
                id=job_id, kind=kind, agent_id=agent_id, session_id=session_id, target_turn=target_turn, created_at=now, expires_at=expires,
            ))
            return
        except ConflictError:  # another worker enqueued it between the read and the write
            existing = await store.get(job_id)
            if existing is None:
                return
    patch = {"target_turn": max(existing.target_turn, target_turn), "expires_at": expires}
    await store.patch_if(job_id, to_jsonable_python(patch), where={"agent_id": [agent_id]})


async def _enqueue_reflect(sp: Any, engine: Any, session: Any, now: datetime, *, due: Callable[[AgentMemory], datetime]) -> None:
    found = await _memory_agent(sp, session)
    if found is None:
        return
    agent_id, settings = found
    job_id = reflect_job_id(agent_id, session.id)
    await _upsert_job(sp, job_id, kind="reflect", agent_id=agent_id, session_id=session.id, target_turn=session.completed_turn_no + 1, now=now)
    await engine.upsert(ClaimKind.MEMORY, job_id, next_attempt_at=due(settings))


async def on_turn_completed(sp: Any, engine: Any, session: Any, now: datetime) -> None:
    """A turn finished: queue the session's reflection for ``idle_minutes`` from now; a later turn pushes it back (the debounce)."""
    await _enqueue_reflect(sp, engine, session, now, due=lambda s: now + timedelta(minutes=s.idle_minutes))


async def on_session_ended(sp: Any, engine: Any, session: Any, now: datetime) -> None:
    """The session ended: its reflection is due now."""
    await _enqueue_reflect(sp, engine, session, now, due=lambda _s: now)


async def schedule_job(sp: Any, engine: Any, *, kind: str, agent_id: str, next_attempt_at: datetime) -> None:
    """Queue a ``curate`` or ``topics`` job of an agent, due at ``next_attempt_at``."""
    job_id = {"curate": curate_job_id, "topics": topics_job_id}[kind](agent_id)
    await _upsert_job(sp, job_id, kind=kind, agent_id=agent_id, session_id=None, target_turn=0, now=datetime.now(UTC))
    if engine is not None:   # a tool that runs where no engine is wired leaves the row for the daily curator run
        await engine.upsert(ClaimKind.MEMORY, job_id, next_attempt_at=next_attempt_at)


# --- the daily reflection budget ---------------------------------------------------------------------------------------------------------------


def _day(now: datetime) -> date:
    return now.astimezone(UTC).date()


def budget_allows(settings: AgentMemory, now: datetime) -> bool:
    """Whether the agent may start another reflection today (the day is a UTC day; yesterday's count is forgotten)."""
    used = settings.reflections_count if settings.reflections_day == _day(now) else 0
    return used < settings.daily_reflections


async def consume_budget(sp: Any, settings: AgentMemory, now: datetime) -> AgentMemory:
    """Count one reflection against today's budget; returns the settings as stored (a lost race re-reads and counts again, up to three times)."""
    store = sp.get_storage(AgentMemory)
    day = _day(now)
    for _ in range(3):
        used = settings.reflections_count if settings.reflections_day == day else 0
        where = {
            "reflections_count": [settings.reflections_count],
            "reflections_day": [settings.reflections_day.isoformat() if settings.reflections_day else None],
        }
        written = await store.patch_if(settings.id, {"reflections_day": day.isoformat(), "reflections_count": used + 1}, where=where)
        if written is not None:
            return written
        fresh = await store.get(settings.id)
        if fresh is None:
            return settings
        settings = fresh
    return settings


# --- the runners (Tasks 7 to 9) ----------------------------------------------------------------------------------------------------------------


async def run_reflect(deps: MemoryDeps, job: MemoryJob, agent: Agent, settings: AgentMemory) -> None:
    from primer.memory.reflect import reflect

    await reflect(deps, job, agent, settings)


async def run_curate(deps: MemoryDeps, job: MemoryJob, agent: Agent, settings: AgentMemory) -> ReleaseOutcome:
    """The daily curator pass; the job requeues itself a day on, whatever the pass did (a failed pass is tried again tomorrow)."""
    from primer.memory.curate import curate

    now = deps.now()
    last = settings.last_curated_at
    if last is not None and now - last < CURATE_EVERY:
        return ReleaseOutcome(success=True, requeue_after=CURATE_EVERY - (now - last))
    jobs = deps.sp.get_storage(MemoryJob)
    # A prompt edit made where no claim engine is wired left its topics job without a lease: it is taken up here, at the latest a day later.
    pending = await jobs.get(topics_job_id(agent.id))
    if pending is not None:
        await run_topics(deps, pending, agent, settings)
    try:
        await curate(deps, agent, settings, now)
    except Exception:  # noqa: BLE001 - tomorrow's pass tries again; the chain of daily jobs must not end here
        logger.warning("memory job %s: the curator failed", job.id, exc_info=True)
    await deps.sp.get_storage(AgentMemory).patch_if(settings.id, {"last_curated_at": now.isoformat()}, where={"enabled": [settings.enabled]})
    # The job outlives its week, or the daily chain would end with the first expiry.
    await jobs.patch_if(job.id, to_jsonable_python({"expires_at": now + JOB_LIFETIME}), where={"agent_id": [job.agent_id]})
    return ReleaseOutcome(success=True, requeue_after=CURATE_EVERY)


async def run_topics(deps: MemoryDeps, job: MemoryJob, agent: Agent, settings: AgentMemory) -> None:
    from primer.memory.topics import derive_topics

    await derive_topics(deps, agent, settings)
    await _forget(deps.sp.get_storage(MemoryJob), job)


# --- the gates ---------------------------------------------------------------------------------------------------------------------------------

_DROP = ReleaseOutcome(success=True, drop_lease=True)


def _until_next_utc_midnight(now: datetime) -> timedelta:
    return datetime.combine(_day(now) + timedelta(days=1), time.min, tzinfo=UTC) - now.astimezone(UTC)


def _nothing_happened(messages: list[Message]) -> bool:
    """No tool call and no user message after the first user message: a chat that taught nothing."""
    start = next((i for i, m in enumerate(messages) if m.role == "user"), None)
    if start is None:
        return True
    rest = messages[start + 1 :]
    return not any(m.role == "user" or any(isinstance(p, ToolCallPart) for p in m.parts) for m in rest)


async def _trivial(deps: MemoryDeps, job: MemoryJob) -> str | None:
    """Why the session is too small to learn from (the detail the skip event carries), or None when it is worth a model call."""
    if job.target_turn - job.reflected_turn < MIN_TURNS:
        return f"turns {job.reflected_turn + 1} to {job.target_turn}: fewer than {MIN_TURNS}"
    if deps.load_messages is None:
        return None
    try:
        messages = await deps.load_messages(job)
    except Exception:  # noqa: BLE001 - an unreadable log is the runner's to report, not a reason to skip
        logger.warning("memory job %s: could not read the session messages for the triviality gate", job.id, exc_info=True)
        return None
    if messages is not None and _nothing_happened(messages):
        return f"{len(messages)} messages and no tool call or second user message"
    return None


async def _skipped(deps: MemoryDeps, job: MemoryJob, reason: str, detail: str | None = None, *, retrying: bool = False) -> None:
    """A reflection attempt that did not complete says so (``memory.reflection.skipped``), so the activity feed never goes quiet."""
    await deps.recorder.emit(
        "memory.reflection.skipped",
        payload={"session_id": job.session_id, "reason": reason, "detail": detail, "retrying": retrying},
        actor=f"memory:{job.agent_id}", entity_kind="agent", entity_id=job.agent_id,
    )


async def report_job_error(deps: MemoryDeps, lease: Lease, exc: BaseException) -> None:
    """A job that raised is dropped; its reflection says why (best effort: the row or the recorder may be what failed)."""
    try:
        job = await deps.sp.get_storage(MemoryJob).get(lease.entity_id)
        if job is not None and job.kind == "reflect":
            await _skipped(deps, job, "error", f"{type(exc).__name__}: {exc}"[:200])
    except Exception:  # noqa: BLE001 - reporting must not raise out of the handler's cleanup
        logger.warning("memory job %s: could not report its failure", lease.entity_id, exc_info=True)


async def run_memory_job(deps: MemoryDeps, lease: Lease) -> ReleaseOutcome:
    """Run one claimed job: the gates in order, then the runner of its kind. The outcome says what the lease does next."""
    sp, now = deps.sp, deps.now()
    jobs = sp.get_storage(MemoryJob)
    job = await jobs.get(lease.entity_id)
    if job is None:
        return _DROP
    agent = await sp.get_storage(Agent).get(job.agent_id)
    if agent is None:  # the agent is gone: the job goes with it
        await _forget(jobs, job)
        return _DROP
    settings = await sp.get_storage(AgentMemory).get(job.agent_id)
    # 1. memory is off (or was never on): nothing is written, the lease is dropped
    if settings is None or not settings.enabled:
        if job.kind == "reflect":
            await _skipped(deps, job, "disabled")
        return _DROP
    # 2. a job nobody ran for a week is stale
    if now > job.expires_at:
        if job.kind == "reflect":
            await _skipped(deps, job, "expired")
        await _forget(jobs, job)
        return _DROP
    if job.kind == "reflect":
        # 3. over today's budget: try again after the UTC midnight
        if not budget_allows(settings, now):
            await _skipped(deps, job, "budget", retrying=True)
            return ReleaseOutcome(success=True, requeue_after=_until_next_utc_midnight(now))
        # 4. a session too small to learn from: marked reflected, no model call
        if (small := await _trivial(deps, job)) is not None:
            await _skipped(deps, job, "too_small", small)
            await jobs.patch_if(job.id, {"reflected_turn": job.target_turn}, where={"agent_id": [job.agent_id]})
            return _DROP
        settings = await consume_budget(sp, settings, now)
        try:
            await run_reflect(deps, job, agent, settings)
        except ReflectionTransient as exc:
            # the claim engine's normal backoff retries it; after MAX_ATTEMPTS the reflection is given up on, with its event
            retrying = lease.attempt_count + 1 < MAX_ATTEMPTS
            await _skipped(deps, job, "llm_unavailable", str(exc)[:200], retrying=retrying)
            if retrying:
                return ReleaseOutcome(success=False, last_error=str(exc)[:500])
    elif job.kind == "curate":
        return await run_curate(deps, job, agent, settings) or _DROP
    else:
        await run_topics(deps, job, agent, settings)
    return _DROP


async def _forget(jobs: Any, job: MemoryJob) -> None:
    try:
        await jobs.delete(job.id)
    except Exception:  # noqa: BLE001 - cleanup; the lease is dropped either way
        logger.warning("memory job %s: could not delete its row", job.id, exc_info=True)
