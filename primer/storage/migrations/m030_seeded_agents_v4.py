"""Migration 30: move every unedited seeded agent to the v4 definitions (wiki-first M1).

Wiki-first M1 rewrites the seeded prompts around the wiki loop (the
purpose-mapped index, then wiki_search, then wiki_read, then the exact
scoped tools the page names, stopping at the approval gate and never
exploring the raw REST API or probing the database for a documented
flow) and adds the operator's identity block, a guarded Jinja section
rendered per run against ``ctx.identity``. v3 is frozen in
``primer.bootstrap.seeded_agents_v1`` as ``SEEDED_AGENTS_V3``.

An install seeded before this build carries v3 rows (migration 15 already
moved them there, or migration 11 on a fresh pre-v3 install reads the same,
longer ``PREVIOUS_SHIPPED`` and moves them itself). A row that still equals
one of the shipped definitions - now including v3 (description and prompt
list match, tools as a set) - is rewritten to the current definition,
which is v4 in the build that adds this migration. A row that already
equals the current definition is left without a log. Any other row was
edited: it is left exactly as it is and named in one warning (``POST
/v1/setup/reset_agents`` restores the shipped definition). Migrations 11
and 15 are not edited: they have already run on deployed installs, and
where they have not they read the same, longer ``PREVIOUS_SHIPPED`` and
the live definitions and move the row themselves.

The write is a field-scoped ``patch_if`` of ``description``,
``system_prompt`` and ``tools``, guarded on the stored description, the
only scalar of the three (a ``where`` compares JSON scalars). A
description edited between the read and the write wins, and the row is
named in a warning; an edit of only the prompt or the tools in that window
is not seen by the guard. The model and every other field are untouched.
A row the ensure pass has not created yet (deleted) is skipped: it comes
back at the current definition.

The view redefines ``Agent`` for the usual reason (a migration must read
rows the live model may reject; see m002/m003), and the equality rule is
written out here rather than imported from migration 15, so the two
cannot drift together.
"""

from __future__ import annotations

import logging

from pydantic import ConfigDict, Field

from primer.bootstrap.operator_defaults import SEEDED_AGENT_FACTORIES
from primer.bootstrap.seeded_agents_v1 import PREVIOUS_SHIPPED
from primer.int.storage_provider import StorageProvider
from primer.model.agent import AgentModel
from primer.model.common import Identifiable
from primer.model.except_ import NotFoundError
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


def _left_as_edited(agent_id: str) -> None:
    logger.warning(
        "seeded agent %r was edited; left as it is (POST "
        "/v1/setup/reset_agents restores the shipped definition)",
        agent_id,
    )


class M030SeededAgentsV4:
    """Move every seeded agent that equals a previously shipped definition to the current one."""

    version = 30
    description = "Move every unedited seeded agent to the v4 definitions"

    async def apply(self, sp: StorageProvider) -> None:
        agents = sp.get_storage(Agent)
        upgraded = 0
        for agent_id, factory in SEEDED_AGENT_FACTORIES.items():
            row = await agents.get(agent_id)
            if row is None:
                continue  # the ensure pass creates it at the current definition
            profile_id = row.model.profile_id if row.model is not None else None
            if not profile_id:
                _left_as_edited(agent_id)  # every shipped row carries a profile
                continue
            current = factory(profile_id)
            if _equals_shipped(row, current):
                continue  # already the current definition, nothing to log
            if not any(_equals_shipped(row, prev) for prev in PREVIOUS_SHIPPED.get(agent_id, ())):
                _left_as_edited(agent_id)
                continue
            try:
                written = await agents.patch_if(
                    agent_id,
                    {
                        "description": current.description,
                        "system_prompt": current.system_prompt,
                        "tools": current.tools,
                    },
                    where={"description": [raw_generation(row, "description")]},
                )
            except NotFoundError:
                continue  # deleted since the read: the ensure pass recreates it
            if written is None:
                _left_as_edited(agent_id)  # its description changed since the read
                continue
            upgraded += 1
        if upgraded:
            logger.info("moved seeded agents to the v4 definitions", extra={"count": upgraded})


__all__ = ["M030SeededAgentsV4"]
