"""Per-user private default workspace (access-control P2 Task 8, spec 6.7).

A full user gets ``ws-<user id>``, made from the default template, owned by the user and private, with the operator
session on it, at their first sign-in as a full user. The call is idempotent and safe under concurrent sign-ins.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from primer.access.person import Person
from primer.bootstrap.defaults import (
    RESERVED_LOCAL_WORKSPACE_TEMPLATE,
    RESERVED_OPERATOR_AGENT,
    RESERVED_WORKSPACE_TEMPLATES,
)
from primer.bootstrap.prime import (
    ensure_main_operator_session,
    ensure_user_default_workspace,
    is_user_default_workspace,
    resolve_default_workspace_id,
)
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta, WorkspaceTemplate
from primer.model.workspace_session import WorkspaceSession

NOW = datetime(2026, 10, 31, tzinfo=UTC)


class _Live:
    def __init__(self, workspace_id):
        self.id = workspace_id
        self.runtime_meta = WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t")


class _Registry:
    def __init__(self) -> None:
        self.calls = 0
        self.persons: list = []

    async def materialise(self, *, template, overrides=None, workspace_id=None, person=None):
        self.calls += 1
        self.persons.append(person)
        await asyncio.sleep(0)  # a yield point, so concurrent sign-ins interleave
        return _Live(workspace_id)


async def _setup(sp) -> _Registry:
    await sp.get_storage(WorkspaceTemplate).create(
        WorkspaceTemplate(**RESERVED_WORKSPACE_TEMPLATES[RESERVED_LOCAL_WORKSPACE_TEMPLATE])
    )
    await sp.get_storage(Agent).create(
        Agent(id=RESERVED_OPERATOR_AGENT, description="op", model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[])
    )
    return _Registry()


async def _user(sp, uid: str, role: str = "user", age: int = 0) -> User:
    user = User(id=uid, username=uid, created_at=NOW.replace(day=1 + age), role=role)
    await sp.get_storage(User).create(user)
    return user


async def _sessions(sp, workspace_id: str) -> list[WorkspaceSession]:
    page = await sp.get_storage(WorkspaceSession).list(OffsetPage(offset=0, length=200))
    return [s for s in page.items if s.workspace_id == workspace_id]


async def test_first_full_sign_in_creates_one_private_default_with_one_operator_session(fake_storage_provider):
    sp = fake_storage_provider
    reg = await _setup(sp)
    user = await _user(sp, "user-ada")

    wid = await ensure_user_default_workspace(sp, reg, user)

    assert wid == "ws-user-ada"
    row = await sp.get_storage(Workspace).get(wid)
    assert row.access.owners == ["user-ada"] and not row.access.public and not row.access.grants
    assert (await sp.get_storage(User).get("user-ada")).default_workspace_id == wid
    sessions = await _sessions(sp, wid)
    assert len(sessions) == 1 and sessions[0].initiated_by.id == "user-ada"


async def test_second_sign_in_reuses_it(fake_storage_provider):
    sp = fake_storage_provider
    reg = await _setup(sp)
    user = await _user(sp, "user-ada")
    first = await ensure_user_default_workspace(sp, reg, user)
    again = await ensure_user_default_workspace(sp, reg, await sp.get_storage(User).get("user-ada"))

    assert again == first and reg.calls == 1
    assert len(await _sessions(sp, first)) == 1


async def test_concurrent_sign_ins_create_one_workspace(fake_storage_provider):
    sp = fake_storage_provider
    reg = await _setup(sp)
    user = await _user(sp, "user-ada")

    ids = await asyncio.gather(*(ensure_user_default_workspace(sp, reg, user) for _ in range(5)))

    assert set(ids) == {"ws-user-ada"} and reg.calls == 1
    assert len(await _sessions(sp, "ws-user-ada")) == 1
    assert len((await sp.get_storage(Workspace).list(OffsetPage(offset=0, length=50))).items) == 1


async def test_a_restricted_or_disabled_user_gets_none_until_promoted(fake_storage_provider):
    sp = fake_storage_provider
    reg = await _setup(sp)
    restricted = await _user(sp, "user-r", role="restricted")

    with pytest.raises(ValueError):
        await ensure_user_default_workspace(sp, reg, restricted)
    assert reg.calls == 0
    assert (await sp.get_storage(Workspace).list(OffsetPage(offset=0, length=5))).items == []

    promoted = restricted.model_copy(update={"role": "user"})
    assert await ensure_user_default_workspace(sp, reg, promoted) == "ws-user-r"


async def test_the_first_admin_adopts_the_unowned_install_default(fake_storage_provider):
    sp = fake_storage_provider
    reg = await _setup(sp)
    await sp.get_storage(Workspace).create(Workspace(
        id="prime", template_id="local-default", provider_id="local", created_at=NOW,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"),
    ))
    await sp.set_default_workspace_id("prime")
    admin = await _user(sp, "user-root", role="admin")
    other = await _user(sp, "user-bob", age=1)

    assert await ensure_user_default_workspace(sp, reg, admin) == "prime"
    assert (await sp.get_storage(Workspace).get("prime")).access.owners == ["user-root"]
    assert await ensure_user_default_workspace(sp, reg, other) == "ws-user-bob"
    assert reg.calls == 1


async def test_resolve_takes_the_person(fake_storage_provider):
    sp = fake_storage_provider
    reg = await _setup(sp)
    await sp.set_default_workspace_id("prime")
    user = await _user(sp, "user-ada")
    await ensure_user_default_workspace(sp, reg, user)

    assert await resolve_default_workspace_id(sp) == "prime"
    assert await resolve_default_workspace_id(sp, Person(None, "system")) == "prime"
    assert await resolve_default_workspace_id(sp, Person(None, "admin")) == "prime"
    assert await resolve_default_workspace_id(sp, Person("user-ada", "user")) == "ws-user-ada"
    assert await resolve_default_workspace_id(sp, Person("user-nobody", "user")) is None


async def test_a_users_default_is_recognised_for_the_delete_refusal(fake_storage_provider):
    sp = fake_storage_provider
    reg = await _setup(sp)
    await ensure_user_default_workspace(sp, reg, await _user(sp, "user-ada"))

    assert await is_user_default_workspace(sp, "ws-user-ada")
    assert not await is_user_default_workspace(sp, "ws-other")


async def test_migration_23_records_the_install_default_as_the_first_admins(tmp_path):
    from primer.model.provider import SqliteConfig
    from primer.storage.migrations.m023_admin_default_workspace import M023AdminDefaultWorkspace
    from primer.storage.sqlite import SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await sp.initialize()
    try:
        await _migration_23_case(sp, M023AdminDefaultWorkspace)
    finally:
        await sp.aclose()


async def _migration_23_case(sp, migration) -> None:
    await sp.get_storage(Workspace).create(Workspace(
        id="primer", template_id="local-default", provider_id="local", created_at=NOW,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"), access=Access(owners=["user-root"]),
    ))
    await _user(sp, "user-late", role="admin", age=5)
    await _user(sp, "user-root", role="admin", age=0)
    await _user(sp, "user-bob", age=1)

    await migration().apply(sp)
    await migration().apply(sp)  # converges

    users = sp.get_storage(User)
    assert (await users.get("user-root")).default_workspace_id == "primer"
    assert (await users.get("user-late")).default_workspace_id is None
    assert (await users.get("user-bob")).default_workspace_id is None


async def test_migration_23_records_it_for_the_oldest_enabled_admin(tmp_path):
    """Final access review F17: a disabled admin, however old, is skipped (as migrations 25 to 27 skip one)."""
    from primer.model.provider import SqliteConfig
    from primer.storage.migrations.m023_admin_default_workspace import M023AdminDefaultWorkspace
    from primer.storage.sqlite import SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await sp.initialize()
    try:
        await sp.get_storage(Workspace).create(Workspace(
            id="primer", template_id="local-default", provider_id="local", created_at=NOW,
            runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"), access=Access(owners=["user-root"]),
        ))
        old = await _user(sp, "user-old", role="admin", age=0)
        await sp.get_storage(User).update(old.model_copy(update={"disabled": True}))
        await _user(sp, "user-root", role="admin", age=1)

        await M023AdminDefaultWorkspace().apply(sp)

        assert (await sp.get_storage(User).get("user-root")).default_workspace_id == "primer"
        assert (await sp.get_storage(User).get("user-old")).default_workspace_id is None
    finally:
        await sp.aclose()


async def test_two_admins_adopting_concurrently_leave_one_owner(fake_storage_provider):
    sp = fake_storage_provider
    reg = await _setup(sp)
    await sp.get_storage(Workspace).create(Workspace(
        id="prime", template_id="local-default", provider_id="local", created_at=NOW,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"),
    ))
    await sp.set_default_workspace_id("prime")
    a = await _user(sp, "user-a", role="admin")
    b = await _user(sp, "user-b", role="admin", age=1)

    ids = await asyncio.gather(
        ensure_user_default_workspace(sp, reg, a), ensure_user_default_workspace(sp, reg, b)
    )

    owners = (await sp.get_storage(Workspace).get("prime")).access.owners
    assert len(owners) == 1 and sorted(ids).count("prime") == 1
    winner = owners[0]
    assert dict(zip(["user-a", "user-b"], ids))[winner] == "prime"
    loser = "user-b" if winner == "user-a" else "user-a"
    assert dict(zip(["user-a", "user-b"], ids))[loser] == f"ws-{loser}"


async def test_an_owned_install_default_is_never_adopted(fake_storage_provider):
    sp = fake_storage_provider
    reg = await _setup(sp)
    await sp.get_storage(Workspace).create(Workspace(
        id="prime", template_id="local-default", provider_id="local", created_at=NOW,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"), access=Access(owners=["user-a"]),
    ))
    await sp.set_default_workspace_id("prime")
    await _user(sp, "user-a", role="admin")
    b = await _user(sp, "user-b", role="admin", age=1)

    assert await ensure_user_default_workspace(sp, reg, b) == "ws-user-b"
    assert (await sp.get_storage(Workspace).get("prime")).access.owners == ["user-a"]


async def test_the_default_workspace_is_materialised_as_the_user(fake_storage_provider):
    """Access-control P4 follow-up: the provider-use and document-mount checks of ``materialise`` apply to the user the workspace is made for."""
    sp = fake_storage_provider
    reg = await _setup(sp)

    await ensure_user_default_workspace(sp, reg, await _user(sp, "user-ada"))

    assert reg.persons == [Person("user-ada", "user")]


class _Backend:
    def __init__(self, provider) -> None:
        pass

    async def initialize(self) -> None:
        return None

    async def create(self, template, *, overrides=None, workspace_id=None, resolvers=None):
        return _Live(workspace_id)


async def _real_registry_world(sp, *, provider_access):
    from primer.api.registries.workspace_registry import WorkspaceRegistry
    from primer.bootstrap.defaults import RESERVED_LOCAL_WORKSPACE_PROVIDER
    from primer.bootstrap.defaults import RESERVED_WORKSPACE_PROVIDERS
    from primer.model.workspace import WorkspaceProvider

    template = WorkspaceTemplate(**RESERVED_WORKSPACE_TEMPLATES[RESERVED_LOCAL_WORKSPACE_TEMPLATE])
    await sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider(**RESERVED_WORKSPACE_PROVIDERS[RESERVED_LOCAL_WORKSPACE_PROVIDER], access=provider_access)
    )
    await sp.get_storage(WorkspaceTemplate).create(template)
    await sp.get_storage(Agent).create(
        Agent(id=RESERVED_OPERATOR_AGENT, description="op", model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[])
    )
    return WorkspaceRegistry(sp, factory=_Backend)


async def test_a_first_full_sign_in_still_creates_the_default_workspace_on_a_public_provider(fake_storage_provider):
    """Pin: the bootstrap publishes the default template's workspace provider, so the use check of the person-aware materialise lets a
    non-admin's first sign-in through (the default workspace and its operator session exist afterwards)."""
    sp = fake_storage_provider
    reg = await _real_registry_world(sp, provider_access=Access(public=True))

    wid = await ensure_user_default_workspace(sp, reg, await _user(sp, "user-ada"))

    assert wid == "ws-user-ada" and (await sp.get_storage(Workspace).get(wid)).access.owners == ["user-ada"]
    assert len(await _sessions(sp, wid)) == 1


