"""An agent's memory settings: read, change, and clean up (agent memory Task 5).

``registries`` is anything with ``provider_registry``, ``semantic_search_registry`` and ``claim_engine`` attributes: the app state in the routes, a
namespace in tests. Only an owner changes settings (the route checks); turning memory on provisions the collection and starts the topics and the
daily curator jobs; turning it off keeps every entry; deleting the agent removes everything.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from primer.int.claim import ClaimKind
from primer.memory.collection import delete_memory_collection, ensure_memory_collection
from primer.memory.jobs import schedule_job
from primer.model.agent import Agent
from primer.model.common import dump_for_storage
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.memory import AgentMemory, MemoryJob
from primer.model.storage import OffsetPage
from primer.storage.q import Q

logger = logging.getLogger(__name__)

#: What an owner sets. Everything else on the row (the day's reflection count, the curator's last run, the topics suggestion) is the system's.
EDITABLE = (
    "enabled", "promotion", "teachers", "idle_minutes", "auto_inject", "inject_top_k", "inject_min_score", "max_index_entries", "max_active",
    "archive_after_days", "daily_reflections", "search", "capture_topics", "do_not_capture", "reflection_prompt",
)
#: How long after memory is turned on the first curator run is due.
FIRST_CURATION = timedelta(hours=24)


async def get_settings(sp: Any, agent_id: str) -> AgentMemory:
    """The agent's settings; the defaults (memory off) when it has none yet."""
    row = await sp.get_storage(AgentMemory).get(agent_id)
    return row if row is not None else AgentMemory(id=agent_id)


async def save_settings(sp: Any, registries: Any, agent: Agent, new: AgentMemory, *, actor: Any = None) -> AgentMemory:
    """Store the owner's editable fields of ``new`` and keep the system's fields as they are; returns the row as stored.

    Turning memory on first makes sure the collection exists (a search the providers cannot serve refuses the whole change), and after the write queues
    the topics job (now) and the curator (in 24 hours). An edit of a topic list marks the lists as the owner's, so a later prompt change only suggests
    new ones. The write is field-scoped and guarded on ``enabled``, the field the on/off transition is judged from: a concurrent counter write by the
    reflection budget is never put back, and a lost race re-reads and judges again.
    """
    store = sp.get_storage(AgentMemory)
    for _ in range(3):
        old = await store.get(agent.id)
        current = old if old is not None else AgentMemory(id=agent.id)
        turning_on = new.enabled and not current.enabled
        edited = current.topics_edited or new.capture_topics != current.capture_topics or new.do_not_capture != current.do_not_capture
        candidate = current.model_copy(update={**{name: getattr(new, name) for name in EDITABLE}, "topics_edited": edited})
        if new.enabled and (turning_on or new.search != current.search):
            await ensure_memory_collection(sp, registries.provider_registry, registries.semantic_search_registry, agent, candidate)
        if old is None:
            try:
                saved = await store.create(candidate)
            except ConflictError:
                continue  # another write created it first: judge again against that row
        else:
            dumped = dump_for_storage(candidate)
            written = await store.patch_if(
                agent.id, {name: dumped[name] for name in (*EDITABLE, "topics_edited")}, where={"enabled": [current.enabled]},
            )
            if written is None:
                continue
            saved = written
        if turning_on:
            now, engine = datetime.now(UTC), registries.claim_engine
            await schedule_job(sp, engine, kind="topics", agent_id=agent.id, next_attempt_at=now)
            await schedule_job(sp, engine, kind="curate", agent_id=agent.id, next_attempt_at=now + FIRST_CURATION)
        return saved
    raise ConflictError(f"the memory settings of agent {agent.id!r} kept changing while they were being saved; retry")


async def delete_agent_memory(sp: Any, ssr: Any, engine: Any, agent_id: str) -> None:
    """Remove an agent's memory: its collection with every entry, its settings row, its queued jobs and their leases. Idempotent."""
    await delete_memory_collection(sp, ssr, agent_id)
    jobs = sp.get_storage(MemoryJob)
    while True:
        page = await jobs.find(Q(MemoryJob).where("agent_id", agent_id).build(), OffsetPage(offset=0, length=100))
        if not page.items:
            break
        for job in page.items:
            if engine is not None:
                await engine.delete_lease(ClaimKind.MEMORY, job.id)
            try:
                await jobs.delete(job.id)
            except NotFoundError:
                pass
    try:
        await sp.get_storage(AgentMemory).delete(agent_id)
    except NotFoundError:
        pass


__all__ = ["EDITABLE", "delete_agent_memory", "get_settings", "save_settings"]
