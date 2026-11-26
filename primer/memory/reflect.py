"""Reflection (agent memory, spec 6.1 to 6.6): one structured model call over a session's new turns, and the deltas it proposes applied to the store.

It runs on the AGENT's own model profile, never the session's override. Whatever the model returns is validated against ``ReflectionOutput`` (at
most three deltas) and every delta is scrubbed (Task 4) before it is written; an answer that fails either is dropped whole, with one
``memory.reflection.skipped`` event and no retry. A transport failure raises ``ReflectionTransient`` for the job runner to requeue.
"""

from __future__ import annotations

import asyncio
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
from primer.memory.reflection_input import (
    ATTEMPT_SECONDS, Budget, EvidenceSummary, InputRefused, InvalidVerdict,
    MAX_CHUNKS, MEMORY_SUMMARY_PROMPT, Plan, bounded_judge, fragments,
    plan_input, prepare_input, request_size, summary_input, text_tokens, transcript,
)
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
from primer.model.workspace_session import SessionMessageKind, WorkspaceSession

logger = logging.getLogger(__name__)

#: Related entry bodies sent with the transcript, at most.
RELATED_ENTRIES = 5


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
    """The ``memory.lookup`` payloads of this session within the turns being reflected on (``reflected_turn < turn <= target_turn``).

    The scan is bounded by the session's own events (the store's ``(session_id, id)`` index), so the agent's lookups of other sessions (the bulk of its 30
    days of events) are never read, whatever their age.
    """
    if not job.session_id:
        return []
    events = deps.sp.get_event_store()
    out: list[dict[str, Any]] = []
    after = 0
    while True:
        page = await events.read_after(after, limit=200, event_type_prefix="memory.lookup", entity_kind="agent", entity_id=agent.id, session_id=job.session_id)
        for event in page:
            payload = event.payload or {}
            if job.reflected_turn < int(payload.get("turn", 0)) <= job.target_turn:
                out.append(payload)
        if len(page) < 200:
            return out
        after = page[-1].id


def turn_starts(messages: list[Message]) -> list[int]:
    """Where each turn's input begins. A turn's input is a run of user messages (a session starts with its instructions after a greeting
    message, a steer carries its attachments): the run counts once, and the tool results of a round are not user messages."""
    return [i for i, m in enumerate(messages) if m.role == "user" and (i == 0 or messages[i - 1].role != "user")]


def plan_newest_window(messages: list[Message], first_turn: int, *, context: str, system: str,
                       schema: dict, window: int) -> tuple[Plan, int, int]:
    """``(plan, first, last)``: the plan over the newest whole turns the bounded input can cover, as 0-based indexes into the range's turns.

    A range the plan cannot cover would be refused again on every later attempt, since it only grows. The window ends at the newest turn that fits on
    its own (a turn too large for the input is left out wherever it is) and reaches back as far as still fits. A turn that does not fit on its own is
    a wall the window cannot cross: the turns older than it fall into the skipped count as well, even ones that would fit alone, because the window
    is contiguous and ends at the newest turn. ``input_compaction_limit`` is raised only when no turn fits alone. Refusals that are not about the
    range (an oversized fixed policy) pass through.
    """
    starts = turn_starts(messages)
    bounds = [*starts, len(messages)]

    def attempt(first: int, last: int) -> Plan | None:
        try:
            return plan_input(messages[bounds[first]:bounds[last + 1]], first_turn + first, context=context, system=system,
                              schema=schema, window=window)
        except InputRefused as exc:
            if str(exc) != "input_compaction_limit":
                raise
            return None

    last = len(starts) - 1
    plan = attempt(0, last)
    if plan is not None:
        return plan, 0, last
    # The range will not be covered. Walk back to the newest turn that fits on its own; a turn whose fragments cannot fill the bounded summary chunks
    # is refused by a cheap token count, not a plan (review 2026-11-23 X2.2), so the oversized tail costs its content, not one plan per turn.
    # The count is exact in the direction that skips: the estimator's join inequality is exact (T(a+b) <= T(a) + T(b): the CJK count is
    # additive and the ceil is subadditive, so a join costs no more than the two token counts, no +1 at a join), and by monotonicity a
    # fitting chunk's joined lines hold T(joined) <= chunk_capacity, so the real cost of combining chunks is only the MAX_CHUNKS - 1
    # newline separators (at most one token each, the (MAX_CHUNKS - 1) term); the +1 per chunk (and per line) in the thresholds is
    # deliberate slack - so a turn above MAX_CHUNKS * (chunk_capacity + 1) + (MAX_CHUNKS - 1) total, or one line above chunk_capacity
    # + 1, is refused by plan_input with input_compaction_limit too: the count never skips a turn the old per-turn walk would reflect
    # (review 2026-11-23 X2.2 / M1, pinned at the exact boundary in tests/memory/test_reflect.py). The slack errs toward planning: a
    # turn the count lets through that plan_input still refuses (the packing needs more chunks, or the per-chunk output budget
    # starves) costs exactly one plan, never an oversized one.
    summary_budget = Budget.for_model(window, summary=True)
    fixed_summary = request_size(MEMORY_SUMMARY_PROMPT, summary_input([]), EvidenceSummary.model_json_schema())
    chunk_capacity = summary_budget.input_tokens - fixed_summary
    while last >= 0:
        lines = [f.line for f in fragments(messages[bounds[last]:bounds[last + 1]], first_turn + last)]
        if (text_tokens("\n".join(lines)) > MAX_CHUNKS * (chunk_capacity + 1) + (MAX_CHUNKS - 1)
                or max(map(text_tokens, lines), default=0) > chunk_capacity + 1):
            last -= 1
            continue
        plan = attempt(last, last)
        if plan is not None:
            break
        last -= 1
    if last < 0:
        raise InputRefused("input_compaction_limit")
    low, high, found = 0, last, (plan, last)
    while low < high:  # the fewest older turns left out; fewer turns never need more chunks
        mid = (low + high) // 2
        plan = attempt(mid, last)
        if plan is None:
            low = mid + 1
        else:
            found, high = (plan, mid), mid
    return found[0], found[1], last


