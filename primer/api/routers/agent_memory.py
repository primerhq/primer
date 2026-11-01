"""Agent memory REST surface: ``/v1/agents/{id}/memory/settings`` (Task 5), the entries, ``purge`` and ``activity`` (Task 12).

Anyone who can read the agent reads its memory settings and entries; only an owner (an admin included) changes settings, approves, archives,
edits, pins or purges entries, or reads the activity feed. The settings row is created on the first PUT, so a GET before that answers the defaults
(memory off).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, create_model

from primer.access.acting import actor_label
from primer.access.person import Person, person_from_request
from primer.access.policy import level_of, require
from primer.api.deps import get_storage_provider
from primer.api.errors import common_responses
from primer.memory.prompts import DEFAULT_REFLECTION_PROMPT
from primer.memory.settings import EDITABLE, get_settings, save_settings
from primer.model.access import AccessLevel
from primer.model.agent import Agent
from primer.model.except_ import ConflictError, NotFoundError, ValidationError
from primer.model.memory import AgentMemory, ItemEdit

MEMORY_OWNER_ONLY = "memory_owner_only"

#: The body of a PUT: the owner's fields with the row's own bounds and defaults, and nothing else (an unknown or system field is a 422).
MemorySettingsBody = create_model(
    "MemorySettingsBody",
    __config__=ConfigDict(extra="forbid"),
    **{name: (AgentMemory.model_fields[name].annotation, AgentMemory.model_fields[name]) for name in EDITABLE},
)


class MemorySettingsView(AgentMemory):
    """The settings as the console reads them, with the built-in reflection prompt it shows read-only."""

    default_reflection_prompt: str = DEFAULT_REFLECTION_PROMPT


agent_memory_router = APIRouter(tags=["agent-memory"])


async def _load(request: Request, agent_id: str) -> Agent:
    agent = await get_storage_provider(request).get_storage(Agent).get(agent_id)
    if agent is None:
        raise NotFoundError(f"Agent {agent_id!r} does not exist")
    return agent


async def _require_owner(request: Request, agent_id: str, what: str) -> tuple[Agent, Person]:
    """The agent and the caller, who must own it: no level at all is a 404 (the agent does not exist to them), a lower level a 403."""
    agent = await _load(request, agent_id)
    person = person_from_request(request)
    require(person, agent.access, AccessLevel.READ, what=f"Agent {agent_id!r}")
    if level_of(person, agent.access) is not AccessLevel.OWNER:
        raise HTTPException(
            status_code=403, detail={"error": MEMORY_OWNER_ONLY, "message": f"only an owner of agent {agent_id!r} {what}"},
        )
    return agent, person


@agent_memory_router.get(
    "/agents/{agent_id}/memory/settings",
    response_model=MemorySettingsView,
    summary="The agent's memory settings",
    responses=common_responses(404, 500),
)
async def get_memory_settings(request: Request, agent_id: str = Path(..., description="Agent id")) -> MemorySettingsView:
    agent = await _load(request, agent_id)
    require(person_from_request(request), agent.access, AccessLevel.READ, what=f"Agent {agent_id!r}")
    row = await get_settings(get_storage_provider(request), agent_id)
    return MemorySettingsView(**row.model_dump())


@agent_memory_router.put(
    "/agents/{agent_id}/memory/settings",
    response_model=MemorySettingsView,
    summary="Change the agent's memory settings (owners only)",
    responses=common_responses(403, 404, 409, 422, 500),
)
async def put_memory_settings(
    request: Request,
    agent_id: str = Path(..., description="Agent id"),
    body: MemorySettingsBody = Body(...),  # type: ignore[valid-type]
) -> MemorySettingsView:
    agent, person = await _require_owner(request, agent_id, "changes its memory settings")
    state = request.app.state
    registries = type("Registries", (), {
        "provider_registry": getattr(state, "provider_registry", None),
        "semantic_search_registry": getattr(state, "semantic_search_registry", None),
        "claim_engine": getattr(state, "claim_engine", None),
    })()
    saved = await save_settings(
        get_storage_provider(request), registries, agent, AgentMemory(id=agent_id, **body.model_dump()),
        actor=person,
    )
    return MemorySettingsView(**saved.model_dump())


# --- entries (Task 12) -------------------------------------------------------------------------------------------------------------------------


class BodyItem(BaseModel):
    id: str
    text: str


class EntryBodyView(BaseModel):
    steps: list[BodyItem]
    pitfalls: list[BodyItem]
    example: list[BodyItem]


class EntryView(BaseModel):
    name: str
    status: Literal["active", "candidates", "archived"]
    description: str
    body: EntryBodyView
    meta: dict[str, Any]


class EntryList(BaseModel):
    items: list[EntryView]


class EntryPatch(BaseModel):
    """What an owner changes on an entry: any of the three. ``body`` is item-level edits, as the reflection's patches are."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    body: list[ItemEdit] | None = None
    pinned: bool | None = None