async def test_a_private_provider_refuses_a_users_default_workspace_as_a_missing_one(fake_storage_provider):
    from primer.model.except_ import NotFoundError

    sp = fake_storage_provider
    reg = await _real_registry_world(sp, provider_access=Access(owners=["someone-else"]))

    with pytest.raises(NotFoundError):
        await ensure_user_default_workspace(sp, reg, await _user(sp, "user-ada"))
    assert await sp.get_storage(Workspace).get("ws-user-ada") is None


async def test_a_fresh_installs_first_user_gets_a_default_workspace_and_can_use_the_operator_profile(fake_storage_provider):
    """Agreement of P2's per-user default workspace with P4's seed rule (publish the default profile only at first setup): after the setup
    pass a non-admin's first sign-in creates their workspace through the person-aware materialise AND the seeded operator's profile and
    LLM provider are usable by them, so their operator session can run."""
    from pydantic import SecretStr

    from primer.access.person import Person
    from primer.bootstrap.seed import default_profile_id, ensure_seeded_agents
    from primer.model.model_profile import ModelProfile
    from primer.model.provider import AnthropicConfig, Limits, LLMProvider, LLMProviderType
    from primer.model_profile.resolver import require_profile_use

    sp = fake_storage_provider
    sp.get_storage(ModelProfile)._data.clear()  # the fixture seeds profiles of its own; the seed binds agents to the first one it finds
    sp.get_storage(Agent)._data.clear()
    await sp.get_storage(LLMProvider).create(
        LLMProvider(id="prov", provider=LLMProviderType.ANTHROPIC, config=AnthropicConfig(api_key=SecretStr("k")), limits=Limits(max_concurrency=1))
    )
    await sp.get_storage(ModelProfile).create(ModelProfile(id="prof", description="d", provider_id="prov", model_name="m", context_length=1000))
    reg = await _real_registry_world(sp, provider_access=Access(public=True))
    sp.get_storage(Agent)._data.clear()  # let the setup pass write the whole roster for the first time

    await ensure_seeded_agents(sp)
    wid = await ensure_user_default_workspace(sp, reg, await _user(sp, "user-ada"))

    assert (await sp.get_storage(Workspace).get(wid)) is not None and len(await _sessions(sp, wid)) == 1
    await require_profile_use(sp, Person("user-ada", "user"), await sp.get_storage(ModelProfile).get(await default_profile_id(sp)))