def new_turns(messages: list[Message], job: MemoryJob) -> list[Message]:
    """The messages of turns ``reflected_turn + 1 .. target_turn``."""
    starts = turn_starts(messages)
    if not starts or job.reflected_turn >= len(starts):
        return []
    end = starts[job.target_turn] if job.target_turn < len(starts) else len(messages)
    return messages[starts[job.reflected_turn] : end]


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


async def _build_context(deps: Any, agent: Agent, settings: AgentMemory, session: WorkspaceSession | None, turns: list[Message], used: set[str], *, system: str, schema: dict, budget: Budget) -> tuple[str, dict]:
    active = (await deps.store.list_entries(agent.id, "active"))[: settings.max_index_entries]
    tool_errors = sum(isinstance(p, ToolResultPart) and p.error for m in turns for p in m.parts)
    sections = [
        f"# Outcome signals\nfinal status: {session.status.value if session else 'unknown'}\ntool errors: {tool_errors}",
        "# Capture topics\n" + ("\n".join(f"- {t}" for t in settings.capture_topics) or "(any topic)"),
        "# Do not capture\n" + ("\n".join(f"- {t}" for t in settings.do_not_capture) or "(nothing listed)"),
        "# Entries used in the reflected turns\n" + (", ".join(sorted(used)) or "(none)"),
    ]
    related = await _related(deps, agent.id, _first_user_text(turns))
    index = [f"- {e.name}: {e.description}" for e in active]
    sizes = (len(index), len(related))
    while True:
        context = "\n\n".join([*sections, "# Active entries (index)\n" + ("\n".join(index) or "(none)"),
                                 "# Related entries\n" + ("\n\n".join(related) or "(none)")])
        if request_size(system, transcript(context, ""), schema) <= budget.input_tokens // 2:
            return context, {"omitted_index_entries": sizes[0] - len(index), "omitted_related_entries": sizes[1] - len(related)}
        if related:
            related.pop()
        elif index:
            index.pop()
        else:
            return context, {"omitted_index_entries": sizes[0] - len(index), "omitted_related_entries": sizes[1] - len(related)}


async def _check_current(deps, job, agent, settings, session):
    from primer.memory.scheduling import locked_occurrence
    from primer.memory.settings import EDITABLE

    async with locked_occurrence(deps.sp, job, engine=deps.engine, lease=deps.lease) as (current, _):
        if current is None or current.target_turn != job.target_turn or current.reflected_turn != job.reflected_turn:
            raise InputRefused("input_superseded")
    fresh = await deps.sp.get_storage(AgentMemory).get(agent.id)
    current_agent = await deps.sp.get_storage(Agent).get(agent.id)
    if (fresh is None or not fresh.enabled or current_agent is None
            or current_agent.model != agent.model or current_agent.access != agent.access
            or any(getattr(fresh, key) != getattr(settings, key) for key in EDITABLE)):
        raise InputRefused("input_superseded")
    current_session = await deps.sp.get_storage(WorkspaceSession).get(job.session_id) if job.session_id else None
    if session is None or current_session is None or _session_identity(current_session) != _session_identity(session):
        raise InputRefused("input_superseded")