class PurgeResult(BaseModel):
    removed: int


class ActivityItem(BaseModel):
    id: int
    type: str
    actor: str
    at: str
    payload: dict[str, Any]


class ActivityList(BaseModel):
    items: list[ActivityItem]


def _view(entry: Any) -> EntryView:
    return EntryView(**entry.as_dict())


def _store(request: Request) -> Any:
    """The entry store over the request's storage, with the same best-effort indexing hooks the document routes use."""
    from primer.api.routers.knowledge import build_document_indexer, build_document_path_rewriter, build_document_unindexer
    from primer.events.recorder import recorder_for
    from primer.knowledge.document_service import DocumentService
    from primer.memory.store import MemoryStore

    sp = get_storage_provider(request)
    # No semantic-search registry (a deployment or test without one) means nothing is indexed, as the settings route already assumes.
    hooks = getattr(request.app.state, "semantic_search_registry", None) is not None
    documents = DocumentService(
        sp, indexer=build_document_indexer(request) if hooks else None, unindexer=build_document_unindexer(request) if hooks else None,
        path_rewriter=build_document_path_rewriter(request) if hooks else None,
    )
    return MemoryStore(sp, documents, recorder_for(sp, getattr(request.app.state, "event_bus", None)))


async def _entry_or_404(store: Any, agent_id: str, name: str) -> Any:
    entry = await store.get(agent_id, name)
    if entry is None:
        raise NotFoundError(f"memory entry {name!r} does not exist")
    return entry


@agent_memory_router.get(
    "/agents/{agent_id}/memory/entries",
    response_model=EntryList,
    summary="The agent's memory entries",
    responses=common_responses(404, 422, 500),
)
async def list_memory_entries(
    request: Request,
    agent_id: str = Path(..., description="Agent id"),
    status: Literal["active", "candidates", "archived"] | None = Query(None, description="Only the entries in this folder"),
) -> EntryList:
    agent = await _load(request, agent_id)
    require(person_from_request(request), agent.access, AccessLevel.READ, what=f"Agent {agent_id!r}")
    return EntryList(items=[_view(e) for e in await _store(request).list_entries(agent_id, status)])


@agent_memory_router.get(
    "/agents/{agent_id}/memory/entries/{name}",
    response_model=EntryView,
    summary="One memory entry",
    responses=common_responses(404, 500),
)
async def get_memory_entry(request: Request, agent_id: str = Path(..., description="Agent id"), name: str = Path(..., description="Entry name")) -> EntryView:
    agent = await _load(request, agent_id)
    require(person_from_request(request), agent.access, AccessLevel.READ, what=f"Agent {agent_id!r}")
    return _view(await _entry_or_404(_store(request), agent_id, name))


