"""Upgrade from the pre-publisher version must gate publication without reseeding."""
from __future__ import annotations

import pytest

from primer.bootstrap.seed import default_policy_id, ensure_crud_approval_policies
from primer.model.except_ import ConflictError
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalPolicy
from primer.session.approvers import ADMIN_ONLY_SPEC
from primer.storage.migrations import LATEST_VERSION, MIGRATIONS, run_migrations
from primer.storage.migrations import m035_workspace_publish_gate as migration
from tests.storage.test_m016_admin_run_gates import sp  # noqa: F401 - isolated SQLite fixture


async def test_forward_upgrade_gates_publisher_without_bootstrap_and_is_idempotent(sp):
    await sp.set_schema_version(34)
    assert isinstance(MIGRATIONS[34], migration.M035WorkspacePublishGate)
    assert await run_migrations(sp, is_fresh_install=False) == LATEST_VERSION == 35
    store = sp.get_storage(ToolApprovalPolicy)
    rows = (await store.list(OffsetPage(offset=0, length=100))).items
    assert len(rows) == 1
    row = rows[0]
    assert row.id == default_policy_id('system', 'publish_service') == migration._POLICY_ID
    assert (row.toolset_id, row.tool_name, row.enabled, row.approval.type.value) == ('system', 'publish_service', True, 'required')
    assert row.approvers.model_dump() == ADMIN_ONLY_SPEC == migration._ADMIN_ONLY
    assert row.timeout_seconds is None and row.preview_args is None
    before = row.model_dump()
    await migration.M035WorkspacePublishGate().apply(sp)
    await ensure_crud_approval_policies(sp)
    assert (await store.get(row.id)).model_dump() == before
    assert len((await store.find(None, OffsetPage(offset=0, length=200))).items) > 1
    assert await run_migrations(sp, is_fresh_install=False) == 35


@pytest.mark.parametrize('spelling', ['system', '_system'])
@pytest.mark.parametrize('kind', ['disabled', 'custom'])
async def test_operator_policy_and_unknown_fields_survive_upgrade(sp, spelling, kind):
    store = sp.get_storage(migration.ToolApprovalPolicy)
    row = migration.ToolApprovalPolicy(
        id='operator-policy', toolset_id=spelling, tool_name='publish_service',
        enabled=kind != 'disabled', approval={'type': 'required'} if kind == 'disabled' else {
            'type': 'policy', 'policy': 'package primer.tool_approval\ndefault required := false\n'},
        approvers={'kind': 'users', 'users': ['owner'], 'roles': []}, future_field={'keep': 1},
    )
    await store.create(row)
    await sp.set_schema_version(34)
    await run_migrations(sp, is_fresh_install=False)
    await ensure_crud_approval_policies(sp)
    assert (await store.get(row.id)).model_dump() == row.model_dump()
    assert await store.get(migration._POLICY_ID) is None


async def test_seeded_id_collision_is_never_overwritten(sp):
    store = sp.get_storage(migration.ToolApprovalPolicy)
    row = migration.ToolApprovalPolicy(id=migration._POLICY_ID, toolset_id='misc', tool_name='other', enabled=False)
    await store.create(row)
    await sp.set_schema_version(34)
    with pytest.raises(ConflictError):
        await run_migrations(sp, is_fresh_install=False)
    assert (await sp.get_system_state()).schema_version == 34
    assert (await store.get(row.id)).model_dump() == row.model_dump()


async def test_concurrent_create_winner_is_preserved(sp, monkeypatch):
    store = sp.get_storage(migration.ToolApprovalPolicy)
    create = store.create
    async def winner(entity):
        await create(entity.model_copy(update={'enabled': False, 'winner_marker': 'retained'}))
        raise ConflictError('synthetic concurrent create')
    monkeypatch.setattr(store, 'create', winner)
    await migration.M035WorkspacePublishGate().apply(sp)
    current = await store.get(migration._POLICY_ID)
    assert current.enabled is False and current.winner_marker == 'retained'


async def test_failed_create_does_not_advance_version_and_retry_repairs(sp, monkeypatch):
    store = sp.get_storage(migration.ToolApprovalPolicy)
    create = store.create
    async def fail(entity):
        raise OSError('synthetic storage failure')
    await sp.set_schema_version(34)
    with monkeypatch.context() as patch:
        patch.setattr(store, 'create', fail)
        with pytest.raises(OSError):
            await run_migrations(sp, is_fresh_install=False)
    assert (await sp.get_system_state()).schema_version == 34
    assert await store.get(migration._POLICY_ID) is None
    assert store.create == create
    assert await run_migrations(sp, is_fresh_install=False) == 35
    assert (await store.get(migration._POLICY_ID)).enabled is True


@pytest.mark.parametrize('spelling', ['system', '_system'])
@pytest.mark.parametrize('enabled', [False, True])
async def test_custom_semantic_policy_precedes_unrelated_seed_id_collision(sp, spelling, enabled):
    from primer.bootstrap.seed import require_workspace_publisher_policy
    store = sp.get_storage(ToolApprovalPolicy)
    unrelated = ToolApprovalPolicy(id=migration._POLICY_ID, toolset_id='misc', tool_name='other',
                                   enabled=False, approval={'type': 'required'})
    custom = ToolApprovalPolicy(
        id='operator-publisher', toolset_id=spelling, tool_name='publish_service', enabled=enabled,
        approval={'type': 'required'}, approvers={'kind': 'users', 'users': ['owner'], 'roles': []},
    )
    await store.create(unrelated)
    await store.create(custom)
    await sp.set_schema_version(34)
    assert await run_migrations(sp, is_fresh_install=False) == 35
    await ensure_crud_approval_policies(sp)
    await require_workspace_publisher_policy(sp)
    assert (await store.get(unrelated.id)).model_dump() == unrelated.model_dump()
    assert (await store.get(custom.id)).model_dump() == custom.model_dump()
    assert (await sp.get_system_state()).schema_version == 35


async def test_startup_policy_verification_read_failure_propagates(sp, monkeypatch):
    from primer.bootstrap.seed import require_workspace_publisher_policy
    store = sp.get_storage(ToolApprovalPolicy)
    async def unavailable(*args, **kwargs):
        raise OSError('policy lookup unavailable')
    monkeypatch.setattr(store, 'find', unavailable)
    with pytest.raises(OSError, match='policy lookup unavailable'):
        await require_workspace_publisher_policy(sp)
