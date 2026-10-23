"""``crud`` reserved internal toolset - platform construction tools.

The builder agent (S5) holds this toolset; the operator deliberately does
not. It re-homes the construction half of the generic CRUD surface plus the
python-toolset management tools under ONE scope, so a single set of
``ToolApprovalPolicy`` rows keyed on ``toolset_id="crud"`` gates every
platform mutation an agent can make.

Nothing here is new behaviour: the handlers are the ones the system and
trigger toolsets already use. Only the scope changes (the misc ->
workspace_ext precedent), which is what makes the approval story
expressible in one place.

Tool catalog (9 tools, none yielding)
-------------------------------------

* ``create_agent`` / ``update_agent``     - primer/toolset/_system_crud.py (managed-row guard: ``AGENT_GUARDS``)
* ``create_graph`` / ``update_graph``     - primer/toolset/_system_crud.py (managed-row guard: ``GRAPH_GUARDS``)
* ``create_trigger`` / ``update_trigger`` - primer/toolset/trigger.py
* ``create_python_toolset`` / ``update_python_toolset_source`` /
  ``list_python_tools``                   - primer/toolset/_python_tools.py

Reads are NOT duplicated here: discovery is grep/tree over the system
collection and the existing navigation toolsets.

The same writes have other doors (the system toolset's agent and graph
tools, the trigger toolset's own, all reachable through
``system__call_tool``). :data:`CRUD_TWINS` names them, and the seeded
policies gate them too (audit G6-1).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from primer.agent.agent_checks import AGENT_WRITE_NOTE, agent_pre_checks
from primer.common.preview_paths import missing_paths
from primer.model.agent import Agent
from primer.model.chat import Tool
from primer.model.graph import Graph
from primer.model.tool_approval import ApproverSpec, PolicyApprovalConfig, RequiredApprovalConfig
from primer.session.approvers import ADMIN_ONLY_SPEC
from primer.toolset._python_tools import build_python_toolset_tools
from primer.toolset._system_common import SYSTEM_TOOLSET_ID
from primer.toolset._system_crud import _crud_tools_for
from primer.toolset._system_guards import AGENT_GUARDS, GRAPH_GUARDS
from primer.toolset.admin_gates import ADMIN_GATES
from primer.toolset.internal import InternalToolsetProvider, ToolHandler
from primer.toolset.trigger import (
    TRIGGER_TOOLSET_ID,
    TOOL_CREATE as _TRIGGER_CREATE,
    TOOL_UPDATE as _TRIGGER_UPDATE,
    _make_create_handler as _make_trigger_create_handler,
    _make_update_handler as _make_trigger_update_handler,
)

if TYPE_CHECKING:
    from primer.api.registries.provider_registry import ProviderRegistry
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


CRUD_TOOLSET_ID = "crud"

CRUD_TOOL_NAMES: tuple[str, ...] = (
    "create_agent",
    "update_agent",
    "create_graph",
    "update_graph",
    "create_trigger",
    "update_trigger",
    "create_python_toolset",
    "update_python_toolset_source",
    "list_python_tools",
)


# The OTHER doors to the writes the crud tools make (audit G6-1): every tool that creates, updates or deletes an agent, a graph or a trigger, keyed
# ``(toolset_id, tool_name)``, with the crud tool that makes the same write (``None`` for a delete: crud has none). The system tools share the crud
# tools' factory and guards, and the trigger tools ARE the handlers crud re-exports, and all are role user and reachable through ``system__call_tool``
# (every seeded agent's door), so a gate on the crud tools alone was a gate a model could walk around. ``bootstrap/seed.py`` seeds a ``required`` policy
# for each, as it does for the crud tools; migration 14 gives an existing install each twin's policy, copied from its crud sibling's as it stands.
CRUD_TWINS: Mapping[tuple[str, str], str | None] = MappingProxyType({
    (SYSTEM_TOOLSET_ID, "create_agent"): "create_agent",
    (SYSTEM_TOOLSET_ID, "update_agent"): "update_agent",
    (SYSTEM_TOOLSET_ID, "delete_agent"): None,
    (SYSTEM_TOOLSET_ID, "create_graph"): "create_graph",
    (SYSTEM_TOOLSET_ID, "update_graph"): "update_graph",
    (SYSTEM_TOOLSET_ID, "delete_graph"): None,
    (TRIGGER_TOOLSET_ID, "create"): "create_trigger",
    (TRIGGER_TOOLSET_ID, "update"): "update_trigger",
    (TRIGGER_TOOLSET_ID, "delete"): None,
})

#: Every ``(toolset_id, tool_name)`` a fresh install gates with an approval policy: the crud tools, their twins, then the admin-tier writes
#: (``primer.toolset.admin_gates``: the policy writes, the toolset writes that run code on the host, the harness lifecycle). :func:`shipped_gate`
#: says which gate each gets.
DEFAULT_GATED_TOOLS: tuple[tuple[str, str], ...] = (
    *((CRUD_TOOLSET_ID, name) for name in CRUD_TOOL_NAMES),
    *CRUD_TWINS,
    *ADMIN_GATES,
)


def shipped_approvers(toolset_id: str, tool_name: str) -> ApproverSpec | None:
    """Who may decide the shipped gate of ``(toolset_id, tool_name)``: an admin only for the admin-tier gates (``admin_gates.ADMIN_GATES``, the
    lead's ruling on #860: a gate on a write only an admin may make, or that runs code on the host, is decided by an admin), ``None`` (whoever
    may decide the session's gates) for the construction gates and every other tool."""
    if (toolset_id, tool_name) not in ADMIN_GATES:
        return None
    return ApproverSpec.model_validate(ADMIN_ONLY_SPEC)


def shipped_gate(toolset_id: str, tool_name: str) -> RequiredApprovalConfig | PolicyApprovalConfig | None:
    """The approval a fresh install gates ``(toolset_id, tool_name)`` with, a new copy each call: ``required``, or the Rego policy of a toolset write
    (``admin_gates.ADMIN_GATES``). ``None`` for a tool that ships ungated. The seed writes it, and ``tool_catalog.default_gate`` reports its type."""
    if (toolset_id, tool_name) not in DEFAULT_GATED_TOOLS:
        return None
    gate = ADMIN_GATES.get((toolset_id, tool_name))
    return gate.model_copy(deep=True) if gate is not None else RequiredApprovalConfig()


# What the approval card of each default-gated tool may show (design note 01a11cd3-66b0). bootstrap/seed.py gates these nine with a required policy, and their twins
# (CRUD_TWINS, which take the same paths through apply_twin_previews), so these are the cards a fresh install shows. Each path names WHAT is created or changed; the free text that can hold a pasted secret is not listed, so it is withheld:
# an agent's system_prompt, compaction_prompt, description and response_format (a schema with free text inside), a graph's and a node's description and templates and
# arguments, a trigger's description, a webhook trigger's token and hmac_secret, a Python toolset's source. A description is free text a person pastes a key into as
# easily as a prompt, so no description is listed.
# "Show all" (the session's own pending-yields route) still returns the whole call to whoever may decide it. Checked against each tool's schema when it is built.
_AGENT_PREVIEW = (
    "entity.id", "entity.model", "entity.temperature", "entity.max_tool_turns", "entity.max_output_tokens", "entity.tools",
    "entity.compaction_tool_access", "entity.allow_external_tools", "entity.tts_voice", "entity.harness_id",
)
_GRAPH_PREVIEW = (
    "entity.id", "entity.nodes.kind", "entity.nodes.id", "entity.nodes.agent_id", "entity.nodes.graph_id", "entity.nodes.tool_id", "entity.nodes.profile_id",
    "entity.edges.kind", "entity.edges.from_node", "entity.edges.to_node", "entity.max_iterations", "entity.on_max_iterations", "entity.harness_id",
)
_TRIGGER_CONFIG_PREVIEW = (
    "config.kind", "config.cron", "config.timezone", "config.catchup", "config.provider_id", "config.channel_id", "config.interactive", "config.fire_at",
    "config.wait_timeout_seconds",
)
PREVIEW_ARGS: dict[str, tuple[str, ...]] = {
    "create_agent": _AGENT_PREVIEW,
    "update_agent": ("id", *_AGENT_PREVIEW),
    "create_graph": _GRAPH_PREVIEW,
    "update_graph": ("id", *_GRAPH_PREVIEW),
    "create_trigger": ("slug", "name", "enabled", *_TRIGGER_CONFIG_PREVIEW),
    "update_trigger": ("id", "name", "enabled", *_TRIGGER_CONFIG_PREVIEW),
    "create_python_toolset": ("toolset_id", "default_timeout_seconds"),
    "update_python_toolset_source": ("toolset_id",),
    "list_python_tools": ("toolset_id",),
}


# A delete names the row it removes and nothing else.
_DELETE_PREVIEW = ("id",)


def twin_preview_args(toolset_id: str, tool_name: str) -> tuple[str, ...] | None:
    """What the approval card of a twin of a crud tool may show (:data:`CRUD_TWINS`): its crud sibling's paths (the arguments are the same), ``id`` for
    a delete. ``None`` for a tool that is not a twin."""
    if (toolset_id, tool_name) not in CRUD_TWINS:
        return None
    sibling = CRUD_TWINS[(toolset_id, tool_name)]
    return PREVIEW_ARGS[sibling] if sibling is not None else _DELETE_PREVIEW


def apply_twin_previews(toolset_id: str, registry: dict[str, tuple[Tool, ToolHandler]]) -> None:
    """Give each twin of a crud tool in ``registry`` (the tools of ``toolset_id``) the card its crud sibling shows, checked against its own schema.

    A default-gated tool declares what its card may show (the crud tools do, below); without it the card of ``system__update_agent`` drew no argument
    at all, where the card of ``crud__update_agent`` names the agent and its model.
    """
    for (twin_toolset, bare), _sibling in CRUD_TWINS.items():
        if twin_toolset != toolset_id or bare not in registry:
            continue
        paths = twin_preview_args(twin_toolset, bare)
        tool, handler = registry[bare]
        gone = missing_paths(tool.args_schema, paths)
        if gone:
            raise ValueError(f"{toolset_id} tool {bare!r}: preview paths {gone} name no argument of its schema")
        registry[bare] = (tool.model_copy(update={"preview_args": paths}), handler)


def build_crud_toolset(
    *,
    storage_provider: "StorageProvider",
    claim_engine: Any = None,
    event_bus: Any = None,
    toolset_id: str = CRUD_TOOLSET_ID,
    provider_registry: "ProviderRegistry | None" = None,
) -> InternalToolsetProvider:
    """Construct the immutable ``crud`` toolset.

    ``provider_registry`` is the registry whose cached toolset adapter ``update_python_toolset_source`` must evict (the app wiring
    passes it; ``None`` for standalone builds).
    """
    registry: dict[str, tuple[Tool, ToolHandler]] = {}
    async def _graph_pre_create(entity: Graph) -> None:
        from primer.knowledge.checks import check_graph_id

        check_graph_id(entity)

    # The builder is the principal most likely to name a profile or toolset that does not exist, so its create / update run the
    # same reference check as the route and the system toolset (A-09).
    pre_checks_by_label: dict[str, tuple[Any, Any]] = {
        "agent": agent_pre_checks(storage_provider),
        "graph": (_graph_pre_create, None),
    }

    for label, plural, model_cls, guards in (
        ("agent", "agents", Agent, AGENT_GUARDS),
        ("graph", "graphs", Graph, GRAPH_GUARDS),
    ):
        pre_create, pre_update = pre_checks_by_label.get(label, (None, None))
        # The factory's default is NO guards, so they are passed here: a builder must not be able to create a row that claims a
        # harness, or edit and release a managed one, through a scope the system toolset's own guards do not cover.
        produced = _crud_tools_for(
            entity_label=label,
            entity_label_plural=plural,
            model_cls=model_cls,
            storage_provider=storage_provider,
            required_role="user",
            guards=guards,
            pre_create=pre_create,
            pre_update=pre_update,
            write_note=AGENT_WRITE_NOTE if label == "agent" else None,
        )
        for bare in (f"create_{label}", f"update_{label}"):
            tool, handler = produced[bare]
            registry[bare] = (
                tool.model_copy(update={"toolset_id": toolset_id}),
                handler,
            )

    registry["create_trigger"] = (
        _TRIGGER_CREATE.model_copy(
            update={"id": "create_trigger", "toolset_id": toolset_id},
        ),
        _make_trigger_create_handler(storage_provider, claim_engine, event_bus),
    )
    registry["update_trigger"] = (
        _TRIGGER_UPDATE.model_copy(
            update={"id": "update_trigger", "toolset_id": toolset_id},
        ),
        _make_trigger_update_handler(storage_provider, claim_engine, event_bus),
    )

    registry.update(
        build_python_toolset_tools(
            storage_provider=storage_provider, toolset_id=toolset_id, provider_registry=provider_registry,
        )
    )

    for bare, paths in PREVIEW_ARGS.items():
        tool, handler = registry[bare]
        gone = missing_paths(tool.args_schema, paths)
        if gone:
            raise ValueError(f"crud tool {bare!r}: PREVIEW_ARGS {gone} name no argument of its schema")
        registry[bare] = (tool.model_copy(update={"preview_args": paths}), handler)

    logger.info(
        "crud toolset assembled with %d tools (id=%s)", len(registry), toolset_id,
    )
    return InternalToolsetProvider(toolset_id=toolset_id, registry=registry)


__all__ = [
    "CRUD_TOOLSET_ID",
    "CRUD_TOOL_NAMES",
    "CRUD_TWINS",
    "DEFAULT_GATED_TOOLS",
    "PREVIEW_ARGS",
    "apply_twin_previews",
    "build_crud_toolset",
    "shipped_approvers",
    "shipped_gate",
    "twin_preview_args",
]
