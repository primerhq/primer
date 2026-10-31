"""Public workspace providers for suites that save templates as a plain user (access-control P4 Task 5).

A template may name only a workspace provider its saver may use, and one that does not exist reads the same, so a suite that saves templates on
ids it never seeded seeds them here: local providers, public, usable by every full user.
"""

from __future__ import annotations

from primer.model.access import Access
from primer.model.workspace import LocalWorkspaceConfig, WorkspaceProvider, WorkspaceProviderType


async def seed_public_workspace_providers(storage_provider, *ids: str) -> None:
    store = storage_provider.get_storage(WorkspaceProvider)
    for provider_id in ids:
        if await store.get(provider_id) is None:
            await store.create(WorkspaceProvider(
                id=provider_id, provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/primer-ws-tests"),
                access=Access(public=True),
            ))
