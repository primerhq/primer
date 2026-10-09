"""``POST /v1/harnesses`` refuses a ``git_token`` that is the mask a GET serves (ticket 01a1212a, round 1 of #711, nit N2).

``make_crud_router`` refuses a served mask on every create, but the harness create route is hand-written (the token arrives as a plain string and is wrapped in a ``SecretStr`` by the handler), so
the copy-a-harness move (``GET``, change the slug, ``POST`` the served body) stored ``**********`` as the git token. It is a 422 now and nothing is stored; a real token still works.
"""

from __future__ import annotations

import pytest

BODY = {"name": "Copy", "slug": "copy-of-harness", "git_url": "https://github.com/example/repo"}


@pytest.mark.asyncio
@pytest.mark.parametrize("served", ["**********", "**********cdef"], ids=["the bare mask", "the mask with the last four characters"])
async def test_a_post_that_carries_the_served_token_mask_is_a_422_and_stores_nothing(client, served: str) -> None:
    r = await client.post("/v1/harnesses", json={**BODY, "git_token": served})

    assert r.status_code == 422, r.text
    assert "re-enter" in r.text
    listing = await client.get("/v1/harnesses")
    assert [h["slug"] for h in listing.json()["items"]] == [], "nothing was stored"


@pytest.mark.asyncio
async def test_a_post_of_a_real_token_still_works(client) -> None:
    r = await client.post("/v1/harnesses", json={**BODY, "git_token": "ghp_real_token_0123456789"})

    assert r.status_code == 201, r.text
    assert r.json()["git_token"] == "**********", "served masked, as before"


@pytest.mark.asyncio
async def test_a_post_with_no_token_still_works(client) -> None:
    r = await client.post("/v1/harnesses", json=BODY)

    assert r.status_code == 201, r.text
