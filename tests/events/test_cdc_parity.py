"""CDC as a subscription: storage mutation -> event -> converged page.

The imperative router hooks are gone; this pins the full replacement
pipeline on the real SQLite provider: the storage write emits the CRUD
event, the seeded system-cdc subscription selects it, and the
dispatcher's converge sink brings the system collection page up to
date - including for writes that never pass through a router, which
the old hooks missed.
"""
from __future__ import annotations

import pytest_asyncio

from primer.bootstrap.seed import ensure_system_event_subscriptions
from primer.events.dispatcher import EventDispatcher
from primer.knowledge.system_collection import (
    SYSTEM_COLLECTION_ID,
    regenerate_system_collection,
)
from primer.knowledge.tree import DocumentTreeService
from primer.model.agent import Agent, AgentModel
from primer.model.event import EventSubscription
from primer.model.except_ import NotFoundError
from primer.model.provider import (
    SqliteConfig, StorageProviderConfig, StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory

import pytest

from primer.events.registry import register_event_kind

# In the app process the router factories register the kinds at import
# time; this suite runs without the routers, so mirror the production
# registration explicitly (idempotent).
register_event_kind("agent", Agent)
from primer.model.collection import Document  # noqa: E402

register_event_kind("document", Document)

# asyncio_mode = "auto" in pyproject.toml: async tests need no marker.


@pytest_asyncio.fixture
async def sp(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    await provider.get_event_store().ensure_schema()
    yield provider
    await provider.aclose()


async def test_storage_mutation_converges_via_the_subscription(sp):
    await regenerate_system_collection(sp, toolset_providers={})
    created = await ensure_system_event_subscriptions(sp)
    assert set(created) == {"system-cdc", "system-logger"}

    dispatcher = EventDispatcher(storage_provider=sp)
    await dispatcher.drain_once()  # pin cursors at head

    # Create: never touches a router; the old hooks would have missed it.
    await sp.get_storage(Agent).create(
        Agent(id="agent-ev", description="a converged purpose",
              model=AgentModel(profile_id="prov--m"))
    )
    assert await dispatcher.drain_once() >= 1

    tree = DocumentTreeService(sp)
    page = await tree.read(collection_id=SYSTEM_COLLECTION_ID,
                           path="agents/agent-ev")
    assert "a converged purpose" in page.body
    index = await tree.read(collection_id=SYSTEM_COLLECTION_ID,
                            path="agents")
    assert "agents/agent-ev" in index.body

    # Update converges the new body.
    row = await sp.get_storage(Agent).get("agent-ev")
    await sp.get_storage(Agent).update(
        row.model_copy(update={"description": "rewritten purpose"})
    )
    await dispatcher.drain_once()
    page = await tree.read(collection_id=SYSTEM_COLLECTION_ID,
                           path="agents/agent-ev")
    assert "rewritten purpose" in page.body

    # Delete takes the page away.
    await sp.get_storage(Agent).delete("agent-ev")
    await dispatcher.drain_once()
    with pytest.raises(NotFoundError):
        await tree.read(collection_id=SYSTEM_COLLECTION_ID,
                        path="agents/agent-ev")


async def test_seed_is_idempotent_and_repairs(sp):
    assert len(await ensure_system_event_subscriptions(sp)) == 2
    assert await ensure_system_event_subscriptions(sp) == []
    await sp.get_storage(EventSubscription).delete("system-logger")
    assert await ensure_system_event_subscriptions(sp) == ["system-logger"]


async def test_converge_page_writes_do_not_feed_back(sp):
    """The system-cdc filter excludes document events, so the pages the
    sink writes never re-enter the sink (no recursion, no churn)."""
    await regenerate_system_collection(sp, toolset_providers={})
    await ensure_system_event_subscriptions(sp)
    dispatcher = EventDispatcher(storage_provider=sp)
    await dispatcher.drain_once()

    await sp.get_storage(Agent).create(
        Agent(id="agent-fb", description="feedback probe",
              model=AgentModel(profile_id="prov--m"))
    )
    first = await dispatcher.drain_once()
    assert first >= 1
    # The converge wrote document rows -> document.* events exist, but
    # a second drain delivers nothing new to the cdc sink.
    store = sp.get_event_store()
    document_events = await store.read_after(0, event_type_prefix="document.")
    assert document_events, "page writes should land document events"
    assert await dispatcher.drain_once() == 0
