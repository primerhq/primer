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
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from primer.agent.agent_checks import AGENT_WRITE_NOTE, agent_pre_checks
from primer.common.preview_paths import missing_paths
from primer.model.agent import Agent
from primer.model.chat import Tool
from primer.model.graph import Graph
from primer.toolset._python_tools import build_python_toolset_tools
from primer.toolset._system_crud import _crud_tools_for
from primer.toolset._system_guards import AGENT_GUARDS, GRAPH_GUARDS
from primer.toolset.internal import InternalToolsetProvider, ToolHandler
from primer.toolset.trigger import (
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


# What the approval card of each default-gated tool may show (design note 01a11cd3-66b0). The nine are the tools bootstrap/seed.py gates with a required policy, so
# these are the cards a fresh install shows. Each path names WHAT is created or changed; the free text that can hold a pasted secret is not listed, so it is withheld:
# an agent's system_prompt and compaction_prompt, a graph node's templates and arguments, a webhook trigger's token and hmac_secret, a Python toolset's source.
# "Show all" (the session's own pending-yields route) still returns the whole call to whoever may decide it. Checked against each tool's schema when it is built.
_AGENT_PREVIEW = (
    "entity.id", "entity.description", "entity.model", "entity.temperature", "entity.max_tool_turns", "entity.max_output_tokens", "entity.tools",
    "entity.compaction_tool_access", "entity.allow_external_tools", "entity.response_format", "entity.tts_voice", "entity.harness_id",
)
_GRAPH_PREVIEW = (
    "entity.id", "entity.description", "entity.nodes.kind", "entity.nodes.id", "entity.nodes.description", "entity.nodes.agent_id", "entity.nodes.graph_id",
    "entity.nodes.tool_id", "entity.nodes.profile_id", "entity.edges.kind", "entity.edges.from_node", "entity.edges.to_node", "entity.max_iterations",
    "entity.on_max_iterations", "entity.harness_id",
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
    "create_trigger": ("slug", "name", "description", "enabled", *_TRIGGER_CONFIG_PREVIEW),
    "update_trigger": ("id", "name", "description", "enabled", *_TRIGGER_CONFIG_PREVIEW),
    "create_python_toolset": ("toolset_id", "default_timeout_seconds"),
    "update_python_toolset_source": ("toolset_id",),
    "list_python_tools": ("toolset_id",),
}


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
    # The builder is the principal most likely to name a profile or toolset that does not exist, so its create / update run the
    # same reference check as the route and the system toolset (A-09). A graph has no such pre-write check.
    pre_checks_by_label = {"agent": agent_pre_checks(storage_provider)}

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


__all__ = ["CRUD_TOOLSET_ID", "CRUD_TOOL_NAMES", "PREVIEW_ARGS", "build_crud_toolset"]
