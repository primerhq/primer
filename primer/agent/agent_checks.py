"""Pre-write checks for :class:`~primer.model.agent.Agent`, shared by the REST router, the system tools and the builder's ``crud`` toolset
(finding A-09, the create/update half).

``POST /v1/agents`` used to answer 201 for an agent whose ``model.profile_id`` and ``tools`` named things that do not exist;
``GET /v1/agents/{id}/status`` then flagged it and the agent failed at its first turn. The rule is the question ``/status`` already
asks, asked before the write instead of after it:

* ``model.profile_id`` must name a stored ModelProfile (code ``model_profile_not_found``, field ``model.profile_id``);
* every toolset a tool id names must resolve (code ``toolset_not_found``, field ``tools``). A tool id is ``<toolset_id>__<tool>``
  (the toolset is the part before the LAST ``__``); an id with no ``__`` is its own toolset. A toolset resolves when it is an
  in-process built-in (``RESERVED_TOOLSET_IDS``, after a retired ``_system``-style alias is mapped to its current id) or a stored
  Toolset row. ``search`` is a stored row, not a built-in.

An UPDATE refuses only a reference it ADDS: an agent that already names a toolset (or a profile) that has since been deleted can
still have its description edited, while pointing it at a missing profile or adding a tool of a missing toolset is refused. Each
check raises :class:`~primer.common.entity_checks.EntityCheckError`; the router's hooks re-raise the 422 shape the profile and
channel routers use for the two REFERENCE codes (the id and description rules below answer a request-validation 422 instead), the tools
answer ``validation-error`` with the field path in front.

The profile is reported before the toolsets, and every missing toolset is named once, sorted, in one message.

Two field rules come first (ticket 01a11c1c; the console's agent form enforced them alone until then):

* a NEW agent's id must match ``AGENT_ID_PATTERN`` (code ``agent_id_invalid``, field ``id``). The id is optional and an omitted one is
  generated as ``agent-<hex>``, which satisfies the rule. It sits in URLs (``/v1/agents/{id}``) and in references (a graph agent node,
  a session binding, a trigger subscription) and is never part of a qualified ``<toolset>__<tool>`` name, so the characters are what a
  URL path needs. Two underscores in a row are RESERVED: a harness install mints ``<slug>__<template>`` ids (``primer/harness/service.py``
  ``resolved_id``), and a REST-created ``acme__assistant`` would collide with a later install of ``acme``. It applies on CREATE only: an
  id is immutable, a deployment may hold ids older than the rule (a ``Legacy_Name``, or an ``old__style``), and the seeded and
  harness-managed agents are written straight to storage, so an install is not subject to it;
* the description must not be blank (code ``agent_description_blank``, field ``description``), on create and on an update that MAKES it
  blank. An agent whose description is already blank can be edited without describing it, like any other reference it already had.

The id is reported before the description. On the route these two answer a request-validation 422 at ``body.id`` / ``body.description``
(see ``AGENT_FIELD_CODES``), the shape the console's field errors read.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING

from primer.access.person import Person
from primer.common.entity_checks import EntityCheckError
from primer.model.agent import Agent
from primer.model.graph import Graph
from primer.model.model_profile import ModelProfile
from primer.model.provider import Toolset

if TYPE_CHECKING:
    from primer.int.storage import Storage
    from primer.int.storage_provider import StorageProvider


# The id of a NEW agent: URL-safe without escaping, never mistaken for something else. ui/components/agents.jsx (AG_validateNewAgent) holds a copy
# and tests/ui/test_agent_form_validation.py fails when the two differ.
AGENT_ID_PATTERN = r"^(?!.*__)[a-z0-9][a-z0-9_-]{0,62}$"
_AGENT_ID_RE = re.compile(AGENT_ID_PATTERN)

# The codes of the two field refusals. The route answers them as request-validation errors at body.<field>; the other codes keep the
# {error, field, message} 422 of the profile and channel routers.
AGENT_FIELD_CODES = frozenset({"agent_id_invalid", "agent_description_blank"})

# Appended to the create and update descriptors of the agent tools (the system toolset and the builder's crud toolset): the model reads
# the rule before it writes.
AGENT_WRITE_NOTE = (
    "A new agent's ``id`` is optional (leave it out to have ``agent-<hex>`` generated); when you send one it must start with a lowercase "
    "letter or a digit and use only lowercase letters, digits, hyphens and single underscores (two underscores in a row, ``__``, are reserved for "
    "the agents a harness installs), at most 63 characters, and an update cannot change it. The ``description`` must not be blank: other agents "
    "find an agent by it."
)


def toolset_id_of(tool_id: str) -> str:
    """The id of the toolset a scoped tool id names: the part before the last ``__``, or the whole id when it has no ``__``."""
    return tool_id.rpartition("__")[0] if "__" in tool_id else tool_id


def _resolved_toolset_id(tool_id: str) -> str:
    """The toolset a tool id resolves to at run time: ``toolset_id_of`` with a retired ``_system``-style alias mapped to its current id."""
    # Function-local: the registry module pulls in the whole provider stack, and this module is imported by the tool factories.
    from primer.api.registries.provider_registry import canonical_toolset_id

    return canonical_toolset_id(toolset_id_of(tool_id))


async def missing_toolset_ids(
    tool_ids: Iterable[str],
    *,
    toolsets: "Storage[Toolset]",
    storage_provider: "StorageProvider | None" = None,
) -> list[str]:
    """The toolsets the tool ids name that resolve to nothing, sorted and each once (a retired alias is reported under its current id).

    One resolution for the pre-write checks and ``GET /v1/agents/{id}/status``, so they cannot disagree about what is dangling.
    """
    from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
    from primer.canvas.settings import read_canvas_settings

    seen: set[str] = set()
    missing: set[str] = set()
    for tool_id in tool_ids:
        toolset_id = _resolved_toolset_id(tool_id)
        if toolset_id in seen:
            continue
        seen.add(toolset_id)
        if toolset_id == "canvas":
            if storage_provider is not None and (await read_canvas_settings(storage_provider)).agent_tools_enabled:
                continue
            missing.add(toolset_id)
            continue
        if toolset_id in RESERVED_TOOLSET_IDS:
            continue
        if await toolsets.get(toolset_id) is None:
            missing.add(toolset_id)
    return sorted(missing)


def _refuse(code: str, field: str, message: str) -> EntityCheckError:
    return EntityCheckError("validation", message, code=code, field=field)


def _shown(agent_id: str) -> str:
    """The id as the message quotes it: a very long one is cut, so a hostile body cannot make a long answer."""
    return repr(agent_id) if len(agent_id) <= 40 else repr(agent_id[:40]) + "..."


def check_agent_fields(entity: Agent, *, existing: Agent | None = None) -> None:
    """The id (a NEW agent only) must be a name, then the description must not be blank (see the module docstring).

    With ``existing`` (an update) the id is not checked, and a blank description is refused only when the stored one was not blank.
    """
    if existing is None and _AGENT_ID_RE.fullmatch(entity.id or "") is None:
        raise _refuse(
            "agent_id_invalid",
            "id",
            f"{_shown(entity.id or '')} is not a valid agent id: it must start with a lowercase letter or a digit and use only "
            "lowercase letters, digits, hyphens and single underscores (two in a row, __, are reserved for the agents a harness installs), "
            "at most 63 characters (for example refund-triage); leave the id out to have one generated",
        )
    if not entity.description.strip() and (existing is None or existing.description.strip()):
        raise _refuse(
            "agent_description_blank",
            "description",
            "the description must not be blank: other agents find an agent by its description",
        )


async def _profile_missing_for(storage_provider: "StorageProvider", person: Person | None, profile_id: str) -> bool:
    """True when ``profile_id`` names no profile, or one the ``person`` saving may not use (the profile and the LLM providers behind it).

    An unusable profile reads exactly like a missing one (spec 9). ``person`` ``None`` checks existence only (internal callers, tests).
    """
    from primer.authz import ProfileUnusable
    from primer.model_profile.resolver import require_profile_use

    row = await storage_provider.get_storage(ModelProfile).get(profile_id)
    if row is None:
        return True
    if person is None:
        return False
    try:
        await require_profile_use(storage_provider, person, row)
    except ProfileUnusable:
        return True
    return False


async def check_profile_exists(entity: Agent, *, storage_provider: "StorageProvider", person: Person | None = None) -> None:
    """``model.profile_id`` must name a stored ModelProfile the saving ``person`` may use (access-control P4 Task 4)."""
    profile_id = entity.model.profile_id
    if await _profile_missing_for(storage_provider, person, profile_id):
        raise _refuse(
            "model_profile_not_found",
            "model.profile_id",
            f"ModelProfile {profile_id!r} does not exist; create the profile first or name an existing one",
        )


async def check_graph_node_profiles(
    entity: Graph, *, storage_provider: "StorageProvider", person: Person | None, existing: Graph | None = None,
) -> None:
    """Every ``profile_id`` an agent node of the graph names must be a profile the saving ``person`` may use; only the ones a save ADDS are checked.

    An unchanged node profile is not re-checked (the run-time check protects every runner), as for an agent's profile.
    """
    already = {getattr(n, "profile_id", None) for n in (existing.nodes if existing is not None else [])}
    for node in entity.nodes:
        profile_id = getattr(node, "profile_id", None)
        if not profile_id or profile_id in already:
            continue
        if await _profile_missing_for(storage_provider, person, profile_id):
            raise _refuse(
                "model_profile_not_found",
                f"nodes.{getattr(node, 'id', '')}.profile_id",
                f"ModelProfile {profile_id!r} does not exist; create the profile first or name an existing one",
            )


async def _unreadable_toolset_ids(tool_ids: Iterable[str], *, storage_provider: "StorageProvider", person: Person) -> list[str]:
    """The toolsets named by ``tool_ids`` whose stored row ``person`` holds no level on, and none of whose named tools is public."""
    from primer.access.policy import level_of, tool_is_public

    named: dict[str, list[str]] = {}
    for tool_id in tool_ids:
        toolset_id = _resolved_toolset_id(tool_id)
        raw = toolset_id_of(tool_id)
        named.setdefault(toolset_id, []).append(tool_id[len(raw) + 2:] if tool_id.startswith(raw + "__") else tool_id)
    hidden: list[str] = []
    for toolset_id, names in named.items():
        row = await storage_provider.get_storage(Toolset).get(toolset_id)
        if row is None or level_of(person, row.access) is not None:
            continue  # built-in (no row), or readable
        if not any(tool_is_public(row.access, name) for name in names):
            hidden.append(toolset_id)
    return hidden


async def check_toolsets_exist(
    entity: Agent, *, storage_provider: "StorageProvider", existing: Agent | None = None, person: Person | None = None,
) -> None:
    """Every toolset the agent's tools name must resolve. With ``existing`` (an update) only the toolsets the update ADDS are checked.

    With a ``person``, a toolset row they hold no level on and none of whose named tools is public counts as missing (spec 9).
    """
    tool_ids = list(entity.tools)
    if existing is not None:
        # A tool id of a toolset the stored agent already names is not new, whether or not that toolset still exists.
        already = {_resolved_toolset_id(t) for t in existing.tools}
        tool_ids = [t for t in tool_ids if _resolved_toolset_id(t) not in already]
    missing = await missing_toolset_ids(
        tool_ids, toolsets=storage_provider.get_storage(Toolset), storage_provider=storage_provider,
    )
    if person is not None and not person.is_admin:
        missing = [*missing, *await _unreadable_toolset_ids(tool_ids, storage_provider=storage_provider, person=person)]
    if missing:
        names = ", ".join(repr(toolset_id) for toolset_id in missing)
        raise _refuse(
            "toolset_not_found",
            "tools",
            f"tools name toolsets that do not exist: {names}; create them first or remove those tools",
        )


async def check_agent_on_create(entity: Agent, *, storage_provider: "StorageProvider", person: Person | None = None) -> None:
    """Every pre-write check for a new agent: the id and the description, then the profile, then the toolsets (both as the saving ``person``)."""
    check_agent_fields(entity)
    await check_profile_exists(entity, storage_provider=storage_provider, person=person)
    await check_toolsets_exist(entity, storage_provider=storage_provider, person=person)


async def check_agent_on_update(
    entity: Agent, existing: Agent, *, storage_provider: "StorageProvider", person: Person | None = None,
) -> None:
    """Every pre-write check for an edit: a profile the agent already had, and toolsets it already named, are not re-checked."""
    check_agent_fields(entity, existing=existing)
    if entity.model.profile_id != existing.model.profile_id:
        await check_profile_exists(entity, storage_provider=storage_provider, person=person)
    await check_toolsets_exist(entity, storage_provider=storage_provider, existing=existing, person=person)


def agent_pre_checks(
    storage_provider: "StorageProvider",
) -> tuple[Callable[[Agent], Awaitable[None]], Callable[[Agent, Agent], Awaitable[None]]]:
    """The ``(pre_create, pre_update)`` pair a tool factory takes, bound to ``storage_provider``."""

    async def pre_create(entity: Agent, *, person: Person | None = None) -> None:
        await check_agent_on_create(entity, storage_provider=storage_provider, person=person)

    async def pre_update(entity: Agent, existing: Agent, *, person: Person | None = None) -> None:
        await check_agent_on_update(entity, existing, storage_provider=storage_provider, person=person)

    # The tool factory passes the calling person to hooks that say they take one (the saver is checked, access-control P4 Task 4).
    pre_create.takes_person = True  # type: ignore[attr-defined]
    pre_update.takes_person = True  # type: ignore[attr-defined]
    return pre_create, pre_update


__all__ = [
    "AGENT_FIELD_CODES",
    "AGENT_ID_PATTERN",
    "AGENT_WRITE_NOTE",
    "agent_pre_checks",
    "check_agent_fields",
    "check_agent_on_create",
    "check_agent_on_update",
    "check_graph_node_profiles",
    "check_profile_exists",
    "check_toolsets_exist",
    "missing_toolset_ids",
    "toolset_id_of",
]