@agent_memory_router.patch(
    "/agents/{agent_id}/memory/entries/{name}",
    response_model=EntryView,
    summary="Edit an entry's description or body, or pin it (owners only)",
    responses=common_responses(403, 404, 409, 422, 500),
)
async def patch_memory_entry(
    request: Request,
    agent_id: str = Path(..., description="Agent id"),
    name: str = Path(..., description="Entry name"),
    body: EntryPatch = Body(...),
) -> EntryView:
    from primer.memory.entry import apply_edits

    _, person = await _require_owner(request, agent_id, "edits its memory")
    store, actor = _store(request), actor_label(person)
    entry = await _entry_or_404(store, agent_id, name)
    if entry.status == "archived" and body.pinned is not None:
        raise ConflictError(f"memory entry {entry.name!r} is archived; only an active entry can be pinned")
    if body.description is not None or body.body is not None:
        try:
            await store.edit(
                agent_id, entry.name, description=body.description,
                body=apply_edits(entry.body, body.body) if body.body is not None else None, actor=actor,
            )
        except ValueError as exc:  # the body is over the cap
            raise ValidationError(str(exc)) from exc
    if body.pinned is not None:
        await store.set_pinned(agent_id, entry.name, body.pinned, actor=actor)
    return _view(await _entry_or_404(store, agent_id, entry.name))


@agent_memory_router.post(
    "/agents/{agent_id}/memory/entries/{name}/approve",
    response_model=EntryView,
    summary="Approve a candidate entry: it becomes active (owners only)",
    responses=common_responses(403, 404, 409, 500),
)
async def approve_memory_entry(request: Request, agent_id: str = Path(..., description="Agent id"), name: str = Path(..., description="Entry name")) -> EntryView:
    _, person = await _require_owner(request, agent_id, "approves its memory entries")
    return _view(await _store(request).approve(agent_id, name, actor=actor_label(person)))


@agent_memory_router.post(
    "/agents/{agent_id}/memory/entries/{name}/archive",
    response_model=EntryView,
    summary="Archive an entry (owners only)",
    responses=common_responses(403, 404, 500),
)
async def archive_memory_entry(request: Request, agent_id: str = Path(..., description="Agent id"), name: str = Path(..., description="Entry name")) -> EntryView:
    _, person = await _require_owner(request, agent_id, "archives its memory entries")
    return _view(await _store(request).archive(agent_id, name, actor=actor_label(person), reason="archived by an owner"))


@agent_memory_router.post(
    "/agents/{agent_id}/memory/purge",
    response_model=PurgeResult,
    summary="Remove every memory entry; the collection and the settings stay (owners only)",
    responses=common_responses(403, 404, 500),
)
async def purge_memory(request: Request, agent_id: str = Path(..., description="Agent id")) -> PurgeResult:
    from primer.events.recorder import recorder_for
    from primer.memory.collection import purge_memory_entries

    _, person = await _require_owner(request, agent_id, "purges its memory")
    sp = get_storage_provider(request)
    removed = await purge_memory_entries(sp, agent_id, semantic_search_registry=getattr(request.app.state, "semantic_search_registry", None))
    await recorder_for(sp, getattr(request.app.state, "event_bus", None)).emit(
        "memory.purged", payload={"removed": removed}, actor=actor_label(person), entity_kind="agent", entity_id=agent_id,
    )
    return PurgeResult(removed=removed)


_PAGE = 500


@agent_memory_router.get(
    "/agents/{agent_id}/memory/activity",
    response_model=ActivityList,
    summary="The agent's memory events, newest first (owners only)",
    responses=common_responses(403, 404, 500),
)
async def memory_activity(
    request: Request,
    agent_id: str = Path(..., description="Agent id"),
    after: int = Query(0, ge=0, description="Only events with a greater id"),
    limit: int = Query(50, ge=1, le=200),
) -> ActivityList:
    from collections import deque

    await _require_owner(request, agent_id, "reads its memory activity")
    events = get_storage_provider(request).get_event_store()
    newest: deque[Any] = deque(maxlen=limit)
    cursor = after
    while True:
        page = await events.read_after(cursor, limit=_PAGE, event_type_prefix="memory.", entity_kind="agent", entity_id=agent_id)
        newest.extend(page)
        if len(page) < _PAGE:
            break
        cursor = page[-1].id
    return ActivityList(items=[
        ActivityItem(id=e.id, type=e.event_type, actor=e.actor, at=e.occurred_at.isoformat(), payload=e.payload or {}) for e in reversed(newest)
    ])


__all__ = ["MEMORY_OWNER_ONLY", "MemorySettingsBody", "MemorySettingsView", "agent_memory_router"]
