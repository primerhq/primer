"""The template routes serve a url file source without its password and keep it through a PUT (ticket 01a11d32).

``GET /v1/workspace_templates`` is a ``user_dep`` route, so every user used to read ``user:password@`` of a ``kind=url`` file source in clear (and in the ``POST``/``PUT`` responses). It
serves ``https://reader:**********@host/seed.txt`` now (a lone ``https://TOKEN@host`` is masked whole), the stored row keeps the real URL, a full-replace ``PUT`` of the served body keeps the
stored password, and the same body sent for another host or user is a 422 that stores nothing. (A copy of the served body under a new id is refused by the generic create check of the
masked-secrets origin PR, not here; the fetch at materialisation reading the stored URL is ``tests/workspace/test_file_source_url_userinfo.py``.)
"""

from __future__ import annotations

import pytest

from primer.model.workspace import WorkspaceTemplate

MASK = "**********"
URL = "https://reader:s3cr3t@files.example.com/seed.txt"
MASKED = f"https://reader:{MASK}@files.example.com/seed.txt"


def _template(url: str = URL, row_id: str = "tpl-a", description: str = "d") -> dict:
    return {
        "id": row_id, "provider_id": "p-loc", "description": description,
        "files": [{"path": "seed.txt", "source": {"kind": "url", "url": url}}],
    }


def _served_url(body: dict) -> str:
    return body["files"][0]["source"]["url"]


async def _stored(app, row_id: str = "tpl-a") -> WorkspaceTemplate:
    return await app.state.storage_provider.get_storage(WorkspaceTemplate).get(row_id)


@pytest.mark.asyncio
async def test_post_get_and_list_serve_the_url_without_its_password(client) -> None:
    created = await client.post("/v1/workspace_templates", json=_template())
    assert created.status_code in (200, 201), created.text

    one = await client.get("/v1/workspace_templates/tpl-a")
    listed = await client.get("/v1/workspace_templates")

    for r in (created, one, listed):
        assert "s3cr3t" not in r.text, r.text
    assert _served_url(created.json()) == MASKED and _served_url(one.json()) == MASKED
    assert [_served_url(item) for item in listed.json()["items"] if item["id"] == "tpl-a"] == [MASKED]


@pytest.mark.asyncio
async def test_the_stored_row_keeps_the_real_url(client, app) -> None:
    await client.post("/v1/workspace_templates", json=_template())

    assert str((await _stored(app)).files[0].source.url) == URL


@pytest.mark.asyncio
async def test_a_lone_token_in_the_username_slot_is_served_masked_whole(client) -> None:
    await client.post("/v1/workspace_templates", json=_template("https://ghp_abcdefghij@files.example.com/seed.txt"))

    r = await client.get("/v1/workspace_templates/tpl-a")

    assert _served_url(r.json()) == f"https://{MASK}@files.example.com/seed.txt" and "ghp_abcdefghij" not in r.text


@pytest.mark.asyncio
async def test_a_put_of_the_served_body_keeps_the_stored_password(client, app) -> None:
    await client.post("/v1/workspace_templates", json=_template())
    served = (await client.get("/v1/workspace_templates/tpl-a")).json()
    served["description"] = "edited"

    r = await client.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 200, r.text
    assert "s3cr3t" not in r.text
    stored = await _stored(app)
    assert str(stored.files[0].source.url) == URL and stored.description == "edited"


@pytest.mark.asyncio
async def test_a_put_that_changes_the_path_on_the_same_origin_keeps_the_password(client, app) -> None:
    await client.post("/v1/workspace_templates", json=_template())
    served = (await client.get("/v1/workspace_templates/tpl-a")).json()
    served["files"][0]["source"]["url"] = f"https://reader:{MASK}@files.example.com/v2/seed.txt"

    r = await client.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 200, r.text
    assert str((await _stored(app)).files[0].source.url) == "https://reader:s3cr3t@files.example.com/v2/seed.txt"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "moved",
    [
        pytest.param(f"https://reader:{MASK}@attacker.example/seed.txt", id="another host"),
        pytest.param(f"https://other:{MASK}@files.example.com/seed.txt", id="another username"),
        pytest.param(f"http://reader:{MASK}@files.example.com/seed.txt", id="another scheme"),
    ],
)
async def test_a_put_whose_mask_cannot_be_restored_is_a_422_and_stores_nothing(client, app, moved: str) -> None:
    """The stored password is not given to a host the person controls, and the literal mask is not stored as a password either."""
    await client.post("/v1/workspace_templates", json=_template())
    served = (await client.get("/v1/workspace_templates/tpl-a")).json()
    served["files"][0]["source"]["url"] = moved

    r = await client.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 422, r.text
    assert "re-enter the password" in r.text and "s3cr3t" not in r.text
    assert str((await _stored(app)).files[0].source.url) == URL, "the stored row is untouched"


@pytest.mark.asyncio
async def test_a_put_with_a_new_password_stores_it_and_a_put_without_one_removes_it(client, app) -> None:
    await client.post("/v1/workspace_templates", json=_template())
    served = (await client.get("/v1/workspace_templates/tpl-a")).json()
    served["files"][0]["source"]["url"] = "https://reader:newpass@files.example.com/seed.txt"
    r = await client.put("/v1/workspace_templates/tpl-a", json=served)
    assert r.status_code == 200 and "newpass" not in r.text, r.text
    assert str((await _stored(app)).files[0].source.url) == "https://reader:newpass@files.example.com/seed.txt"

    served["files"][0]["source"]["url"] = "https://files.example.com/seed.txt"
    await client.put("/v1/workspace_templates/tpl-a", json=served)

    assert str((await _stored(app)).files[0].source.url) == "https://files.example.com/seed.txt"


@pytest.mark.asyncio
async def test_a_create_with_a_real_password_is_unaffected(client) -> None:
    r = await client.post("/v1/workspace_templates", json=_template("https://a:b@files.example.com/x", row_id="tpl-b"))

    assert r.status_code in (200, 201), r.text
