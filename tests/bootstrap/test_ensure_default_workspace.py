"""S5 P3: the default workspace is ensure-seeded, never re-created."""
from __future__ import annotations

from primer.bootstrap.defaults import (
    RESERVED_DEFAULT_WORKSPACE,
    RESERVED_LOCAL_WORKSPACE_TEMPLATE,
    RESERVED_WORKSPACE_TEMPLATES,
)
from primer.bootstrap.seed import ensure_default_workspace
from primer.model.workspace import (
    Workspace,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)


class _LiveWorkspace:
    """Stand-in for the live handle ``materialise`` returns.

    ``runtime_meta`` is a real ``WorkspaceRuntimeMeta`` because the row
    field is required and validated; an empty dict would fail at create.
    """

    id = RESERVED_DEFAULT_WORKSPACE
    runtime_meta = WorkspaceRuntimeMeta(
        url="ws://127.0.0.1:5959", token="stub-token",
    )


class _Registry:
    def __init__(self) -> None:
        self.calls = 0

    async def materialise(self, *, template, overrides=None, workspace_id=None):
        self.calls += 1
        return _LiveWorkspace()


async def _seed_template(sp):
    await sp.get_storage(WorkspaceTemplate).create(
        WorkspaceTemplate(
            **RESERVED_WORKSPACE_TEMPLATES[RESERVED_LOCAL_WORKSPACE_TEMPLATE]
        )
    )


async def test_creates_the_workspace_from_the_local_template(fake_storage_provider):
    await _seed_template(fake_storage_provider)
    registry = _Registry()
    created = await ensure_default_workspace(
        fake_storage_provider, workspace_registry=registry,
    )
    assert created == RESERVED_DEFAULT_WORKSPACE
    row = await fake_storage_provider.get_storage(Workspace).get(
        RESERVED_DEFAULT_WORKSPACE
    )
    assert row.template_id == RESERVED_LOCAL_WORKSPACE_TEMPLATE
    assert row.provider_id == "local"
    assert row.phase == "running"


async def test_second_pass_does_not_materialise_again(fake_storage_provider):
    await _seed_template(fake_storage_provider)
    registry = _Registry()
    await ensure_default_workspace(fake_storage_provider, workspace_registry=registry)
    again = await ensure_default_workspace(
        fake_storage_provider, workspace_registry=registry,
    )
    assert again is None
    assert registry.calls == 1


async def test_missing_template_is_a_no_op(fake_storage_provider):
    registry = _Registry()
    assert await ensure_default_workspace(
        fake_storage_provider, workspace_registry=registry,
    ) is None
    assert registry.calls == 0
