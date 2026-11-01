"""The curator (agent memory, spec 6.7): a daily pass over one agent's entries. It merges near-duplicates, archives what has gone stale and keeps the
active set under its cap. It never deletes anything: a change is a move to ``archived/`` (a merge leaves a ``merged_into`` pointer), and a JSON snapshot of
every entry is written before the first change.

The pass first decides what to do (reading only), then, if there is anything, snapshots and applies it. A pass that finds nothing writes nothing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError

from primer.knowledge.collection_query import run_collection_query
from primer.memory.collection import memory_collection_id
from primer.memory.entry import EntryBody, render_body
from primer.memory.prompts import DEFAULT_MERGE_PROMPT
from primer.memory.store import MemoryEntry
from primer.model.agent import Agent
from primer.model.collection import Collection
from primer.model.memory import AgentMemory

logger = logging.getLogger(__name__)

#: Cosine similarity at which two active entries are asked about (the spec leaves the value to the builder).
MERGE_SIMILARITY = 0.92
#: A candidate nobody credited in this long is archived (spec 9).
CANDIDATE_DAYS = 30
_NEIGHBOURS = 5


class MergeOutput(BaseModel):
    merged: bool
    description: str = ""
    steps: list[str] = []
    pitfalls: list[str] = []
    example: str | None = None


@dataclass
class CurationReport:
    merged: int = 0
    archived: int = 0
    snapshot_path: str | None = None


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _score(entry: MemoryEntry) -> int:
    return int(entry.meta.get("helpful", 0) or 0) - int(entry.meta.get("harmful", 0) or 0)


def _normal(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _created(entry: MemoryEntry) -> datetime:
    return _when(entry.meta.get("created_at")) or datetime.min.replace(tzinfo=UTC)


def _stale(entry: MemoryEntry, now: datetime, settings: AgentMemory) -> bool:
    """An active entry nobody has validated (a helpful credit) for ``archive_after_days``, that is neither pinned nor used by a trigger's run in that time."""
    if entry.meta.get("pinned"):
        return False
    horizon = now - timedelta(days=settings.archive_after_days)
    used = _when(entry.meta.get("last_trigger_use_at"))
    if used is not None and used >= horizon:
        return False
    validated = _when(entry.meta.get("last_validated_at")) or _created(entry)
    return validated < horizon


def _unused_candidate(entry: MemoryEntry, now: datetime) -> bool:
    credited = int(entry.meta.get("helpful", 0) or 0) + int(entry.meta.get("harmful", 0) or 0)
    return credited == 0 and _created(entry) < now - timedelta(days=CANDIDATE_DAYS)


def _over_cap(actives: list[MemoryEntry], cap: int) -> list[MemoryEntry]:
    """The entries to drop to stay at ``cap``: pinned ones are kept first, then the best helpful-minus-harmful (newer first on a tie)."""
    ranked = sorted(actives, key=lambda e: (not e.meta.get("pinned"), -_score(e), -_created(e).timestamp()))
    return ranked[cap:] if len(ranked) > cap else []


async def _pairs(deps: Any, agent: Agent, actives: list[MemoryEntry]) -> list[tuple[MemoryEntry, MemoryEntry]]:
    """Pairs of active entries that look alike, older first. By vector similarity when the collection is searched semantically; otherwise only
    entries whose descriptions are the same text once normalised."""
    by_name = {e.name: e for e in actives}
    found: dict[tuple[str, str], tuple[MemoryEntry, MemoryEntry]] = {}

    def add(a: MemoryEntry, b: MemoryEntry) -> None:
        older, newer = sorted((a, b), key=_created)
        found.setdefault((older.name, newer.name), (older, newer))

    collection = await deps.sp.get_storage(Collection).get(memory_collection_id(agent.id))
    semantic = False
    if collection is not None:
        for entry in actives:
            try:
                hits, mode = await run_collection_query(
                    sp=deps.sp, provider_registry=deps.provider_registry, semantic_search_registry=deps.semantic_search_registry,
                    collection=collection, query=f"{entry.description}\n{render_body(entry.body)}", limit=_NEIGHBOURS + 1,
                    path_prefix="active/", mode="semantic",
                )
            except Exception:  # noqa: BLE001 - no semantic search for this collection: the description rule below still applies
                logger.info("memory %s: no semantic search for the curator", agent.id, exc_info=True)
                break
            if mode != "semantic":
                break
            semantic = True
            for hit in hits:
                other = by_name.get(hit.path.removeprefix("active/"))
                if other is not None and other.name != entry.name and hit.score is not None and hit.score >= MERGE_SIMILARITY:
                    add(entry, other)
    if not semantic:
        groups: dict[str, list[MemoryEntry]] = {}
        for entry in actives:
            groups.setdefault(_normal(entry.description), []).append(entry)
        for key, group in groups.items():
            if key:
                for other in group[1:]:
                    add(group[0], other)
    return sorted(found.values(), key=lambda pair: (_created(pair[0]), _created(pair[1])))


