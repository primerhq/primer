"""System tools for an agent's memory (agent memory T13): the tool twins of the entry routes of ``primer/api/routers/agent_memory.py``.

Anyone who can read the agent lists its entries; only an owner (an admin included) approves, archives, edits or pins one. An agent the caller cannot
read answers ``not-found``; a reader who is not an owner answers ``forbidden``. The owner check sits inside each handler, as in ``set_entity_access``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError

from primer.access.acting import actor_label
from primer.access.person import person_for_tool
from primer.access.policy import level_of
from primer.memory.entry import apply_edits
from primer.model.access import AccessLevel
from primer.model.agent import Agent
from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.except_ import AccessDeniedError, ConflictError, NotFoundError
from primer.model.except_ import ValidationError as PrimerValidationError
from primer.model.memory import ItemEdit
from primer.model.yield_ import ToolContext
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err, ok
from primer.toolset._system_common import SYSTEM_TOOLSET_ID, _err_from_validation
from primer.toolset.internal import ToolHandler

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


class _ListArgs(BaseModel):
    agent_id: str = Field(..., min_length=1, description="The agent's id.")
    status: Literal["active", "candidates", "archived"] | None = Field(default=None, description="Only the entries in this folder.")


class _EntryArgs(BaseModel):
    agent_id: str = Field(..., min_length=1, description="The agent's id.")
    name: str = Field(..., min_length=1, description="The entry's name (its slug).")


class _EditArgs(_EntryArgs):
    description: str | None = Field(default=None, description="The new one-line description.")
    pinned: bool | None = Field(default=None, description="Pin (true) or unpin (false) an active entry.")
    body: list[ItemEdit] | None = Field(default=None, description="Item-level edits of the steps, pitfalls and example.")


def _refusal(exc: Exception) -> ToolCallResult:
    if isinstance(exc, NotFoundError):
        return err(str(exc), error_type="not-found")
    if isinstance(exc, ConflictError):
        return err(str(exc), error_type="conflict")
    return err(str(exc), error_type="validation-error")


def memory_tools(
    *, storage_provider: "StorageProvider", provider_registry: Any = None, semantic_search_registry: Any = None,
) -> dict[str, tuple[Tool, ToolHandler]]:
    """Build ``list_agent_memory``, ``approve_agent_memory_entry``, ``archive_agent_memory_entry`` and ``edit_agent_memory_entry``."""
    from primer.events.recorder import recorder_for
    from primer.knowledge.document_service import DocumentService
    from primer.memory.store import MemoryStore

    cached: list[Any] = []

    def get_store() -> Any:
        """The entry store, built on first use (a test double provider without a content store never calls these tools)."""
        if cached:
            return cached[0]
        hooks: dict[str, Any] = {}
        if semantic_search_registry is not None and provider_registry is not None:
            from primer.knowledge.indexing import make_document_indexer, make_document_path_rewriter, make_document_unindexer

            hooks = {
                "indexer": make_document_indexer(
                    storage_provider=storage_provider, provider_registry=provider_registry, semantic_search_registry=semantic_search_registry,
                ),
                "unindexer": make_document_unindexer(storage_provider=storage_provider, semantic_search_registry=semantic_search_registry),
                "path_rewriter": make_document_path_rewriter(storage_provider=storage_provider, semantic_search_registry=semantic_search_registry),
            }
        cached.append(MemoryStore(storage_provider, DocumentService(storage_provider, **hooks), recorder_for(storage_provider)))
        return cached[0]

    async def _agent_for(agent_id: str, ctx: ToolContext | None, *, owner: bool) -> tuple[Agent, Any]:
        person = person_for_tool(ctx)
        agent = await storage_provider.get_storage(Agent).get(agent_id)
        level = level_of(person, agent.access) if agent is not None else None
        if agent is None or level is None:
            raise NotFoundError(f"Agent {agent_id!r} does not exist")
        if owner and level is not AccessLevel.OWNER:
            raise AccessDeniedError(f"only an owner of agent {agent_id!r} changes its memory")
        return agent, person

    def _guard(handler):  # noqa: ANN001 - wraps a handler with the shared argument and refusal handling
        async def run(arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolCallResult:
            try:
                return await handler(arguments, ctx)
            except ValidationError as exc:
                return _err_from_validation(exc)
            except AccessDeniedError as exc:
                return err(str(exc), error_type="forbidden")
            except (NotFoundError, ConflictError, PrimerValidationError) as exc:
                return _refusal(exc)

        return run

    async def _entry_or_404(agent_id: str, name: str):
        entry = await get_store().get(agent_id, name)
        if entry is None:
            raise NotFoundError(f"memory entry {name!r} does not exist")
        return entry

    async def _list(arguments: dict[str, Any], ctx: ToolContext | None) -> ToolCallResult:
        args = _ListArgs.model_validate(arguments)
        await _agent_for(args.agent_id, ctx, owner=False)
        return ok({"items": [e.as_dict() for e in await get_store().list_entries(args.agent_id, args.status)]})

    async def _approve(arguments: dict[str, Any], ctx: ToolContext | None) -> ToolCallResult:
        args = _EntryArgs.model_validate(arguments)
        _, person = await _agent_for(args.agent_id, ctx, owner=True)
        return ok((await get_store().approve(args.agent_id, args.name, actor=actor_label(person))).as_dict())

    async def _archive(arguments: dict[str, Any], ctx: ToolContext | None) -> ToolCallResult:
        args = _EntryArgs.model_validate(arguments)
        _, person = await _agent_for(args.agent_id, ctx, owner=True)
        entry = await get_store().archive(args.agent_id, args.name, actor=actor_label(person), reason="archived by an owner")
        return ok(entry.as_dict())

    async def _edit(arguments: dict[str, Any], ctx: ToolContext | None) -> ToolCallResult:
        args = _EditArgs.model_validate(arguments)
        _, person = await _agent_for(args.agent_id, ctx, owner=True)
        actor = actor_label(person)
        entry = await _entry_or_404(args.agent_id, args.name)
        if args.pinned is not None and entry.status != "active":
            raise ConflictError(f"memory entry {entry.name!r} is {entry.status}; only an active entry can be pinned")
        if args.description is not None or args.body is not None:
            try:
                await get_store().edit(
                    args.agent_id, entry.name, description=args.description,
                    body=apply_edits(entry.body, args.body) if args.body is not None else None, actor=actor,
                )
            except ValueError as exc:  # the body is over the cap
                raise PrimerValidationError(str(exc)) from exc
        if args.pinned is not None:
            await get_store().set_pinned(args.agent_id, entry.name, args.pinned, actor=actor)
        return ok((await _entry_or_404(args.agent_id, entry.name)).as_dict())

    def _tool(tool_id: str, purpose: str, when: str, args: type[BaseModel], example: ToolExample) -> Tool:
        return make_tool(
            id=tool_id, toolset_id=SYSTEM_TOOLSET_ID, purpose=purpose, when=when, args_schema=args.model_json_schema(), examples=[example],
            required_role="user", preview_args=("agent_id", "name") if "name" in args.model_fields else ("agent_id",),
        )

    owner_only = " Owners only (type=forbidden otherwise); an agent you cannot read answers type=not-found."
    tools = [
        (_tool(
            "list_agent_memory", "List the playbook entries an agent has learned: active, candidates (awaiting approval or a trial run) and archived.",
            "Use when reviewing what an agent remembers. Needs read access to the agent. Pass status to see one folder.", _ListArgs,
            ToolExample(args={"agent_id": "agent-3f9a1c8d", "status": "candidates"}, returns="``{items: [{name, status, description, body, meta}]}``"),
        ), _list),
        (_tool(
            "approve_agent_memory_entry", "Approve a candidate memory entry so the agent uses it (it becomes active).",
            "Use when an entry waiting for approval is right." + owner_only, _EntryArgs,
            ToolExample(args={"agent_id": "agent-3f9a1c8d", "name": "retry-github-429"}, returns="the entry, now active"),
        ), _approve),
        (_tool(
            "archive_agent_memory_entry", "Archive a memory entry so the agent no longer uses it.",
            "Use when an entry is wrong, stale or unwanted. Archived entries are kept and can be read." + owner_only, _EntryArgs,
            ToolExample(args={"agent_id": "agent-3f9a1c8d", "name": "retry-github-429"}, returns="the entry, now archived"),
        ), _archive),
        (_tool(
            "edit_agent_memory_entry", "Change a memory entry's description or items, or pin it so the curator never merges or archives it.",
            "Use when an entry is almost right. Item edits name a section (steps, pitfalls, example) and an item id. Only an active entry can be "
            "pinned; a body over about 800 tokens answers type=validation-error." + owner_only, _EditArgs,
            ToolExample(args={"agent_id": "agent-3f9a1c8d", "name": "retry-github-429", "description": "Wait, then retry", "pinned": True}, returns="the entry"),
        ), _edit),
    ]
    return {tool.id: (tool, _guard(handler)) for tool, handler in tools}


__all__ = ["memory_tools"]
