"""The harness routes serve a git_url without its password and keep it through an update (ticket 01a11d32, the harness family).

A PAT in a git URL (``https://ghp_xxx@github.com/org/repo``) was readable in clear by every user: the harness reads (list, get) are user-tier, and ``git_token`` beside it was masked. The routes
serve ``https://reader:**********@host/org/repo.git`` now (a lone token masked whole), the stored row keeps the real URL, an update that sends the served URL back keeps the stored credential (the
same scheme, host, port and user; it is not a "move" for the SEC-03 token rule), another host or user answers 422 and stores nothing, and a create that carries the served mask is a 422.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr

from primer.model.harness import Harness, HarnessStatus
from primer.model.storage import OffsetPage
from tests.api.test_require_user_admin import _login, _seed

pytestmark = pytest.mark.asyncio

MASK = "**********"
URL = "https://reader:s3cr3t@git.example.com/org/repo.git"
MASKED = f"https://reader:{MASK}@git.example.com/org/repo.git"
TOKEN_URL = "https://ghp_abcdefghij@github.com/org/repo"


async def _put_harness(app, *, git_url: str = URL, git_token: str | None = None, hid: str = "hns_url00000001") -> Harness:
    harness = Harness(
        id=hid, slug="url-harness", name="Url", git_url=git_url, git_token=SecretStr(git_token) if git_token else None,
        status=HarnessStatus.READY, overrides_schema={"type": "object", "properties": {}}, available_bundle_hash="bh", created_at=datetime.now(timezone.utc),
    )
    await app.state.storage_provider.get_storage(Harness).create(harness)
    return harness


async def _stored(app, hid: str = "hns_url00000001") -> Harness:
    return await app.state.storage_provider.get_storage(Harness).get(hid)


async def test_create_get_and_list_serve_the_url_without_its_password(client, app) -> None:
    created = await client.post("/v1/harnesses", json={"name": "A", "slug": "a-harness", "git_url": URL})
    assert created.status_code == 201, created.text
    hid = created.json()["id"]

    one = await client.get(f"/v1/harnesses/{hid}")
    listed = await client.get("/v1/harnesses")

    for r in (created, one, listed):
        assert "s3cr3t" not in r.text, r.text
    assert created.json()["git_url"] == MASKED and one.json()["git_url"] == MASKED
    assert [h["git_url"] for h in listed.json()["items"] if h["id"] == hid] == [MASKED]
    assert (await _stored(app, hid)).git_url == URL


async def test_a_lone_token_in_the_username_slot_is_served_masked_whole(client) -> None:
    created = await client.post("/v1/harnesses", json={"name": "A", "slug": "a-harness", "git_url": TOKEN_URL})

    assert created.json()["git_url"] == f"https://{MASK}@github.com/org/repo" and "ghp_abcdefghij" not in created.text


async def test_a_user_who_may_only_read_sees_no_password_either(raw_client, app) -> None:
    harness = await _put_harness(app)
    await _seed(app, uid="u-user", username="user1", role="user")
    await _login(raw_client, "user1")

    one = await raw_client.get(f"/v1/harnesses/{harness.id}")
    listed = await raw_client.get("/v1/harnesses")

    assert one.status_code == 200 and listed.status_code == 200
    assert one.json()["git_url"] == MASKED
    assert "s3cr3t" not in one.text + listed.text


async def test_an_update_that_sends_the_served_url_back_keeps_the_password_and_is_not_a_move(client, app) -> None:
    """With a stored token the SEC-03 rule refuses a MOVE of git_url; the served URL sent back is the same remote."""
    harness = await _put_harness(app, git_token="admin-secret-token")
    served = (await client.get(f"/v1/harnesses/{harness.id}")).json()

    r = await client.put(f"/v1/harnesses/{harness.id}", json={"description": "edited", "git_url": served["git_url"], "git_token": served["git_token"]})

    assert r.status_code == 200, r.text
    stored = await _stored(app)
    assert stored.git_url == URL and stored.description == "edited"
    assert stored.git_token.get_secret_value() == "admin-secret-token"
    assert "s3cr3t" not in r.text


async def test_an_update_that_changes_the_path_on_the_same_origin_keeps_the_password(client, app) -> None:
    harness = await _put_harness(app)

    r = await client.put(f"/v1/harnesses/{harness.id}", json={"git_url": f"https://reader:{MASK}@git.example.com/org/other.git"})

    assert r.status_code == 200, r.text
    assert (await _stored(app)).git_url == "https://reader:s3cr3t@git.example.com/org/other.git"


@pytest.mark.parametrize(
    "moved",
    [
        pytest.param(f"https://reader:{MASK}@attacker.example/org/repo.git", id="another host"),
        pytest.param(f"https://other:{MASK}@git.example.com/org/repo.git", id="another username"),
        pytest.param(f"https://reader:{MASK}@git.example.com:8443/org/repo.git", id="another port"),
    ],
)
async def test_an_update_whose_mask_cannot_be_restored_is_a_422_and_stores_nothing(client, app, moved: str) -> None:
    harness = await _put_harness(app)

    r = await client.put(f"/v1/harnesses/{harness.id}", json={"git_url": moved})

    assert r.status_code == 422, r.text
    assert "re-enter the password" in r.text and "s3cr3t" not in r.text
    assert (await _stored(app)).git_url == URL, "the stored row is untouched"


async def test_an_update_with_a_new_password_stores_it_and_one_without_a_credential_removes_it(client, app) -> None:
    harness = await _put_harness(app)
    r = await client.put(f"/v1/harnesses/{harness.id}", json={"git_url": "https://reader:newpass@git.example.com/org/repo.git"})
    assert r.status_code == 200 and "newpass" not in r.text, r.text
    assert (await _stored(app)).git_url == "https://reader:newpass@git.example.com/org/repo.git"

    await client.put(f"/v1/harnesses/{harness.id}", json={"git_url": "https://git.example.com/org/repo.git"})

    assert (await _stored(app)).git_url == "https://git.example.com/org/repo.git"


async def test_a_create_that_carries_the_served_mask_is_a_422_and_stores_nothing(client, app) -> None:
    r = await client.post("/v1/harnesses", json={"name": "Copy", "slug": "copy-harness", "git_url": MASKED})

    assert r.status_code == 422, r.text
    assert "re-enter the password" in r.text
    rows = await app.state.storage_provider.get_storage(Harness).list(OffsetPage(offset=0, length=50))
    assert rows.items == []
