"""A client cannot create a collection under an id the platform reserves (ticket 01a16cad).

``check_collection_id`` checked only the slug format ``[a-z0-9._-]+``, which admits a leading underscore, so ``POST /v1/collections``
answered 201 for ``_internal_agents`` (an id the internal collections bootstrap materialises) and for ``system`` (the platform wiki)
whenever those rows did not exist yet: internal collections disabled, or before the boot wrote them. The bootstrap then met a user row
under its own id. The same check now refuses, on create, every id that starts with ``_internal_`` and the platform wiki's id with a 422
at ``body.id`` whose ``type`` is ``collection_id_reserved``. The platform writes those rows straight to storage, not through this check
(``tests/bootstrap/test_reserved_collection_ids_boot.py``); the tool doors are pinned in
``tests/toolset/test_system_collection_reserved_ids.py``.

Ids are immutable (a PUT needs the stored row and a body id equal to the path id), so only a create can plant one; a row an older install
already stores under a reserved id stays readable and editable.
"""

from __future__ import annotations

import pytest

from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID
from primer.model.collection import Collection
from primer.model.internal import INTERNAL_COLLECTION_IDS

# Re-export so pytest can resolve the sqlite-backed app and client fixtures (the real collection router over a real SQLite store).
from tests.api.test_knowledge_documents_by_path import app, client, provider  # noqa: F401

# Spelled out on purpose: a test that built this list from the code would pass whatever the rule became.
RESERVED_IDS = [
    "_internal_agents", "_internal_graphs", "_internal_collections", "_internal_tools", "_internal_ai_docs",
    "_internal_whatever", "_internal_", "system",
]
# Near misses the rule must not catch: the prefix is ``_internal_`` and the wiki's id is exactly ``system``.
ORDINARY_IDS = ["mine", "internal_agents", "my_internal_notes", "systems", "system-notes", "my-system", "_private"]


def _errors(resp) -> list[tuple[list, str]]:
    return [(e["loc"], e["type"]) for e in resp.json()["extensions"]["errors"]]


def test_the_list_covers_every_id_the_platform_materialises() -> None:
    """A new internal collection, or a renamed wiki, must land in the list above, or the refusal tests below miss it."""
    assert set(INTERNAL_COLLECTION_IDS.values()) <= set(RESERVED_IDS)
    assert SYSTEM_COLLECTION_ID in RESERVED_IDS


@pytest.mark.asyncio
@pytest.mark.parametrize("cid", RESERVED_IDS)
async def test_creating_a_collection_under_a_reserved_id_is_a_named_422_and_stores_nothing(client, provider, cid: str) -> None:
    resp = await client.post("/v1/collections", json={"id": cid, "description": "mine"})

    assert resp.status_code == 422, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert _errors(resp) == [(["body", "id"], "collection_id_reserved")]
    msg = resp.json()["extensions"]["errors"][0]["msg"]
    assert repr(cid) in msg and "reserved for the platform" in msg, msg
    assert await provider.get_storage(Collection).get(cid) is None
    assert (await client.get(f"/v1/collections/{cid}")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("cid", ORDINARY_IDS)
async def test_an_ordinary_id_still_creates_a_user_collection(client, provider, cid: str) -> None:
    resp = await client.post("/v1/collections", json={"id": cid, "description": "mine"})

    assert resp.status_code == 201, resp.text
    stored = await provider.get_storage(Collection).get(cid)
    assert stored is not None and stored.system is False


@pytest.mark.asyncio
async def test_an_omitted_id_is_still_assigned_and_created(client, provider) -> None:
    resp = await client.post("/v1/collections", json={"description": "mine"})

    assert resp.status_code == 201, resp.text
    assert resp.json()["id"].startswith("collection-")
    assert await provider.get_storage(Collection).get(resp.json()["id"]) is not None


@pytest.mark.asyncio
async def test_the_slug_rule_still_answers_first_for_an_id_that_is_not_a_slug(client) -> None:
    """``_INTERNAL_agents`` is not a slug: the existing code, not the new one."""
    resp = await client.post("/v1/collections", json={"id": "_INTERNAL_agents", "description": "mine"})

    assert resp.status_code == 422, resp.text
    assert _errors(resp) == [(["body", "id"], "collection_id_invalid")]


@pytest.mark.asyncio
async def test_an_update_cannot_plant_a_reserved_id_either(client, provider) -> None:
    """Ids are immutable, so create is the only door: a PUT needs the stored row, and its body id must equal the path id."""
    assert (await client.post("/v1/collections", json={"id": "mine", "description": "mine"})).status_code == 201

    no_row = await client.put("/v1/collections/_internal_agents", json={"id": "_internal_agents", "description": "x"})
    renamed = await client.put("/v1/collections/mine", json={"id": "_internal_agents", "description": "x"})

    assert no_row.status_code == 404, no_row.text
    assert renamed.status_code == 409, renamed.text
    assert await provider.get_storage(Collection).get("_internal_agents") is None
    assert (await provider.get_storage(Collection).get("mine")).description == "mine"


@pytest.mark.asyncio
async def test_a_user_row_an_older_install_stored_under_a_reserved_id_stays_readable_and_editable(client, provider) -> None:
    """No migration (lead ruling 4): the rule is about creating, so an existing row is read and written as before."""
    await provider.get_storage(Collection).create(Collection(id="_internal_old", description="planted before the rule"))

    got = await client.get("/v1/collections/_internal_old")
    listed = await client.get("/v1/collections?limit=200")
    edited = await client.put("/v1/collections/_internal_old", json={"id": "_internal_old", "description": "edited"})

    assert got.status_code == 200 and got.json()["description"] == "planted before the rule", got.text
    assert "_internal_old" in [row["id"] for row in listed.json()["items"]]
    assert edited.status_code == 200, edited.text
    assert (await provider.get_storage(Collection).get("_internal_old")).description == "edited"
