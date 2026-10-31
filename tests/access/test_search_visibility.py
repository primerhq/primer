"""Search returns only what the person can read (access-control P5 Task 5).

The per-entity search routes (``POST /v1/{agents,graphs,collections,tools}/search``) stay admin-only; the wiki search and the live map
(``this-install/``) are public, so they list only public rows (private ones leave them the moment they turn private, join when they turn public); and
a list's ``?q`` narrows the readable set, never widens it.
"""

from __future__ import annotations

import pytest

from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID, converge_entity, regenerate_system_collection
from primer.knowledge.tree import DocumentTreeService
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Document
from primer.model.except_ import NotFoundError
from primer.model.model_profile import ModelProfile
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401
from tests.knowledge.test_system_collection import sp  # noqa: F401
from tests.trigger.test_channel_trigger_access import _login, _register_admin, _seed_user

AGENT_PATH = "this-install/agents/agent-x"
PROFILES_PATH = "this-install/providers/model-profiles"


def _agent(agent_id: str, access: Access, description: str = "a distinctive purpose") -> Agent:
    return Agent(id=agent_id, description=description, model=AgentModel(profile_id="prov--m"), access=access)


@pytest.mark.parametrize("plural", ["agents", "graphs", "collections", "tools"])
async def test_entity_search_routes_are_admin_only(client, app, plural) -> None:
    await _register_admin(client)
    await _seed_user(app, "ann")
    await _login(client, "ann")

    r = await client.post(f"/v1/{plural}/search", json={"query": "anything"})

    assert r.status_code in (401, 403), (plural, r.status_code, r.text)


async def test_private_agent_absent_from_live_map_and_wiki_search(sp) -> None:
    await sp.get_storage(Agent).create(_agent("agent-x", Access(owners=["u1"])))
    await sp.get_storage(Agent).create(_agent("agent-pub", Access(owners=["u1"], public=True), "the public one"))

    await regenerate_system_collection(sp, toolset_providers={})

    tree = DocumentTreeService(sp)
    with pytest.raises(NotFoundError):
        await tree.read(collection_id=SYSTEM_COLLECTION_ID, path=AGENT_PATH)
    index = (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="this-install/agents")).body
    assert "agent-pub" in index and "agent-x" not in index
    # The wiki's fulltext search scans the bodies of this collection's documents: the private agent's description is in none of them.
    bodies = [
        await sp.get_content_store().get(d.id)
        for d in (await sp.get_storage(Document).find(
            Predicate(left=FieldRef(name="collection_id"), op=Op.EQ, right=Value(value=SYSTEM_COLLECTION_ID)), OffsetPage(offset=0, length=200),
        )).items
    ]
    assert bodies and not any("a distinctive purpose" in (body or "") for body in bodies)
    assert any("the public one" in (body or "") for body in bodies)


async def test_turning_private_removes_the_live_map_page(sp) -> None:
    store = sp.get_storage(Agent)
    await store.create(_agent("agent-x", Access(owners=["u1"], public=True)))
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    assert "a distinctive purpose" in (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path=AGENT_PATH)).body

    row = await store.get("agent-x")
    await store.update(row.model_copy(update={"access": Access(owners=["u1"])}))
    await converge_entity(sp, entity_type="agent", entity_id="agent-x")

    with pytest.raises(NotFoundError):
        await tree.read(collection_id=SYSTEM_COLLECTION_ID, path=AGENT_PATH)
    assert "agent-x" not in (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="this-install/agents")).body


async def test_turning_public_adds_it(sp) -> None:
    store = sp.get_storage(Agent)
    await store.create(_agent("agent-x", Access(owners=["u1"])))
    await regenerate_system_collection(sp, toolset_providers={})

    row = await store.get("agent-x")
    await store.update(row.model_copy(update={"access": Access(owners=["u1"], public=True)}))
    await converge_entity(sp, entity_type="agent", entity_id="agent-x")

    tree = DocumentTreeService(sp)
    assert "a distinctive purpose" in (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path=AGENT_PATH)).body
    assert "agent-x" in (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="this-install/agents")).body


async def test_private_model_profile_absent_from_live_map(sp) -> None:
    profiles = sp.get_storage(ModelProfile)
    await profiles.create(ModelProfile(id="prof-private", description="d", provider_id="prov", model_name="secret-model", context_length=1000, access=Access()))
    await profiles.create(ModelProfile(id="prof-public", description="d", provider_id="prov", model_name="open-model", context_length=1000, access=Access(public=True)))

    await regenerate_system_collection(sp, toolset_providers={})

    body = (await DocumentTreeService(sp).read(collection_id=SYSTEM_COLLECTION_ID, path=PROFILES_PATH)).body
    assert "prof-public" in body
    assert "prof-private" not in body and "secret-model" not in body


async def test_list_q_returns_only_readable_rows(client, app) -> None:
    await _register_admin(client)
    ann = await _seed_user(app, "ann")
    store = app.state.storage_provider.get_storage(Agent)
    await store.create(_agent("zz-mine", Access(owners=[ann])))
    await store.create(_agent("zz-shared", Access(owners=["u-other"], grants={ann: "read"})))
    await store.create(_agent("zz-public", Access(owners=["u-other"], public=True)))
    await store.create(_agent("zz-secret", Access(owners=["u-other"])))
    await _login(client, "ann")

    r = await client.get("/v1/agents", params={"q": "zz-"})

    assert r.status_code == 200, r.text
    assert sorted(i["id"] for i in r.json()["items"]) == ["zz-mine", "zz-public", "zz-shared"]
