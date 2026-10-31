"""A workspace made private leaves the public live map at once (access-control P3 SEC, finding 10).

``converge_entity`` handles the workspace kind, and the seeded ``system-cdc`` subscription listens to the workspace CRUD events (not to
``workspace.file_changed``, which would converge on every file write).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.bootstrap.seed import SYSTEM_CDC_SUBSCRIPTION, ensure_system_event_subscriptions
from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID, converge_entity, regenerate_system_collection
from primer.knowledge.tree import DocumentTreeService
from primer.model.access import Access
from primer.model.event import EventSubscription
from primer.model.except_ import NotFoundError
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from tests.knowledge.test_system_collection import sp  # noqa: F401

PATH = "this-install/workspaces/ws-pub"


def _workspace(access: Access) -> Workspace:
    return Workspace(
        id="ws-pub", name="Public one", template_id="tpl", provider_id="p", created_at=datetime(2026, 1, 1, tzinfo=UTC), phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"), access=access,
    )


async def test_a_workspace_made_private_is_dropped_from_the_live_map(sp) -> None:
    store = sp.get_storage(Workspace)
    await store.create(_workspace(Access(owners=["u1"], public=True)))
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    assert "Public one" in (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path=PATH)).body

    row = await store.get("ws-pub")
    await store.update(row.model_copy(update={"access": Access(owners=["u1"])}))
    assert await converge_entity(sp, entity_type="workspace", entity_id="ws-pub") is True

    with pytest.raises(NotFoundError):
        await tree.read(collection_id=SYSTEM_COLLECTION_ID, path=PATH)
    assert "ws-pub" not in (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="this-install/workspaces")).body


async def test_a_workspace_made_public_joins_the_live_map(sp) -> None:
    store = sp.get_storage(Workspace)
    await store.create(_workspace(Access(owners=["u1"])))
    await regenerate_system_collection(sp, toolset_providers={})

    row = await store.get("ws-pub")
    await store.update(row.model_copy(update={"access": Access(owners=["u1"], public=True)}))
    await converge_entity(sp, entity_type="workspace", entity_id="ws-pub")

    page = await DocumentTreeService(sp).read(collection_id=SYSTEM_COLLECTION_ID, path=PATH)
    assert "Public one" in page.body


async def test_the_cdc_subscription_listens_to_workspace_crud_but_not_file_changes(sp) -> None:
    await ensure_system_event_subscriptions(sp)

    types = (await sp.get_storage(EventSubscription).get(SYSTEM_CDC_SUBSCRIPTION)).filter.event_types

    assert {"workspace.created", "workspace.updated", "workspace.deleted"} <= set(types)
    assert "workspace.*" not in types and "workspace.file_changed" not in types
