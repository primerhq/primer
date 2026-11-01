"""PERF-T15 (H6): the live map and the boot pass do not redo work per entity.

Characterization first: the generated pages are the same (a burst settled equals the same changes one by one; the pages of a fixture
install are pinned). Then the counts: index rebuilds per burst, storage reads per boot and per event.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from primer.bootstrap.operator_defaults import operator_agent
from primer.knowledge import system_collection as sc
from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID, converge_entity, regenerate_system_collection
from primer.knowledge.tree import DocumentTreeService
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection, Document
from primer.model.model_profile import ModelProfile
from primer.model.provider import LLMProvider
from tests.knowledge.test_system_collection import sp  # noqa: F401
from tests.knowledge.test_wiki_toggle_and_live_map import _llm

PUBLIC = Access(public=True)
GOLDEN = Path(__file__).parent / "fixtures" / "live_map_golden.json"


def _agent(i: int, profile: str = "prof") -> Agent:
    return Agent(access=PUBLIC, id=f"agent-{i:03d}", description=f"helper {i}", model=AgentModel(profile_id=profile))


async def _snapshot(sp, root: str = "this-install") -> dict[str, list]:
    """Every page under ``root``: path -> [body, title, meta]."""
    tree = DocumentTreeService(sp)
    out: dict[str, list] = {}
    for node in await tree.tree(collection_id=SYSTEM_COLLECTION_ID, parent=root, depth=10):
        page = await tree.read(collection_id=SYSTEM_COLLECTION_ID, path=node.path)
        out[node.path] = [page.body, page.document.title, page.document.meta]
    page = await tree.read(collection_id=SYSTEM_COLLECTION_ID, path=root)
    out[root] = [page.body, page.document.title, page.document.meta]
    return dict(sorted(out.items()))


async def _fixture_install(sp) -> None:
    await sp.get_storage(LLMProvider).create(_llm(Access(owners=["u1"], public=True)))
    await sp.get_storage(ModelProfile).create(ModelProfile(
        id="prof", description="d", provider_id="prov", model_name="m", context_length=1000, access=Access(owners=["u1"], public=True)))
    await sp.get_storage(ModelProfile).create(ModelProfile(
        id="hidden", description="d", provider_id="prov", model_name="m", context_length=1000, access=Access(owners=["u1"])))
    for i in range(5):
        await sp.get_storage(Agent).create(_agent(i, "prof" if i % 2 else "hidden"))
    await sp.get_storage(Agent).create(operator_agent("prof").model_copy(update={"access": PUBLIC}))
    await sp.get_storage(Collection).create(Collection(id="notes", description="my notes", access=PUBLIC))


async def test_the_pages_of_a_fixture_install_are_pinned(sp):  # noqa: F811
    await _fixture_install(sp)
    await regenerate_system_collection(sp, toolset_providers={})
    snap = await _snapshot(sp)
    if not GOLDEN.exists():                                   # captured from the unchanged code
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
    assert snap == json.loads(GOLDEN.read_text())


async def test_a_burst_settled_equals_the_same_changes_one_by_one(sp, tmp_path):  # noqa: F811
    from primer.storage.factory import StorageProviderFactory
    from primer.model.provider import SqliteConfig, StorageProviderConfig, StorageProviderType

    other = StorageProviderFactory.create(StorageProviderConfig(
        provider=StorageProviderType.SQLITE, config=SqliteConfig(path=tmp_path / "o.sqlite")))
    await other.initialize()
    await other.get_content_store().ensure_schema()
    try:
        for store in (sp, other):
            await _fixture_install(store)
            await regenerate_system_collection(store, toolset_providers={})
        ids = [f"agent-{i:03d}" for i in range(10, 40)]
        for store, burst in ((sp, False), (other, True)):
            for i in range(10, 40):
                await store.get_storage(Agent).create(_agent(i))
                await converge_entity(store, entity_type="agent", entity_id=f"agent-{i:03d}", **({"coalesce": True} if burst else {}))
            for i in ids[::3]:                                  # updates and deletes in the same burst
                row = await store.get_storage(Agent).get(i)
                await store.get_storage(Agent).update(row.model_copy(update={"description": "changed"}))
                await converge_entity(store, entity_type="agent", entity_id=i, **({"coalesce": True} if burst else {}))
            for i in ids[1::7]:
                await store.get_storage(Agent).delete(i)
                await converge_entity(store, entity_type="agent", entity_id=i, **({"coalesce": True} if burst else {}))
            if burst:
                await sc.settle()
        assert await _snapshot(sp) == await _snapshot(other)
    finally:
        await other.aclose()


async def test_a_public_private_flip_updates_the_page_after_settling(sp):  # noqa: F811
    await _fixture_install(sp)
    await regenerate_system_collection(sp, toolset_providers={})
    path = "this-install/agents/agent-003"
    tree = DocumentTreeService(sp)
    assert "`prof`" in (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path=path)).body
    profiles = sp.get_storage(ModelProfile)
    await profiles.update((await profiles.get("prof")).model_copy(update={"access": Access(owners=["u1"])}))
    await converge_entity(sp, entity_type="model_profile", entity_id="prof", coalesce=True)
    await sc.settle()
    assert "a private profile" in (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path=path)).body


# --- counts ------------------------------------------------------------------------------------------------------------------------------------

class _Counting:
    def __init__(self, inner, model, counts):
        self._inner, self._model, self._counts = inner, model.__name__, counts

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in ("list", "find", "get", "update", "create", "patch_if") and callable(attr):
            async def counted(*a, **k):
                self._counts[(self._model, name)] += 1
                return await attr(*a, **k)
            return counted
        return attr


def _count(sp) -> Counter:
    counts: Counter = Counter()
    real, cache = sp.get_storage, {}

    def get_storage(model):
        return cache.setdefault(model, _Counting(real(model), model, counts))

    sp.get_storage = get_storage
    return counts


async def test_a_burst_of_100_agents_rebuilds_the_agent_index_once(sp):  # noqa: F811
    await _fixture_install(sp)
    await regenerate_system_collection(sp, toolset_providers={})
    counts = _count(sp)
    for i in range(100, 200):
        await sp.get_storage(Agent).create(_agent(i))
        await converge_entity(sp, entity_type="agent", entity_id=f"agent-{i:03d}", coalesce=True)
    await sc.settle()
    assert counts[("Agent", "list")] <= 3, counts            # one rebuild of the final state, not 100 scans of every agent
    index = await DocumentTreeService(sp).read(collection_id=SYSTEM_COLLECTION_ID, path="this-install/agents")
    assert "agent-199" in index.body and "agent-100" in index.body


async def test_agent_events_read_the_public_profiles_once(sp):  # noqa: F811
    await _fixture_install(sp)
    await regenerate_system_collection(sp, toolset_providers={})
    counts = _count(sp)
    for i in range(100, 130):
        await sp.get_storage(Agent).create(_agent(i))
        await converge_entity(sp, entity_type="agent", entity_id=f"agent-{i:03d}", coalesce=True)
    await sc.settle()
    assert counts[("ModelProfile", "list")] <= 2, counts


async def test_a_boot_pass_over_200_agents_reads_o_pages_not_o_pages_times_children(sp):  # noqa: F811
    await _fixture_install(sp)
    for i in range(100, 300):
        await sp.get_storage(Agent).create(_agent(i))
    await regenerate_system_collection(sp, toolset_providers={})
    counts = _count(sp)
    await regenerate_system_collection(sp, toolset_providers={})   # nothing changed: the second boot only reads
    finds = counts[("Document", "find")]
    assert finds < 100, counts                                      # was ~ one find per child of every page it read


async def test_tree_walks_one_query_per_level(sp):  # noqa: F811
    await _fixture_install(sp)
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    before = await tree.tree(collection_id=SYSTEM_COLLECTION_ID, parent="", depth=10)
    counts = _count(sp)
    tree = DocumentTreeService(sp)
    after = await tree.tree(collection_id=SYSTEM_COLLECTION_ID, parent="", depth=10)
    assert after == before
    assert counts[("Document", "find")] <= 14, counts