def _session_identity(session):
    # This is an execution-time witness, not an enqueue-time binding claim.
    return tuple(getattr(session, key) for key in (
        "created_at", "workspace_id", "binding", "binding_epoch", "turn_person", "initiated_by",
    ))


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
    from primer.memory.scheduling import patch_job_progress

    await patch_job_progress(deps, job, {"reflected_turn": job.target_turn})


async def resolve_agent_llm(deps: Any, agent: Agent) -> tuple[Any, Any] | None:
    from primer.model_profile.resolver import resolve_llm

    try:
        return await resolve_llm(deps.sp, deps.provider_registry, default_profile_id=agent.model.profile_id)
    except Exception:  # noqa: BLE001 - a deleted, unusable or unreachable profile ends the job, whatever the cause
        logger.info("memory %s: the agent's model profile cannot be resolved", agent.id, exc_info=True)
        return None


async def reflect(deps: Any, job: MemoryJob, agent: Agent, settings: AgentMemory) -> ReflectionReport:
    """Reflect on the session's new turns and apply the deltas. The budget is counted by the caller."""
    # ``deps.session`` is the session as captured before the log was read (run_memory_job); a direct caller has none and captures it here.
    session = getattr(deps, "session", None)
    if session is None:
        session = await deps.sp.get_storage(WorkspaceSession).get(job.session_id) if job.session_id else None
    if session is not None:
        session = session.model_copy(deep=True)

    async def check():
        await _check_current(deps, job, agent, settings, session)

    try:
        await check()
    except InputRefused as exc:
        return await _skip(deps, agent, job, str(exc))
    resolved = await resolve_agent_llm(deps, agent)
    if resolved is None:
        return await _skip(deps, agent, job, "profile_unavailable")
    llm, model = resolved
    try:
        messages = await deps.load_messages(job) if deps.load_messages is not None else None
        await check()
    except InputRefused as exc:
        return await _skip(deps, agent, job, str(exc))
    if messages is None:
        return await _skip(deps, agent, job, "transcript_unavailable")
    if len(turn_starts(messages)) < job.target_turn:
        return await _skip(deps, agent, job, "input_incomplete")
    turns = new_turns(messages, job)
    if not turns:
        await _mark_reflected(deps, job)
        return await _skip(deps, agent, job, "no_new_turns")
    collection_id = memory_collection_id(agent.id)
    # The "entries used" gate and section cover the turns being reflected on only (review 2026-11-23 X2.2 / M3): an entry consulted in an
    # already-reflected turn is not in the reflected turns' prompt and its credit must not land from this run.
    used, trial = used_entries(turns, collection_id, await _lookups(deps, agent, job))
    system = settings.reflection_prompt or DEFAULT_REFLECTION_PROMPT
    schema = ReflectionOutput.model_json_schema()
    budget = Budget.for_model(model.context_length)
    try:
        async with asyncio.timeout(ATTEMPT_SECONDS):
            await check()
            context, context_metadata = await _build_context(deps, agent, settings, session, turns, used,
                                           system=system, schema=schema, budget=budget)
            try:
                plan, first, last = plan_newest_window(turns, job.reflected_turn + 1, context=context, system=system,
                                                       schema=schema, window=model.context_length)
            except InputRefused as exc:
                if str(exc) != "input_compaction_limit":
                    raise
                # Not even the newest turn fits the bounded input, and the range only grows: retrying could never succeed, so the
                # watermark moves past it (a skipped range is never read again) and the event says what was given up.
                await check()
                await _mark_reflected(deps, job)
                logger.warning("memory %s: turns %d to %d of session %s are too large to reflect on; skipped", agent.id,
                               job.reflected_turn + 1, job.target_turn, job.session_id)
                return await _skip(deps, agent, job, "input_too_large_skipped",
                                   f"turns {job.reflected_turn + 1} to {job.target_turn} skipped: too large for the bounded input")
            user_message, input_metadata = await prepare_input(
                plan, llm=llm, model=model, context=context, system=system, schema=schema, check=check,
            )
            input_metadata.update(context_metadata)
            input_metadata["source_turns"] = job.target_turn - job.reflected_turn
            skipped = len(turn_starts(turns)) - (last - first + 1)
            input_metadata["skipped_turns"] = skipped
            if skipped:
                logger.info("memory %s: session %s reflected on %d of %d turns; %d did not fit the bounded input",
                            agent.id, job.session_id, last - first + 1, input_metadata["source_turns"], skipped)
            await check()
            try:
                raw = await bounded_judge(llm, model, system=system, user=user_message, schema=schema, budget=budget)
            except InvalidVerdict:
                # Invalid output is a terminal disposition only while this
                # attempt still has authority to advance the source watermark.
                await check()
                await _mark_reflected(deps, job)
                return await _skip(deps, agent, job, "invalid_output")
            await check()
    except InputRefused as exc:
        if str(exc) != "input_compaction_limit":
            return await _skip(deps, agent, job, str(exc))
        # A range refusal that survived planning: the summarised evidence, or the judge call's own pre-check, still exceeded the bounded
        # input. It is deterministic for this range (summaries at temperature zero), so retrying it could never succeed: advance the
        # watermark like the window path (no re-arm loop) and say what was given up (review 2026-11-23 X2.2).
        try:
            await check()
        except InputRefused as current:
            return await _skip(deps, agent, job, str(current))
        await _mark_reflected(deps, job)
        logger.warning("memory %s: turns %d to %d of session %s did not fit the bounded input after planning; skipped", agent.id,
                       job.reflected_turn + 1, job.target_turn, job.session_id)
        return await _skip(deps, agent, job, "input_too_large_skipped",
                           f"turns {job.reflected_turn + 1} to {job.target_turn} skipped: too large for the bounded input")
    except Exception as exc:  # noqa: BLE001 - existing durable retries; never log provider payloads
        raise ReflectionTransient("memory model unavailable") from exc
    try:
        output = ReflectionOutput.model_validate(raw)
    except ValidationError:
        await _mark_reflected(deps, job)
        return await _skip(deps, agent, job, "invalid_output")

    ref = (session.turn_person or session.initiated_by) if session else None
    person = person_from_ref(ref)
    trigger_run = bool(ref is not None and ref.type == "trigger")
    needs_approval = settings.promotion == "approve" or (settings.teachers == "owner" and level_of(person, agent.access) != AccessLevel.OWNER)
    source = {"session_id": job.session_id, "from_turn": job.reflected_turn + 1 + first, "to_turn": job.reflected_turn + 1 + last}
    # The crash-replay guard (EXEC2-03): a stable key for this reflect run (the job id is stable across the re-claims of the same run; the watermark
    # keeps the run apart from later runs on the same session). A delta a crashed run already applied to an entry is recorded under this key, so the
    # re-claim replays as a no-op instead of double-applying (primer/memory/store.py).
    run_key = f"{job.id}:{job.target_turn}"
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
                    trigger_run=trigger_run, run_key=run_key,
                )
        elif personal_data_hits(_texts(delta), known_names=names, provider=provider):
            done = None
        elif isinstance(delta, MemoryAdd):
            done = await deps.store.add_candidate(
                agent.id, delta, source=source, taught_by=person.user_id or "system", needs_approval=needs_approval,
            )
        else:
            done = await deps.store.patch(agent.id, delta, source=source, run_key=run_key)
        report.written += done is not None
        report.dropped += done is None
    await _mark_reflected(deps, job)
    await deps.recorder.emit(
        "memory.reflection.completed", payload={"session_id": job.session_id, "written": report.written, "dropped": report.dropped,
                                                "run_succeeded": output.run_succeeded, **input_metadata},
        actor=f"memory:{agent.id}", entity_kind="agent", entity_id=agent.id,
    )
    return report


