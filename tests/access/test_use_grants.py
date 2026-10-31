"""``set_access(use_only=True)``: the shape of grantable infrastructure (access-control P4 Task 3, spec 6.1, 6.2).

No owners, only ``read`` grants (meaning *use*), no per-tool flags; admins hold ``owner`` by role so only they change it; a grantee reads
the view; a stranger answers not found. Run against a model profile and an LLM provider (both grantable kinds).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.access.infra import usable
from primer.access.person import Person
from primer.access.service import AccessUpdate, get_access_view, set_access
from primer.model.access import Access
from primer.model.except_ import AccessDeniedError, ConflictError, NotFoundError, ValidationError
from primer.model.model_profile import ModelProfile
from primer.model.provider import AnthropicConfig, Limits, LLMProvider, LLMProviderType, SqliteConfig
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider

ADMIN, ADA, BOB = Person("root", "admin"), Person("ada", "user"), Person("bob", "user")


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        for uid, role in (("root", "admin"), ("ada", "user"), ("bob", "user")):
            await provider.get_storage(User).create(User(id=uid, username=uid, password_hash=None, created_at=datetime.now(UTC), role=role))
        await provider.get_storage(LLMProvider).create(
            LLMProvider(id="prov", provider=LLMProviderType.ANTHROPIC, config=AnthropicConfig(api_key=SecretStr("k")), limits=Limits(max_concurrency=1))
        )
        await provider.get_storage(ModelProfile).create(
            ModelProfile(id="prof", description="d", provider_id="prov", model_name="m", context_length=1000)
        )
        yield provider
    finally:
        await provider.aclose()


KINDS = [(ModelProfile, "prof"), (LLMProvider, "prov")]


async def _set(sp, model, rid, person, **fields):
    update = AccessUpdate(owners=fields.pop("owners", []), **fields)
    return await set_access(sp.get_storage(model), sp.get_storage(User), rid, person, update, what=f"{model.__name__} {rid!r}", use_only=True)


@pytest.mark.parametrize(("model", "rid"), KINDS)
async def test_an_admin_grants_use_and_a_grantee_reads_the_view(sp, model, rid):
    view = await _set(sp, model, rid, ADMIN, grants={"ada": "read"})
    assert view.owners == [] and [(g.user.id, g.level) for g in view.grants] == [("ada", "read")]

    ada_view = await get_access_view(sp.get_storage(model), sp.get_storage(User), rid, ADA, what="x")
    assert ada_view.your_level == "read"
    with pytest.raises(NotFoundError):
        await get_access_view(sp.get_storage(model), sp.get_storage(User), rid, BOB, what="x")


@pytest.mark.parametrize(("model", "rid"), KINDS)
async def test_use_only_refuses_owners_full_grants_and_public_tools(sp, model, rid):
    with pytest.raises(ValidationError):
        await _set(sp, model, rid, ADMIN, owners=["ada"])
    with pytest.raises(ValidationError):
        await _set(sp, model, rid, ADMIN, grants={"ada": "full"})
    with pytest.raises(ValidationError):
        await _set(sp, model, rid, ADMIN, public_tools=["t"])
    assert (await sp.get_storage(model).get(rid)).access == Access()


@pytest.mark.parametrize(("model", "rid"), KINDS)
async def test_a_non_admin_cannot_change_a_use_grant(sp, model, rid):
    await _set(sp, model, rid, ADMIN, grants={"ada": "read"})
    with pytest.raises(AccessDeniedError):  # a grantee reads it but holds no owner level
        await _set(sp, model, rid, ADA, grants={"ada": "read", "bob": "read"})
    with pytest.raises(NotFoundError):  # a stranger cannot even tell it exists
        await _set(sp, model, rid, BOB, grants={"bob": "read"})


@pytest.mark.parametrize(("model", "rid"), KINDS)
async def test_a_stale_rev_is_a_conflict(sp, model, rid):
    await _set(sp, model, rid, ADMIN, grants={"ada": "read"})
    with pytest.raises(ConflictError):
        await _set(sp, model, rid, ADMIN, grants={}, rev=0)


@pytest.mark.parametrize(("model", "rid"), KINDS)
async def test_usable_follows_the_grant_and_public(sp, model, rid):
    assert await usable(sp, ADMIN, model, rid) is not None  # admins use everything
    assert await usable(sp, ADA, model, rid) is None  # a new row is private
    assert await usable(sp, ADA, model, "missing") is None
    await _set(sp, model, rid, ADMIN, grants={"ada": "read"})
    assert await usable(sp, ADA, model, rid) is not None
    assert await usable(sp, BOB, model, rid) is None
    await _set(sp, model, rid, ADMIN, grants={"ada": "read"}, public=True)
    assert await usable(sp, BOB, model, rid) is not None
    assert await usable(sp, Person("rex", "restricted"), model, rid) is None


@pytest.mark.parametrize(("model", "rid"), KINDS)
async def test_a_grantees_view_has_no_other_grantees_and_an_admins_has_all(sp, model, rid):
    await _set(sp, model, rid, ADMIN, grants={"ada": "read", "bob": "read"}, public=True)
    storage, users = sp.get_storage(model), sp.get_storage(User)

    ada = await get_access_view(storage, users, rid, ADA, what="x", use_only=True)
    assert ada.your_level == "read" and ada.public is True
    assert ada.owners == [] and ada.grants == [] and ada.public_tools == []

    admin = await get_access_view(storage, users, rid, ADMIN, what="x", use_only=True)
    assert sorted(g.user.id for g in admin.grants) == ["ada", "bob"] and admin.rev == 1
