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

* ``create_agent`` / ``update_agent``     - primer/toolset/_system_crud.py
* ``create_graph`` / ``update_graph``     - primer/toolset/_system_crud.py
* ``create_trigger`` / ``update_trigger`` - primer/toolset/trigger.py
* ``create_python_toolset`` / ``update_python_toolset_source`` /
  ``list_python_tools``                   - primer/toolset/_python_tools.py

Reads are NOT duplicated here: discovery is grep/tree over the system
collection and the existing navigation toolsets.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from primer.model.agent import Agent
from primer.model.chat import Tool
from primer.model.graph import Graph
from primer.toolset._python_tools import build_python_toolset_tools
from primer.toolset._system_crud import _crud_tools_for
from primer.toolset.internal import InternalToolsetProvider, ToolHandler
from primer.toolset.trigger import (
    TOOL_CREATE as _TRIGGER_CREATE,
    TOOL_UPDATE as _TRIGGER_UPDATE,
    _make_create_handler as _make_trigger_create_handler,
    _make_update_handler as _make_trigger_update_handler,
)

if TYPE_CHECKING:
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


def build_crud_toolset(
    *,
    storage_provider: "StorageProvider",
    claim_engine: Any = None,
    event_bus: Any = None,
    toolset_id: str = CRUD_TOOLSET_ID,
) -> InternalToolsetProvider:
    """Construct the immutable ``crud`` toolset."""
    registry: dict[str, tuple[Tool, ToolHandler]] = {}

    for label, plural, model_cls in (
        ("agent", "agents", Agent),
        ("graph", "graphs", Graph),
    ):
        produced = _crud_tools_for(
            entity_label=label,
            entity_label_plural=plural,
            model_cls=model_cls,
            storage_provider=storage_provider,
            required_role="user",
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
            storage_provider=storage_provider, toolset_id=toolset_id,
        )
    )

    logger.info(
        "crud toolset assembled with %d tools (id=%s)", len(registry), toolset_id,
    )
    return InternalToolsetProvider(toolset_id=toolset_id, registry=registry)


__all__ = ["CRUD_TOOLSET_ID", "CRUD_TOOL_NAMES", "build_crud_toolset"]
