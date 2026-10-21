"""Migration 11: move the five seeded agents to the core kit (pivot P2, task 12).

The seeded agents' per-role grant lists collapse to the core kit: every
capability an agent needs is reached through ``system__call_tool`` and
documented in the platform wiki. An upgraded install's rows still carry
the old grants; this rewrites them in place, once.

A row is upgraded only while it still equals a definition this lineage
shipped (``PREVIOUS_SHIPPED``: description, prompt list and tools as a
set). An operator edit in any field is not drift to be undone, so an
edited row is left exactly as it is and named in one warning (``POST
/v1/setup/reset_agents`` restores the shipped definition). The write is
a field-scoped ``patch_if`` of ``description``, ``system_prompt`` and
``tools`` guarded on the stored description, so an edit landing between
the read and the write wins and keeps its value; the model and every
other field are untouched. A row the ensure pass has not created yet
(deleted) is skipped: it comes back at the current definition.

The view redefines ``Agent`` for the usual reason: a migration must read
rows the live model may reject and must not depend on fields it does not
touch. See m002/m003 for the idiom.
"""

from __future__ import annotations

import logging

from pydantic import ConfigDict, Field

from primer.bootstrap.operator_defaults import SEEDED_AGENT_FACTORIES
from primer.bootstrap.seeded_agents_v1 import PREVIOUS_SHIPPED
from primer.int.storage_provider import StorageProvider
from primer.model.agent import AgentModel
from primer.model.common import Identifiable
from primer.storage import raw_generation

logger = logging.getLogger(__name__)


class Agent(Identifiable):  # noqa: N801 - name selects the storage table
    """Migration-local view: the shipped triple and the stored profile; the rest rides."""

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    description: str = Field(default="")
    model: AgentModel | None = Field(default=None)
    system_prompt: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


def _equals_shipped(row: Agent, definition) -> bool:
    """Shipped equality: description and prompt list match, tools as a set."""
    return (
        row.description == definition.description
        and list(row.system_prompt) == list(definition.system_prompt)
        and set(row.tools) == set(definition.tools)
    )


class M011SeededAgentsCoreKit:
    """Move the five seeded agents to the core kit, in place."""

    version = 11
    description = "Move the five seeded agents to the core kit"

    async def apply(self, sp: StorageProvider) -> None:
        agents = sp.get_storage(Agent)
        upgraded = 0
        for agent_id, factory in SEEDED_AGENT_FACTORIES.items():
            row = await agents.get(agent_id)
            if row is None:
                continue  # the ensure pass creates it at the current definition
            profile_id = row.model.profile_id if row.model is not None else None
            if profile_id:
                v2 = factory(profile_id)
                if _equals_shipped(row, v2):
                    continue  # already the shipped definition, nothing to log
                if any(_equals_shipped(row, prev) for prev in PREVIOUS_SHIPPED[agent_id]):
                    await agents.patch_if(
                        agent_id,
                        {
                            "description": v2.description,
                            "system_prompt": v2.system_prompt,
                            "tools": v2.tools,
                        },
                        where={"description": [raw_generation(row, "description")]},
                    )
                    upgraded += 1
                    continue
            logger.warning(
                "seeded agent %r was edited; left as it is (POST "
                "/v1/setup/reset_agents restores the shipped definition)",
                agent_id,
            )
        if upgraded:
            logger.info("moved seeded agents to the core kit", extra={"count": upgraded})


__all__ = ["M011SeededAgentsCoreKit"]