async def _log_lines(sp: Any, read_log: Callable[[str, str], Awaitable[bytes]], job: MemoryJob, *, strict: bool = False) -> list[str] | None:
    """The lines of the job's session log, or None when the session or its log is gone."""
    if not job.session_id:
        return None
    row = await sp.get_storage(WorkspaceSession).get(job.session_id)
    if row is None:
        return None
    row = row.model_copy(deep=True)
    try:
        raw = await read_log(row.workspace_id, f"sessions/{row.id}")
    except Exception:  # noqa: BLE001 - an unreadable log is reported by the caller as "no transcript"
        logger.info("memory job %s: the session log cannot be read", job.id, exc_info=True)
        return None
    if strict:
        current = await sp.get_storage(WorkspaceSession).get(job.session_id)
        if current is None or _session_identity(current) != _session_identity(row):
            raise InputRefused("input_superseded")
    # The decode and the strict record check are CPU-bound over the whole log: off the worker's event loop, as the executor's own parse is.
    return await asyncio.to_thread(_checked_lines, raw, strict)


def _checked_lines(raw: Any, strict: bool) -> list[str]:
    try:
        lines = (raw.decode("utf-8", "strict" if strict else "replace")
                 if isinstance(raw, (bytes, bytearray)) else str(raw)).split("\n")
        if lines[-1] == "":
            lines.pop()  # LF terminates the last record; it is not another record.
        if strict:
            visible = {}
            last_seq = 0
            for line in lines:
                if not line.strip():
                    continue
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("not a log record")
                if "role" in obj or "parts" in obj:
                    Message.model_validate(obj)
                elif not isinstance(obj.get("kind"), str):
                    raise ValueError("unrecognized log record")
                else:
                    seq = obj.get("seq")
                    if seq is not None and (type(seq) is not int or seq <= last_seq):
                        raise ValueError("invalid record ordering")
                    if obj["kind"] == SessionMessageKind.REWIND_MARKER.value:
                        to_seq = (obj.get("payload") or {}).get("to_seq")
                        if (type(to_seq) is not int
                                or visible.get(to_seq) != SessionMessageKind.USER_INPUT.value
                                or (seq is not None and to_seq >= seq)):
                            raise ValueError("invalid rewind target")
                        visible = {key: kind for key, kind in visible.items() if key <= to_seq}
                    elif seq is not None:
                        visible[seq] = obj["kind"]
                    if seq is not None:
                        last_seq = seq
    except (ValueError, AttributeError, TypeError, RecursionError):
        raise InputRefused("input_incomplete") from None
    return lines