async def _confirm(llm: Any, model: Any, older: MemoryEntry, newer: MemoryEntry) -> MergeOutput | None:
    def show(e: MemoryEntry) -> str:
        return f"## {e.name}\n{e.description}\n{render_body(e.body)}"

    try:
        raw = await llm.judge_structured(
            model=model.model_name or "", system_prompt=DEFAULT_MERGE_PROMPT,
            user_message=f"# Older entry\n{show(older)}\n\n# Newer entry\n{show(newer)}", response_schema=MergeOutput.model_json_schema(),
        )
        out = MergeOutput.model_validate(raw)
    except (ValueError, ValidationError):
        logger.info("memory: a merge answer was not usable", exc_info=True)
        return None
    return out if out.merged and (out.steps or out.pitfalls) else None


def _merged_body(out: MergeOutput) -> EntryBody:
    return EntryBody(
        steps=[(f"s{i}", t) for i, t in enumerate(out.steps, 1)],
        pitfalls=[(f"p{i}", t) for i, t in enumerate(out.pitfalls, 1)],
        example=[("e1", out.example)] if out.example else [],
    )


async def curate(deps: Any, agent: Agent, settings: AgentMemory, now: datetime) -> CurationReport:
    from primer.memory.reflect import resolve_agent_llm

    store = deps.store
    entries = await store.list_entries(agent.id)
    actives = [e for e in entries if e.status == "active"]
    candidates = [e for e in entries if e.status == "candidates"]

    # 1. decide, reading only
    gone: dict[str, str] = {}   # name -> reason
    for entry in actives:
        if _stale(entry, now, settings):
            gone[entry.name] = f"no helpful use in {settings.archive_after_days} days"
    for entry in candidates:
        if _unused_candidate(entry, now):
            gone[entry.name] = f"unused for {CANDIDATE_DAYS} days"
    kept = [e for e in actives if e.name not in gone]
    for entry in _over_cap(kept, settings.max_active):
        gone[entry.name] = f"over the cap of {settings.max_active} active entries"
    kept = [e for e in kept if e.name not in gone]

    merges: list[tuple[MemoryEntry, MemoryEntry, MergeOutput]] = []
    since = settings.last_curated_at
    changed = since is None or any(e.updated_at is not None and e.updated_at > since for e in kept)
    if changed and len(kept) > 1:
        resolved = await resolve_agent_llm(deps, agent)
        taken: set[str] = set()
        for older, newer in await _pairs(deps, agent, kept):
            if resolved is None or older.name in taken or newer.name in taken:
                continue
            verdict = await _confirm(*resolved, older, newer)
            if verdict is not None:
                merges.append((older, newer, verdict))
                taken.update((older.name, newer.name))

    report = CurationReport()
    if not gone and not merges:
        return report

    # 2. snapshot, then change
    path = f"snapshots/{now.astimezone(UTC).strftime('%Y%m%d-%H%M%S')}"
    snapshot = {
        "taken_at": now.isoformat(),
        "entries": [{"path": f"{e.status}/{e.name}", "meta": e.meta, "body": e.as_dict()["body"], "description": e.description} for e in entries],
    }
    await store.write_snapshot(agent.id, path, json.dumps(snapshot, indent=2, default=str))
    report.snapshot_path = path
    actor = f"memory:{agent.id}"
    for older, newer, out in merges:
        try:
            await store.edit(agent.id, older.name, description=out.description or older.description, body=_merged_body(out), actor=actor)
        except ValueError:   # the merged body is over the cap: the pair stays as it is
            continue
        await store.archive(agent.id, newer.name, actor=actor, reason=f"merged into {older.name}", merged_into=older.name)
        report.merged += 1
    for name, reason in gone.items():
        await store.archive(agent.id, name, actor=actor, reason=reason)
        report.archived += 1
    await deps.recorder.emit(
        "memory.curated", payload={"merged": report.merged, "archived": report.archived, "snapshot": path}, actor=actor, entity_kind="agent",
        entity_id=agent.id,
    )
    return report


__all__ = ["CANDIDATE_DAYS", "MERGE_SIMILARITY", "CurationReport", "MergeOutput", "curate"]