@pytest.mark.parametrize(
    "squat_access",
    [
        Access(owners=["user-mallory"], grants={"user-victim": "full"}),  # mallory's row, shared back to the victim
        Access(owners=["user-victim"], grants={"user-mallory": "full"}),  # handed to the victim, mallory kept full
        Access(),  # an unowned row
    ],
)
async def test_a_squatted_default_id_is_never_adopted(fake_storage_provider, squat_access):
    """Final access review F2: a row already holding ``ws-<user id>`` that is not the user's alone and private is not adopted as
    their default: a fresh id is chosen, recorded, and the squatted row is left as it was."""
    sp = fake_storage_provider
    reg = await _setup(sp)
    victim = await _user(sp, "user-victim")
    await _user(sp, "user-mallory", age=1)
    await sp.get_storage(Workspace).create(Workspace(
        id="ws-user-victim", template_id="local-default", provider_id="local", created_at=NOW,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"), access=squat_access,
    ))

    wid = await ensure_user_default_workspace(sp, reg, victim)

    assert wid != "ws-user-victim" and wid.startswith("ws-user-victim")
    assert (await sp.get_storage(User).get("user-victim")).default_workspace_id == wid
    row = await sp.get_storage(Workspace).get(wid)
    assert row.access.owners == ["user-victim"] and not row.access.grants and not row.access.public
    assert len(await _sessions(sp, wid)) == 1 and await _sessions(sp, "ws-user-victim") == []
    assert (await sp.get_storage(Workspace).get("ws-user-victim")).access == squat_access
    assert await ensure_user_default_workspace(sp, reg, await sp.get_storage(User).get("user-victim")) == wid
