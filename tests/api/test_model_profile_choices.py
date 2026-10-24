"""``GET /v1/model_profiles/choices``: the model profiles a signed-in user may see (audit G3-02, the lead's ruling).

``POST /v1/agents`` is user tier and an agent must name a model profile, but ``GET /v1/model_profiles`` is admin-only (a profile is provider
configuration), so the console's agent form had no profile to offer a user-role account and its Create button stayed disabled. The choices
route answers every signed-in user with the least that needs: ``id``, ``description``, ``kind`` and ``context_length`` of each profile,
sorted by id and paged like the other list routes, and nothing of the provider, the model name, the members or the config. The literal path
has to win over ``GET /v1/model_profiles/{id}``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport

from primer.auth.passwords import hash_password
from primer.model.model_profile import ModelProfile, ModelProfileConfig
from primer.model.storage import CursorPage, OffsetPage
from primer.model.user import User
from tests._support.agent_check_rows import agent_body
from tests.api.conftest import app, fake_provider_registry  # noqa: F401

_FIELDS = {"id", "description", "kind", "context_length"}


@pytest.fixture
async def admin(app) -> AsyncIterator[httpx.AsyncClient]:  # noqa: F811
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # The first registration is the admin.
        r = await c.post("/v1/auth/register", json={"username": "profileadmin", "password": "profileadminpass1"})
        assert r.status_code == 200, r.text
        yield c


async def _signed_in(app, *, role: str, username: str) -> httpx.AsyncClient:  # noqa: F811
    """A client signed in as a fresh account with ``role``; the caller closes it."""
    await app.state.storage_provider.get_storage(User).create(
        User(
            id=f"user-{username}", username=username, password_hash=await hash_password(f"{username}pass1"),
            created_at=datetime.now(timezone.utc), role=role,
        )
    )
    c = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    r = await c.post("/v1/auth/login", json={"username": username, "password": f"{username}pass1"})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
async def user(app, admin) -> AsyncIterator[httpx.AsyncClient]:  # noqa: F811
    c = await _signed_in(app, role="user", username="profileuser")
    try:
        me = await c.get("/v1/auth/status")
        assert me.json().get("role") == "user", me.text
        yield c
    finally:
        await c.aclose()


@pytest.fixture
async def restricted(app, admin) -> AsyncIterator[httpx.AsyncClient]:  # noqa: F811
    c = await _signed_in(app, role="restricted", username="profilerestricted")
    try:
        yield c
    finally:
        await c.aclose()


def _single(profile_id: str, *, context_length: int = 32_000) -> ModelProfile:
    return ModelProfile(
        id=profile_id, description=f"{profile_id} on a private provider", provider_id="llm-private-provider",
        model_name="private-wire-model", context_length=context_length,
        config=ModelProfileConfig(extended={"x-private-knob": "private-knob-value"}),
    )


async def _seed(app, *rows: ModelProfile) -> None:  # noqa: F811
    for row in rows:
        await app.state.storage_provider.get_storage(ModelProfile).create(row)


async def _all_profile_ids(app) -> list[str]:  # noqa: F811
    page = await app.state.storage_provider.get_storage(ModelProfile).list(OffsetPage(offset=0, length=200))
    return sorted(row.id for row in page.items)


@pytest.mark.asyncio
async def test_a_user_lists_the_profile_choices_while_the_profile_list_stays_admin_only(app, user) -> None:  # noqa: F811
    await _seed(app, _single("zz-single", context_length=8192))

    choices = await user.get("/v1/model_profiles/choices", params={"limit": 200})
    full = await user.get("/v1/model_profiles", params={"limit": 200})

    assert choices.status_code == 200, choices.text
    assert {"id": "zz-single", "description": "zz-single on a private provider", "kind": "single", "context_length": 8192} in choices.json()["items"]
    assert full.status_code == 403, full.text
    assert full.json()["extensions"]["error"] == "forbidden_role"


@pytest.mark.asyncio
async def test_a_choice_is_four_fields_and_serves_nothing_of_the_provider_or_the_config(app, user) -> None:  # noqa: F811
    await _seed(app, _single("zz-single"), ModelProfile(
        id="zz-pool", description="a pool", kind="aggregated", members=["zz-single", "zz-single-2"],
    ), _single("zz-single-2"))

    r = await user.get("/v1/model_profiles/choices", params={"limit": 200})

    assert r.status_code == 200, r.text
    for private in ("llm-private-provider", "private-wire-model", "x-private-knob", "private-knob-value", "members", "strategy"):
        assert private not in r.text, private
    assert all(set(item) == _FIELDS for item in r.json()["items"]), r.json()
    by_id = {item["id"]: item for item in r.json()["items"]}
    assert by_id["zz-pool"] == {"id": "zz-pool", "description": "a pool", "kind": "aggregated", "context_length": None}


@pytest.mark.asyncio
async def test_the_choices_are_every_profile_sorted_by_id(app, user) -> None:  # noqa: F811
    await _seed(app, _single("zz-b"), _single("aa-a"), _single("mm-m"))

    r = await user.get("/v1/model_profiles/choices", params={"limit": 200})

    ids = [item["id"] for item in r.json()["items"]]
    assert ids == await _all_profile_ids(app), "every profile, sorted by id"
    assert {"zz-b", "aa-a", "mm-m"} <= set(ids)


@pytest.mark.asyncio
async def test_the_choices_are_paged_like_the_other_lists(app, user) -> None:  # noqa: F811
    await _seed(app, _single("aa-1"), _single("aa-2"), _single("aa-3"))
    every = await _all_profile_ids(app)

    first = (await user.get("/v1/model_profiles/choices", params={"limit": 2})).json()
    second = (await user.get("/v1/model_profiles/choices", params={"limit": 2, "offset": 2})).json()
    # A cursor walk continues from the cursor a previous cursor page returned (the first page of one is read from the function the route calls).
    from primer.model_profile.choices import list_model_profile_choices

    opening = await list_model_profile_choices(app.state.storage_provider.get_storage(ModelProfile), CursorPage(cursor=None, length=2))
    by_cursor = (await user.get("/v1/model_profiles/choices", params={"limit": 2, "cursor": opening.next_cursor})).json()

    assert (first["kind"], first["offset"], first["length"], first["total"]) == ("offset", 0, 2, len(every))
    assert [i["id"] for i in first["items"]] == every[:2]
    assert [i["id"] for i in second["items"]] == every[2:4]
    assert by_cursor["kind"] == "cursor" and [i["id"] for i in by_cursor["items"]] == every[2:4]
    assert (await user.get("/v1/model_profiles/choices", params={"limit": 201})).status_code == 422
    assert (await user.get("/v1/model_profiles/choices", params={"offset": 1, "cursor": "x"})).status_code == 400


@pytest.mark.asyncio
async def test_a_user_creates_an_agent_on_a_profile_the_choices_list(app, user) -> None:  # noqa: F811
    """The flow the console runs for a user-role account: read the choices, create the agent on one of them."""
    await _seed(app, _single("mp-1"))

    choices = await user.get("/v1/model_profiles/choices", params={"limit": 200})
    assert "mp-1" in [item["id"] for item in choices.json()["items"]]
    created = await user.post("/v1/agents", json=agent_body("ag-user", profile_id="mp-1"))

    assert created.status_code == 201, created.text


@pytest.mark.asyncio
async def test_the_choices_path_is_not_read_as_a_profile_id(app, admin) -> None:  # noqa: F811
    r = await admin.get("/v1/model_profiles/choices")

    assert r.status_code == 200, r.text
    assert isinstance(r.json()["items"], list)


@pytest.mark.asyncio
async def test_an_admin_sees_the_same_choices_as_a_user(app, admin, user) -> None:  # noqa: F811
    await _seed(app, _single("zz-single"))

    params = {"limit": 200}
    assert (await admin.get("/v1/model_profiles/choices", params=params)).json() == (
        await user.get("/v1/model_profiles/choices", params=params)
    ).json()


@pytest.mark.asyncio
async def test_a_user_still_cannot_read_one_profile_by_id(app, user) -> None:  # noqa: F811
    await _seed(app, _single("zz-single"))

    assert (await user.get("/v1/model_profiles/zz-single")).status_code == 403


@pytest.mark.asyncio
async def test_a_restricted_user_gets_no_choices(app, restricted) -> None:  # noqa: F811
    """User tier means a signed-in user with a role that may use the feature routes, as the agent routes."""
    assert (await restricted.get("/v1/model_profiles/choices")).status_code == 403


@pytest.mark.asyncio
async def test_a_signed_out_caller_gets_no_choices(app, admin) -> None:  # noqa: F811
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonymous:
        assert (await anonymous.get("/v1/model_profiles/choices")).status_code == 401
