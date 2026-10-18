"""The workspace providers a signed-in user may see: id, kind and label, never how to reach them (platform revamp 2026-10, plan 08 task 7).

``GET /v1/workspace_providers`` is admin-only, because a provider row carries its connection: a kubeconfig path, a service-account
token and cluster address, a remote runtime's TLS key, a local root folder. A non-admin still has to name a provider when they write a
template (``provider_id``), and the console's template form shows a "Runs on" menu of them, so ``GET /v1/workspace_providers/_choices``
and the ``workspaces::list_workspace_provider_choices`` tool answer from here with the least that needs: a
:class:`~primer.model.workspace.WorkspaceProviderChoice` per provider, ``id``, ``kind``, ``label`` and ``default_image``, sorted by id.
Both read the rows through :func:`list_provider_choices`, so the route and the tool cannot disagree on what a user sees.

``default_image`` exists for the Docker form: a container provider runs :data:`~primer.model.workspace.DOCKER_RUNTIME_IMAGE` when a
template names no image, and the form pre-fills it (design 20 Q6). Local and Kubernetes have no image to pre-fill, so theirs is ``None``.
"""

from __future__ import annotations

from typing import Any

from primer.model.storage import CursorPage, OrderBy
from primer.model.workspace import (
    DOCKER_RUNTIME_IMAGE,
    WorkspaceProvider,
    WorkspaceProviderChoice,
    WorkspaceProviderChoices,
)

# A walk over every provider asks for full pages of this many rows (the storage layer caps a page at 200).
_PAGE = 200

# What a person calls each kind of container runtime. ``containerd`` is spelled the way its project spells it.
_CONTAINER_LABELS = {"docker": "Docker", "podman": "Podman", "containerd": "containerd"}


def provider_choice(row: WorkspaceProvider) -> WorkspaceProviderChoice:
    """The choice a provider row is listed as: its id, its backend kind, what a person calls it, and the image it pre-fills."""
    kind = row.provider.value
    if kind == "container":
        runtime = row.config.runtime
        return WorkspaceProviderChoice(
            id=row.id, kind="container", label=_CONTAINER_LABELS.get(runtime, runtime), default_image=DOCKER_RUNTIME_IMAGE,
        )
    if kind == "kubernetes":
        return WorkspaceProviderChoice(id=row.id, kind="kubernetes", label="Kubernetes")
    return WorkspaceProviderChoice(id=row.id, kind="local", label="Local")


async def list_provider_choices(storage: Any) -> WorkspaceProviderChoices:
    """Every provider in ``storage`` as a choice, sorted by id.

    The rows are read a page at a time by cursor, so a provider deleted between two pages cannot move another across the boundary
    (an offset walk would skip it), and sorted here as well, so the order does not depend on the backend's collation.
    """
    rows: list[WorkspaceProvider] = []
    cursor: str | None = None
    while True:
        page = await storage.list(CursorPage(cursor=cursor, length=_PAGE), order_by=[OrderBy(field="id", direction="asc")])
        rows.extend(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return WorkspaceProviderChoices(items=sorted((provider_choice(row) for row in rows), key=lambda choice: choice.id))


__all__ = ["list_provider_choices", "provider_choice"]
