"""Migration 34: move every unedited seeded agent to the v5 definitions (wiki-first W6).

v5 adds the operator's moderator and bookkeeper role text to its prompt (the
moderator flow: plan with the planner, create a separate workspace, launch
the agents or sessions the plan names in it; the bookkeeper flow: survey
every readable workspace and its sessions' running/parked state via
``list_workspaces`` + ``find_sessions``), each pointing at its wiki runbook
(``runbooks/moderate-a-request``, ``runbooks/workspace-status``). The other
four seeded agents are unchanged; their v4 rows already equal the current
definition and are left alone. A row that still equals a previously shipped
definition (the freezes in ``primer.bootstrap.seeded_agents_v1``, now
including v4) is moved to the current one; an edited row is left as it is.

The scalar-only CAS cannot guard the two lists. A description-preserving
guarded write first acquires the row/write lock inside the provider's
transaction. Its returned CURRENT row is rechecked against the snapshot
before the upgrade, on the same connection while the lock is held. A
changed triple rolls back the locking write too. This protects prompt and
tool edits without adding JSON-value comparison rules to storage. Prompt
order matters; tool order, as in shipped equality, does not.
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


class _ChangedSinceRead(Exception):
    """Abort the transaction, including its locking write, on a changed row."""


class M034SeededAgentsV5:
    """Move every seeded agent that equals a previously shipped definition to the current one."""

    version = 34
    description = "Move every unedited seeded agent to the v5 definitions"

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
                async with sp.transaction() as conn:
                    # Both providers keep this write's lock until the outer
                    # transaction ends; thread conn through BOTH writes.
                    locked = await agents.patch_if(
                        agent_id, {"description": row.description},
                        where={"description": [raw_generation(row, "description")]},
                        conn=conn,
                    )
                    if locked is None or not _equals_shipped(locked, row):
                        raise _ChangedSinceRead
                    written = await agents.patch_if(
                        agent_id,
                        {
                            "description": current.description,
                            "system_prompt": current.system_prompt,
                            "tools": current.tools,
                        },
                        where={"description": [raw_generation(locked, "description")]},
                        conn=conn,
                    )
                    if written is None:
                        raise _ChangedSinceRead
            except NotFoundError:
                continue  # deleted since the read: the ensure pass recreates it
            except _ChangedSinceRead:
                _left_as_edited(agent_id)
                continue
            upgraded += 1
        if upgraded:
            logger.info("moved seeded agents to the v5 definitions", extra={"count": upgraded})


__all__ = ["M034SeededAgentsV5"]
