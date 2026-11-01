"""Capture topics (agent memory, spec 8): what an agent is for, derived from its system prompt and tools by one model call.

The result is the owner's to edit. A list the owner has edited (``topics_edited``) is never overwritten: a later derivation only leaves a
``topics_suggestion`` beside it. The do-not-capture list always carries the personal data classes, whatever the model says.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError

from primer.memory.prompts import DEFAULT_TOPICS_PROMPT
from primer.model.agent import Agent
from primer.model.memory import AgentMemory

logger = logging.getLogger(__name__)

#: The classes of personal data (spec 6.4) that are never captured.
PERSONAL_DATA_CLASSES = [
    "names of people",
    "contact details (emails, phone numbers, addresses)",
    "credentials and secrets",
    "user-specific data",
    "identifiers (user, account, session and workspace ids)",
]
#: A topics job is due this long after the edit that asked for it, so the edit has been written when it runs.
TOPICS_DELAY = timedelta(minutes=1)


class TopicsOutput(BaseModel):
    capture_topics: list[str]
    do_not_capture: list[str] = []


def _clean(items: list[str]) -> list[str]:
    """The items trimmed, without empties or repeats (case aside), in order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = " ".join(item.split())
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def _input(agent: Agent) -> str:
    prompt = "\n\n".join(agent.system_prompt) if agent.system_prompt else "(none)"
    tools = "\n".join(f"- {t}" for t in agent.tools) or "(none)"
    return f"# System prompt\n{prompt}\n\n# Tools\n{tools}"


async def derive_topics(deps: Any, agent: Agent, settings: AgentMemory) -> AgentMemory:
    """Derive the topics and store them; returns the settings as stored. A profile that cannot be resolved, or an answer that is not the schema,
    changes nothing (the next prompt change or the owner's own edit is the way on)."""
    from primer.memory.reflect import resolve_agent_llm

    resolved = await resolve_agent_llm(deps, agent)
    if resolved is None:
        return settings
    llm, model = resolved
    try:
        raw = await llm.judge_structured(
            model=model.model_name or "", system_prompt=DEFAULT_TOPICS_PROMPT, user_message=_input(agent),
            response_schema=TopicsOutput.model_json_schema(),
        )
        out = TopicsOutput.model_validate(raw)
    except (ValueError, ValidationError):
        logger.info("memory %s: the topics answer was not usable", agent.id, exc_info=True)
        return settings
    topics = _clean(out.capture_topics)
    store = deps.sp.get_storage(AgentMemory)
    if settings.topics_edited:
        patch: dict[str, Any] = {"topics_suggestion": topics}
    else:
        patch = {"capture_topics": topics, "do_not_capture": _clean([*out.do_not_capture, *PERSONAL_DATA_CLASSES]), "topics_suggestion": None}
    written = await store.patch_if(settings.id, patch, where={"enabled": [settings.enabled]})
    return written or settings


async def schedule_topics_on_change(sp: Any, engine: Any, entity: Agent, existing: Agent, now: datetime) -> None:
    """An agent edit that changes its system prompt or tools, for an agent with memory on, queues a topics job a moment later.

    Called before the edit is written (the REST pre-update hook and the tool's), hence the delay. With no claim engine (a tool that runs where none
    is wired) only the job row is made; the daily curator run picks it up (see ``run_curate``).
    """
    if entity.system_prompt == existing.system_prompt and entity.tools == existing.tools:
        return
    settings = await sp.get_storage(AgentMemory).get(existing.id)
    if settings is None or not settings.enabled:
        return
    from primer.memory.jobs import schedule_job

    await schedule_job(sp, engine, kind="topics", agent_id=existing.id, next_attempt_at=now + TOPICS_DELAY)


__all__ = ["PERSONAL_DATA_CLASSES", "TopicsOutput", "derive_topics", "schedule_topics_on_change"]
