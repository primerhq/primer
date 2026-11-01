"""Reflection (agent memory, spec 6.1 to 6.6): one structured model call over a session's new turns, and the deltas it proposes applied to the store.

It runs on the AGENT's own model profile, never the session's override. Whatever the model returns is validated against ``ReflectionOutput`` (at
most three deltas) and every delta is scrubbed (Task 4) before it is written; an answer that fails either is dropped whole, with one
``memory.reflection.skipped`` event and no retry. A transport failure raises ``ReflectionTransient`` for the job runner to requeue.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from primer.access.person import person_from_ref
from primer.access.policy import level_of
from primer.memory.collection import memory_collection_id
from primer.memory.entry import slugify_name
from primer.memory.prompts import DEFAULT_REFLECTION_PROMPT
from primer.memory.scrub import known_names, personal_data_hits
from primer.model.access import AccessLevel
from primer.model.agent import Agent
from primer.model.chat import (
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from primer.model.collection import Collection
from primer.model.memory import (
    AgentMemory,
    MemoryAdd,
    MemoryCredit,
    MemoryJob,
    MemoryPatch,
    ReflectionOutput,
)
from primer.model.provider import LLMProvider
from primer.model.workspace_session import WorkspaceSession

logger = logging.getLogger(__name__)

#: The transcript sent to the model is cut to about this many tokens (4 characters each) after the oldest turns are dropped.
INPUT_TOKEN_CAP = 30_000
_CHARS_PER_TOKEN = 4
#: Related entry bodies sent with the transcript, at most.
RELATED_ENTRIES = 5
_CLIP = 500



class ReflectionTransient(Exception):
    """The model could not be reached (or its stream failed): the job may be retried."""


@dataclass
class ReflectionReport:
    written: int = 0
    dropped: int = 0
    run_succeeded: bool | None = None
    skipped_reason: str | None = None


# --- what the run used -------------------------------------------------------------------------------------------------------------------------


def _entry_name(path: str) -> str | None:
    """The entry name of a collection path in ``active/`` or ``candidates/``, else None."""
    folder, _, name = path.partition("/")
    return name or None if folder in ("active", "candidates") else None


def _search_paths(output: str) -> list[str]:
    try:
        hits = json.loads(output).get("hits", [])
    except (ValueError, AttributeError):
        return []
    return [h["path"] for h in hits if isinstance(h, dict) and isinstance(h.get("path"), str)]


def used_entries(messages: list[Message], collection_id: str, lookups: Iterable[dict[str, Any]] = ()) -> tuple[set[str], str | None]:
    """``(names the run used, the trial candidate's name)``.

    Used means a ``collections__search`` hit or a successful ``collections__read_document`` on this collection, or an entry a ``memory.lookup``
    event of the run shows was put in front of the model (``lookups`` are the payloads: ``paths`` and ``trial``). Nothing else counts.
    """
    calls: dict[str, ToolCallPart] = {}
    names: set[str] = set()
    trial: str | None = None
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart) and part.name in ("collections__search", "collections__read_document"):
                if part.arguments.get("collection") == collection_id:
                    calls[part.id] = part
            elif isinstance(part, ToolResultPart) and part.id in calls and not part.error:
                call = calls[part.id]
                paths = _search_paths(part.output) if call.name == "collections__search" else [str(call.arguments.get("path", ""))]
                names.update(n for n in map(_entry_name, paths) if n)
    for lookup in lookups:
        names.update(n for n in map(_entry_name, lookup.get("paths") or []) if n)
        if lookup.get("trial"):
            trial = _entry_name(lookup["trial"]) or lookup["trial"]
            names.add(trial)
    return names, trial


# --- the input ---------------------------------------------------------------------------------------------------------------------------------


async def _lookups(deps: Any, agent: Agent, job: MemoryJob) -> list[dict[str, Any]]:
    """The ``memory.lookup`` payloads of this session within the turns being reflected on (``reflected_turn < turn <= target_turn``)."""
    if not job.session_id:
        return []
    events = deps.sp.get_event_store()
    out: list[dict[str, Any]] = []
    after = 0
    while True:
        page = await events.read_after(after, limit=200, event_type_prefix="memory.lookup", entity_kind="agent", entity_id=agent.id)
        for event in page:
            payload = event.payload or {}
            if event.session_id == job.session_id and job.reflected_turn < int(payload.get("turn", 0)) <= job.target_turn:
                out.append(payload)
        if len(page) < 200:
            return out
        after = page[-1].id


def _clip(text: str, limit: int = _CLIP) -> str:
    return text if len(text) <= limit else text[:limit] + " [...]"


def _new_turns(messages: list[Message], job: MemoryJob) -> list[Message]:
    """The messages of turns ``reflected_turn + 1 .. target_turn``; a turn starts at a user message."""
    starts = [i for i, m in enumerate(messages) if m.role == "user"]
    if not starts or job.reflected_turn >= len(starts):
        return []
    end = starts[job.target_turn] if job.target_turn < len(starts) else len(messages)
    return messages[starts[job.reflected_turn] : end]


def _render(messages: list[Message]) -> str:
    lines: list[str] = []
    for m in messages:
        for part in m.parts:
            if isinstance(part, TextPart):
                lines.append(f"[{m.role}] {_clip(part.text, 2000)}")
            elif isinstance(part, ToolCallPart):
                lines.append(f"[tool call] {part.name} {_clip(json.dumps(part.arguments, default=str))}")
            elif isinstance(part, ToolResultPart):
                lines.append(f"[tool {'ERROR' if part.error else 'result'}] {_clip(part.output)}")
    text = "\n".join(lines)
    cap = INPUT_TOKEN_CAP * _CHARS_PER_TOKEN
    # shortcut: the oldest part is dropped, not summarised by a compaction call; upgrade when sessions routinely outgrow the cap.
    return text if len(text) <= cap else "[earlier turns omitted]\n" + text[-cap:]


async def _related(deps: Any, agent_id: str, query: str) -> list[str]:
    """Up to five entry bodies most related to the session's first user message; none when the collection cannot be searched."""
    from primer.knowledge.collection_query import run_collection_query
    from primer.memory.entry import render_body

    try:
        collection = await deps.sp.get_storage(Collection).get(memory_collection_id(agent_id))
        if collection is None or not query.strip():
            return []
        hits, _ = await run_collection_query(
            sp=deps.sp, provider_registry=deps.provider_registry, semantic_search_registry=deps.semantic_search_registry,
            collection=collection, query=query[:500], limit=RELATED_ENTRIES + 1,
        )
    except Exception:  # noqa: BLE001 - related entries are context; the reflection goes on without them
        logger.info("memory %s: related entries unavailable", agent_id, exc_info=True)
        return []
    out: list[str] = []
    for hit in hits:
        name = _entry_name(hit.path)
        entry = await deps.store.get(agent_id, name) if name else None
        if entry is not None and entry.status != "archived":
            out.append(f"## {entry.name} ({entry.status})\n{entry.description}\n{render_body(entry.body)}")
        if len(out) == RELATED_ENTRIES:
            break
    return out


def _first_user_text(messages: list[Message]) -> str:
    for m in messages:
        if m.role == "user":
            return " ".join(p.text for p in m.parts if isinstance(p, TextPart))
    return ""


async def _build_input(deps: Any, agent: Agent, settings: AgentMemory, session: WorkspaceSession | None, turns: list[Message], used: set[str]) -> str:
    active = (await deps.store.list_entries(agent.id, "active"))[: settings.max_index_entries]
    tool_errors = sum(isinstance(p, ToolResultPart) and p.error for m in turns for p in m.parts)
    sections = [
        f"# Outcome signals\nfinal status: {session.status.value if session else 'unknown'}\ntool errors: {tool_errors}",
        "# Capture topics\n" + ("\n".join(f"- {t}" for t in settings.capture_topics) or "(any topic)"),
        "# Do not capture\n" + ("\n".join(f"- {t}" for t in settings.do_not_capture) or "(nothing listed)"),
        "# Active entries (index)\n" + ("\n".join(f"- {e.name}: {e.description}" for e in active) or "(none)"),
        "# Related entries\n" + ("\n\n".join(await _related(deps, agent.id, _first_user_text(turns))) or "(none)"),
        "# Entries used in this session\n" + (", ".join(sorted(used)) or "(none)"),
        "# Transcript\n" + _render(turns),
    ]
    return "\n\n".join(sections)


# --- the run -----------------------------------------------------------------------------------------------------------------------------------


def _texts(delta: MemoryAdd | MemoryPatch) -> str:
    if isinstance(delta, MemoryAdd):
        return "\n".join([delta.name, delta.description, *delta.steps, *delta.pitfalls, delta.example or "", *delta.topics])
    return "\n".join([delta.name, *(e.text or "" for e in delta.edits)])


async def _skip(deps: Any, agent: Agent, job: MemoryJob, reason: str, detail: str | None = None) -> ReflectionReport:
    await deps.recorder.emit(
        "memory.reflection.skipped", payload={"session_id": job.session_id, "reason": reason, "detail": detail, "retrying": False}, actor=f"memory:{agent.id}",
        entity_kind="agent", entity_id=agent.id,
    )
    return ReflectionReport(skipped_reason=reason)


async def _mark_reflected(deps: Any, job: MemoryJob) -> None:
    await deps.sp.get_storage(MemoryJob).patch_if(job.id, {"reflected_turn": job.target_turn}, where={"agent_id": [job.agent_id]})


async def resolve_agent_llm(deps: Any, agent: Agent) -> tuple[Any, Any] | None:
    from primer.model_profile.resolver import resolve_llm

    try:
        return await resolve_llm(deps.sp, deps.provider_registry, default_profile_id=agent.model.profile_id)
    except Exception:  # noqa: BLE001 - a deleted, unusable or unreachable profile ends the job, whatever the cause
        logger.info("memory %s: the agent's model profile cannot be resolved", agent.id, exc_info=True)
        return None


async def reflect(deps: Any, job: MemoryJob, agent: Agent, settings: AgentMemory) -> ReflectionReport:
    """Reflect on the session's new turns and apply the deltas. The budget is counted by the caller."""
    resolved = await resolve_agent_llm(deps, agent)
    if resolved is None:
        return await _skip(deps, agent, job, "profile_unavailable")
    llm, model = resolved
    messages = await deps.load_messages(job) if deps.load_messages is not None else None
    if messages is None:
        return await _skip(deps, agent, job, "transcript_unavailable")
    turns = _new_turns(messages, job)
    if not turns:
        await _mark_reflected(deps, job)
        return await _skip(deps, agent, job, "no_new_turns")
    collection_id = memory_collection_id(agent.id)
    used, trial = used_entries(messages, collection_id, await _lookups(deps, agent, job))
    session = await deps.sp.get_storage(WorkspaceSession).get(job.session_id) if job.session_id else None
    user_message = await _build_input(deps, agent, settings, session, turns, used)
    try:
        raw = await llm.judge_structured(
            model=model.model_name or "", system_prompt=settings.reflection_prompt or DEFAULT_REFLECTION_PROMPT, user_message=user_message,
            response_schema=ReflectionOutput.model_json_schema(),
        )
    except ValueError as exc:   # the answer was not a JSON object
        await _mark_reflected(deps, job)
        return await _skip(deps, agent, job, "invalid_output", str(exc)[:200])
    except Exception as exc:  # noqa: BLE001 - the model could not be reached: the runner requeues
        raise ReflectionTransient(str(exc)) from exc
    try:
        output = ReflectionOutput.model_validate(raw)
    except ValidationError as exc:
        await _mark_reflected(deps, job)
        return await _skip(deps, agent, job, "invalid_output", str(exc)[:200])

    ref = (session.turn_person or session.initiated_by) if session else None
    person = person_from_ref(ref)
    trigger_run = bool(ref is not None and ref.type == "trigger")
    needs_approval = settings.promotion == "approve" or (settings.teachers == "owner" and level_of(person, agent.access) != AccessLevel.OWNER)
    source = {"session_id": job.session_id, "from_turn": job.reflected_turn + 1, "to_turn": job.target_turn}
    names = await known_names(deps.sp)
    provider = None
    if getattr(model, "provider_id", None):
        provider = await deps.sp.get_storage(LLMProvider).get(model.provider_id)
    report = ReflectionReport(run_succeeded=output.run_succeeded)
    for delta in output.deltas:
        done = None
        if isinstance(delta, MemoryCredit):
            slug = slugify_name(delta.name)
            if slug in used:
                done = await deps.store.credit(
                    agent.id, delta.name, delta.verdict, run_succeeded=output.run_succeeded, trial=slug == trial, at=deps.now(),
                    trigger_run=trigger_run,
                )
        elif personal_data_hits(_texts(delta), known_names=names, provider=provider):
            done = None
        elif isinstance(delta, MemoryAdd):
            done = await deps.store.add_candidate(
                agent.id, delta, source=source, taught_by=person.user_id or "system", needs_approval=needs_approval,
            )
        else:
            done = await deps.store.patch(agent.id, delta, source=source)
        report.written += done is not None
        report.dropped += done is None
    await _mark_reflected(deps, job)
    await deps.recorder.emit(
        "memory.reflection.completed", payload={"session_id": job.session_id, "written": report.written, "dropped": report.dropped,
                                                "run_succeeded": output.run_succeeded},
        actor=f"memory:{agent.id}", entity_kind="agent", entity_id=agent.id,
    )
    return report


def session_message_loader(sp: Any, read_log: Callable[[str, str], Awaitable[bytes]]) -> Callable[[MemoryJob], Awaitable[list[Message] | None]]:
    """``MemoryDeps.load_messages``: the session's whole LLM history (compaction markers honoured), or None when the session or its log is gone.

    ``read_log(workspace_id, relative_path)`` reads a workspace state file (the worker's IO shim).
    """
    from primer.workspace.session import reconstruct_compacted_history

    async def load(job: MemoryJob) -> list[Message] | None:
        if not job.session_id:
            return None
        row = await sp.get_storage(WorkspaceSession).get(job.session_id)
        if row is None:
            return None
        try:
            raw = await read_log(row.workspace_id, f"sessions/{row.id}/messages.jsonl")
        except Exception:  # noqa: BLE001 - an unreadable log is reported by the caller as "no transcript"
            logger.info("memory job %s: the session log cannot be read", job.id, exc_info=True)
            return None
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return reconstruct_compacted_history(text.splitlines())

    return load


__all__ = ["DEFAULT_REFLECTION_PROMPT", "ReflectionReport", "ReflectionTransient", "reflect", "resolve_agent_llm", "session_message_loader", "used_entries"]
