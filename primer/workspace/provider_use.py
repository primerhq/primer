"""A template names a workspace provider only if its SAVER may use it (access-control P4 Task 5, plan Decision 4).

A workspace create checks the creator at :meth:`WorkspaceRegistry.materialise`; this is the save-time half. A provider that does not exist, or
one the saving person holds no use grant on (and that is not public), is refused in the same words; admins name any.
"""

from __future__ import annotations

from typing import Any

from primer.access.person import Person
from primer.access.policy import level_of
from primer.model.workspace import WorkspaceProvider


async def provider_unusable(storage_provider: Any, person: Person, provider_id: str) -> bool:
    """True when ``provider_id`` names no workspace provider, or one ``person`` may not use. Admins and the system principal use any that exists."""
    if person.is_admin:
        return False
    row = await storage_provider.get_storage(WorkspaceProvider).get(provider_id)
    return row is None or level_of(person, row.access) is None


def provider_unusable_message(provider_id: str) -> str:
    return f"WorkspaceProvider {provider_id!r} does not exist"


__all__ = ["provider_unusable", "provider_unusable_message"]
