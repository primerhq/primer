"""A workspace create needs a USE of the template's provider (access-control P4 Task 5, plan Decision 5).

``WorkspaceRegistry.materialise`` is the one door every create passes (REST, the ``create_workspace`` tool, the per-user default workspace,
the bootstrap seed). The creator must be able to use the provider (a use grant, or public; admins any). An unusable provider answers exactly
like a missing one and nothing is built; ``person=None`` (the seed) checks nothing.
"""

from __future__ import annotations

import pytest

from primer.access.person import Person
from primer.api.registries import WorkspaceRegistry
from primer.model.access import Access
from primer.model.except_ import NotFoundError
from primer.model.workspace import LocalWorkspaceConfig, WorkspaceProvider, WorkspaceProviderType, WorkspaceTemplate
from tests.api.test_workspaces import _FakeBackend, _SP

ADA, ROOT = Person("ada", "user"), Person("root", "admin")


def _provider(provider_id: str, access: Access) -> WorkspaceProvider:
    return WorkspaceProvider(
        id=provider_id, provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/primer-ws-tests"), access=access,
    )


def _template(provider_id: str) -> WorkspaceTemplate:
    return WorkspaceTemplate(id=f"tpl-{provider_id}", description="t", provider_id=provider_id)


@pytest.fixture
async def world():
    sp = _SP()
    for pid, access in (
        ("p-private", Access()),
        ("p-public", Access(public=True)),
        ("p-granted", Access(grants={"ada": "read"}, rev=1)),
    ):
        await sp.get_storage(WorkspaceProvider).create(_provider(pid, access))
    built: list[str] = []

    def factory(provider):
        built.append(provider.id)
        return _FakeBackend(provider)

    return sp, WorkspaceRegistry(sp, factory=factory), built


async def test_create_needs_provider_use(world):
    _, registry, built = world

    row = await registry.create(template=_template("p-public"), workspace_id="w-pub", person=ADA)
    granted = await registry.create(template=_template("p-granted"), workspace_id="w-granted", person=ADA)

    assert row.provider_id == "p-public" and granted.provider_id == "p-granted"
    with pytest.raises(NotFoundError):
        await registry.create(template=_template("p-private"), person=ADA)
    assert "p-private" not in built, "an unusable provider builds nothing"


async def test_unusable_provider_answers_missing(world):
    _, registry, _ = world

    with pytest.raises(NotFoundError) as unusable:
        await registry.create(template=_template("p-private"), person=ADA)
    with pytest.raises(NotFoundError) as missing:
        await registry.create(template=_template("p-nope"), person=ADA)

    assert str(unusable.value).replace("p-private", "X") == str(missing.value).replace("p-nope", "X")


async def test_admins_and_the_seed_use_any_provider(world):
    _, registry, _ = world

    assert (await registry.create(template=_template("p-private"), workspace_id="w-root", person=ROOT)).provider_id == "p-private"
    assert (await registry.create(template=_template("p-private"), person=None)).provider_id == "p-private"  # the bootstrap seed


async def test_the_create_workspace_tool_and_rest_answer_the_same(world):
    from primer.model.principal import PrincipalRef
    from primer.model.workspace import WorkspaceTemplate as T
    from primer.model.yield_ import ToolContext
    from primer.toolset.workspaces import build_workspaces_toolset

    sp, registry, _ = world
    await sp.get_storage(T).create(_template("p-private").model_copy(update={"access": Access(public=True)}))
    toolset = build_workspaces_toolset(storage_provider=sp, workspace_registry=registry)
    ref = PrincipalRef(type="user", id="ada", display="ada", role="user", source="local", user_id="ada")
    ctx = ToolContext(tool_call_id="c", session_id=None, workspace_id=None, initiated_by=ref)

    result = await toolset.call(tool_name="create_workspace", arguments={"template_id": "tpl-p-private"}, ctx=ctx)

    assert result.is_error and "does not exist" in result.output and "WorkspaceProvider" in result.output