def _without_compaction_markers(lines: list[str]) -> list[str]:
    """The log as the model never saw it folded: a compaction marker summarises the head of the history FOR THE MODEL and deletes nothing, and
    what a reflection learns from is what was said and done, not its summary. Rewinds stay: a rewound turn did not happen."""
    marker = SessionMessageKind.COMPACTION_MARKER.value
    kept = []
    for line in lines:
        if marker in line:
            try:
                obj = json.loads(line)
                if obj.get("kind") == marker:
                    # Keep the sequence boundary for following seqless Message
                    # rows. Deleting it could attach discarded post-compaction
                    # messages to a surviving pre-compaction rewind target.
                    if obj.get("seq") is not None:
                        kept.append(json.dumps({"seq": obj["seq"]}))
                    continue
            except (ValueError, AttributeError):
                pass
        kept.append(line)
    return kept


def session_message_loader(sp: Any, read_log: Callable[[str, str], Awaitable[bytes]]) -> Callable[[MemoryJob], Awaitable[list[Message] | None]]:
    """``MemoryDeps.load_messages``: the session's whole history as it was lived (compaction markers ignored, the same parse as the executor's),
    or None when the session or its log is gone. Reflection validates record
    integrity before that fold; detectable damage or a changed read identity
    raises InputRefused instead of silently discarding evidence.

    ``read_log(workspace_id, slot)`` reads the whole session log under a session's state directory, every segment in order (the worker's IO
    shim, ``read_session_log``): what was said lives before the newest compaction marker too, so this reads every segment, not only that one's.
    """
    from primer.workspace.session import reconstruct_compacted_history

    async def load(job: MemoryJob) -> list[Message] | None:
        lines = await _log_lines(sp, read_log, job, strict=True)
        if lines is None:
            return None
        return await asyncio.to_thread(lambda: reconstruct_compacted_history(_without_compaction_markers(lines)))

    return load


def session_log_shapes(sp: Any, read_log: Callable[[str, str], Awaitable[bytes]]) -> Callable[[MemoryJob], Awaitable[str]]:
    """``MemoryDeps.describe_log``: what the first lines of the session log are (a role, or a kind), for a skip event to show the real file."""

    async def describe(job: MemoryJob) -> str:
        lines = await _log_lines(sp, read_log, job) or []
        shapes = []
        for line in lines[:20]:
            try:
                obj = json.loads(line)
            except ValueError:
                shapes.append("?")
                continue
            shapes.append(str(obj.get("role") or obj.get("kind") or "?") if isinstance(obj, dict) else "?")
        return f"{len(lines)} log lines: " + ",".join(shapes)

    return describe
