"""The workspace publisher must park before capture even for an admin run."""
import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.bootstrap.seed import default_policy_id
from primer.mcp.exposure import ExposureDeps, ToolNotExposable, update_exposure
from primer.mcp.safety import is_exposable
from primer.model.service import Service, ServiceVersion
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalPolicy
from primer.model.yield_ import YieldToWorker
from primer.session.approvers import ADMIN_ONLY_SPEC
from primer.storage.migrations import run_migrations
from tests.toolset.test_admin_run_gates import ADMIN, _Session, _call, _world


@pytest.mark.parametrize('kind', ['fresh', 'upgrade34'])
@pytest.mark.parametrize('door', ['direct', 'through-call-tool', 'direct-as-_system', 'through-call-tool-as-_system'])
async def test_admin_publisher_parks_before_capture(tmp_path, kind, door):
    sp, registry, providers, resolver = await _world(tmp_path, 'fresh')
    try:
        if kind == 'upgrade34':
            await sp.get_storage(ToolApprovalPolicy).delete(default_policy_id('system', 'publish_service'))
            await sp.set_schema_version(34)
            await run_migrations(sp, is_fresh_install=False)  # No ensure pass after upgrade.
        manager = ToolExecutionManager.for_workspace(
            toolset_providers=providers, session=_Session(), approval_resolver=resolver,
            provider_registry=registry, initiated_by=ADMIN,
            tools=['system__call_tool', 'system__publish_service', '_system__publish_service'],
        )
        with pytest.raises(YieldToWorker) as parked:
            await manager.execute(_call(door, 'system', 'publish_service', {'name': 'site', 'path': 'apps/site'}))
        meta = parked.value.yielded.resume_metadata
        assert parked.value.yielded.tool_name == '_approval'
        assert meta['policy_id'] == default_policy_id('system', 'publish_service')
        assert meta['approval_type'] == 'required' and meta['approvers'] == ADMIN_ONLY_SPEC
        if 'call-tool' in door:
            assert meta['via_call_tool'] == {'toolset_id': 'system', 'tool_name': 'publish_service'}
        for model in (Service, ServiceVersion):
            assert (await sp.get_storage(model).list(OffsetPage(offset=0, length=10))).items == []
    finally:
        await sp.aclose()


async def test_workspace_publisher_is_not_an_external_mcp_tool(tmp_path):
    sp, registry, providers, resolver = await _world(tmp_path, 'fresh')
    try:
        tool = next(t for t in [t async for t in providers['system'].list_tools()] if t.id == 'publish_service')
        assert tool.required_role == 'admin' and tool.requires_workspace
        assert tool.preview_args == ('name', 'path')
        assert is_exposable(tool, provider=providers['system']) == (False, 'needs_workspace')
        deps = ExposureDeps(storage_provider=sp, provider_registry=registry, approval_resolver=resolver)
        with pytest.raises(ToolNotExposable):
            await update_exposure(enabled=True, allowed_tools=['system__publish_service'], updated_by='admin', deps=deps)
    finally:
        await sp.aclose()


@pytest.mark.parametrize('door', ['direct', 'through-call-tool', 'direct-as-_system', 'through-call-tool-as-_system'])
async def test_omitted_enabled_publisher_policy_still_parks_before_capture(tmp_path, door):
    from primer.bootstrap.seed import require_workspace_publisher_policy
    sp, registry, providers, resolver = await _world(tmp_path, 'fresh')
    try:
        store = sp.get_storage(ToolApprovalPolicy)
        ident = default_policy_id('system', 'publish_service')
        # Exercise actual JSON storage semantics, not a model that reserializes
        # the default into the row before the resolver's predicate reads it.
        async with sp.transaction() as conn:
            await conn.execute(f'UPDATE "{store._table}" SET data=json_remove(data, \'$.enabled\') WHERE id=?', (ident,))
        assert (await store.get(ident)).enabled is True
        await require_workspace_publisher_policy(sp)
        manager = ToolExecutionManager.for_workspace(
            toolset_providers=providers, session=_Session(), approval_resolver=resolver,
            provider_registry=registry, initiated_by=ADMIN,
            tools=['system__call_tool', 'system__publish_service', '_system__publish_service'],
        )
        with pytest.raises(YieldToWorker) as parked:
            await manager.execute(_call(door, 'system', 'publish_service', {'name': 'site', 'path': 'apps/site'}))
        meta = parked.value.yielded.resume_metadata
        assert meta['policy_id'] == ident and meta['approval_type'] == 'required'
        assert meta['approvers'] == ADMIN_ONLY_SPEC
        for model in (Service, ServiceVersion):
            assert not (await sp.get_storage(model).list(OffsetPage(offset=0, length=10))).items
    finally:
        await sp.aclose()


@pytest.mark.parametrize('enabled', [False, None])
async def test_publisher_explicit_disabled_choice_and_invalid_null_are_distinct(tmp_path, enabled):
    from primer.model.except_ import RowDecodeError
    from primer.bootstrap.seed import ensure_workspace_publisher_policy, require_workspace_publisher_policy
    sp, registry, providers, resolver = await _world(tmp_path, 'fresh')
    try:
        store = sp.get_storage(ToolApprovalPolicy)
        ident = default_policy_id('system', 'publish_service')
        async with sp.transaction() as conn:
            await conn.execute(f'UPDATE "{store._table}" SET data=json_set(data, \'$.enabled\', json(?)) WHERE id=?',
                               ('null' if enabled is None else 'false', ident))
        if enabled is None:
            with pytest.raises(RowDecodeError):
                await resolver.find(toolset_id='system', tool_name='publish_service')
            with pytest.raises(RowDecodeError):
                await require_workspace_publisher_policy(sp)
        else:
            before = (await store.get(ident)).model_dump()
            await ensure_workspace_publisher_policy(sp)
            await require_workspace_publisher_policy(sp)
            assert await resolver.find(toolset_id='system', tool_name='publish_service') is None
            assert (await store.get(ident)).model_dump() == before
    finally:
        await sp.aclose()


# Compatibility reads must use the same model semantics as startup, even when
# an existing row was not written by today's typed serializer.
_DOORS = ['direct', 'through-call-tool', 'direct-as-_system', 'through-call-tool-as-_system']


def _publisher_manager(registry, providers, resolver):
    return ToolExecutionManager.for_workspace(
        toolset_providers=providers, session=_Session(), approval_resolver=resolver,
        provider_registry=registry, initiated_by=ADMIN,
        tools=['system__call_tool', 'system__publish_service', '_system__publish_service'],
    )


async def _stored_enabled(sp, store, ident, value):
    import json
    async with sp.transaction() as conn:
        await conn.execute(
            f'UPDATE "{store._table}" SET data=json_set(data, \'$.enabled\', json(?)) WHERE id=?',
            (json.dumps(value), ident),
        )


def _forbid_publisher_dispatch(monkeypatch, providers):
    original = providers['system'].call
    calls = []

    async def guarded_call(*args, **kwargs):
        if kwargs.get('tool_name') == 'publish_service':
            calls.append(kwargs)
            pytest.fail('publisher handler reached before approval or policy-decode refusal')
        return await original(*args, **kwargs)

    monkeypatch.setattr(providers['system'], 'call', guarded_call)
    return calls


@pytest.mark.parametrize('stored_spelling', ['system', '_system'])
@pytest.mark.parametrize('door', _DOORS)
async def test_readable_string_enabled_parks_with_custom_settings_before_capture(tmp_path, monkeypatch, stored_spelling, door):
    from primer.bootstrap.seed import ensure_workspace_publisher_policy, require_workspace_publisher_policy
    from primer.model.tool_approval import ApproverSpec, RequiredApprovalConfig

    sp, registry, providers, resolver = await _world(tmp_path, 'fresh')
    try:
        store = sp.get_storage(ToolApprovalPolicy)
        await store.delete(default_policy_id('system', 'publish_service'))
        policy = ToolApprovalPolicy(
            id='operator-publisher', toolset_id=stored_spelling, tool_name='publish_service',
            approval=RequiredApprovalConfig(), approvers=ApproverSpec(kind='users', users=['reviewer']),
            timeout_seconds=123,
        )
        await store.create(policy)
        await _stored_enabled(sp, store, policy.id, 'true')
        assert (await store.get(policy.id)).enabled is True
        await ensure_workspace_publisher_policy(sp)
        await require_workspace_publisher_policy(sp)
        calls = _forbid_publisher_dispatch(monkeypatch, providers)
        with pytest.raises(YieldToWorker) as parked:
            await _publisher_manager(registry, providers, resolver).execute(
                _call(door, 'system', 'publish_service', {'name': 'site', 'path': 'apps/site'}),
            )
        meta = parked.value.yielded.resume_metadata
        assert meta['policy_id'] == policy.id and meta['approval_type'] == 'required'
        assert meta['approvers'] == policy.approvers.model_dump()
        assert parked.value.yielded.timeout == 123
        assert (await store.get(policy.id)).model_dump() == policy.model_dump()
        assert await store.get(default_policy_id('system', 'publish_service')) is None
        assert not calls
        for model in (Service, ServiceVersion):
            assert not (await sp.get_storage(model).list(OffsetPage(offset=0, length=10))).items
    finally:
        await sp.aclose()


@pytest.mark.parametrize('stored_spelling', ['system', '_system'])
@pytest.mark.parametrize('door', _DOORS)
async def test_disabled_page_cannot_hide_later_enabled_publisher(tmp_path, monkeypatch, stored_spelling, door):
    import primer.agent.approval as approval_module
    from primer.model.tool_approval import RequiredApprovalConfig

    # Exercise a real storage cursor with a wholly disabled first page, keeping
    # this fixture small. The production page size remains fifty.
    monkeypatch.setattr(approval_module, '_CANDIDATES', 2)
    sp, registry, providers, resolver = await _world(tmp_path, 'fresh')
    try:
        store = sp.get_storage(ToolApprovalPolicy)
        await store.delete(default_policy_id('system', 'publish_service'))
        for ident in ('a-disabled', 'b-disabled', 'z-active'):
            await store.create(ToolApprovalPolicy(
                id=ident, toolset_id=stored_spelling, tool_name='publish_service',
                enabled=ident == 'z-active', approval=RequiredApprovalConfig(),
            ))
        await _stored_enabled(sp, store, 'z-active', 'yes')
        calls = _forbid_publisher_dispatch(monkeypatch, providers)
        with pytest.raises(YieldToWorker) as parked:
            await _publisher_manager(registry, providers, resolver).execute(
                _call(door, 'system', 'publish_service', {'name': 'site', 'path': 'apps/site'}),
            )
        assert parked.value.yielded.resume_metadata['policy_id'] == 'z-active'
        assert not calls
        for model in (Service, ServiceVersion):
            assert not (await sp.get_storage(model).list(OffsetPage(offset=0, length=10))).items
    finally:
        await sp.aclose()


@pytest.mark.parametrize('stored_spelling', ['system', '_system'])
@pytest.mark.parametrize('value', [False, 'false', 0])
async def test_decoded_disabled_publisher_retains_operator_choice(tmp_path, stored_spelling, value):
    from primer.bootstrap.seed import ensure_workspace_publisher_policy, require_workspace_publisher_policy
    sp, registry, providers, resolver = await _world(tmp_path, 'fresh')
    try:
        store = sp.get_storage(ToolApprovalPolicy)
        ident = default_policy_id('system', 'publish_service')
        policy = await store.get(ident)
        await store.update(policy.model_copy(update={'toolset_id': stored_spelling}))
        await _stored_enabled(sp, store, ident, value)
        before = (await store.get(ident)).model_dump()
        assert before['enabled'] is False
        await ensure_workspace_publisher_policy(sp)
        await require_workspace_publisher_policy(sp)
        for spelling in ('system', '_system'):
            assert await resolver.find(toolset_id=spelling, tool_name='publish_service') is None
        assert (await store.get(ident)).model_dump() == before
    finally:
        await sp.aclose()


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('conditional', [False, True])
async def test_publisher_duplicates_keep_restrictive_stable_selection(tmp_path, monkeypatch, reverse, conditional):
    import primer.agent.approval as approval_module
    from primer.model.tool_approval import ApproverSpec, PolicyApprovalConfig, RequiredApprovalConfig

    monkeypatch.setattr(approval_module, '_CANDIDATES', 1)
    sp, registry, providers, resolver = await _world(tmp_path, 'fresh')
    try:
        store = sp.get_storage(ToolApprovalPolicy)
        await store.delete(default_policy_id('system', 'publish_service'))
        rows = [
            ToolApprovalPolicy(id='a-conditional', toolset_id='system', tool_name='publish_service',
                               approval=PolicyApprovalConfig(policy='package primer.tool_approval\ndefault required := false\n')),
            ToolApprovalPolicy(id='b-first', toolset_id='_system', tool_name='publish_service',
                               approval=RequiredApprovalConfig(), timeout_seconds=77,
                               approvers=ApproverSpec(kind='users', users=['reviewer'])),
            ToolApprovalPolicy(id='c-second', toolset_id='system', tool_name='publish_service',
                               approval=RequiredApprovalConfig(), timeout_seconds=99,
                               approvers=ApproverSpec(kind='users', users=['reviewer'])),
            ToolApprovalPolicy(id='a-disabled', toolset_id='_system', tool_name='publish_service', enabled=False,
                               approval=RequiredApprovalConfig()),
        ]
        if not conditional:
            rows = rows[1:]
        for row in reversed(rows) if reverse else rows:
            await store.create(row)
        await _stored_enabled(sp, store, 'b-first', 'true')
        before = {row.id: (await store.get(row.id)).model_dump() for row in rows}
        selected = await resolver.find(toolset_id='_system', tool_name='publish_service')
        assert selected.id == 'b-first' and selected.timeout_seconds == 77
        assert selected.approval.type.value == 'required'
        expected = ADMIN_ONLY_SPEC if conditional else {'kind': 'users', 'users': ['reviewer'], 'roles': []}
        assert selected.approvers.model_dump() == expected
        assert 'b-first' in selected.approval.reason and 'c-second' in selected.approval.reason
        assert 'a-disabled' not in selected.approval.reason
        assert {row.id: (await store.get(row.id)).model_dump() for row in rows} == before
    finally:
        await sp.aclose()


@pytest.mark.parametrize('stored_spelling', ['system', '_system'])
@pytest.mark.parametrize('door', _DOORS)
async def test_malformed_later_publisher_row_refuses_dispatch_without_caching_none(tmp_path, monkeypatch, stored_spelling, door):
    import primer.agent.approval as approval_module
    from primer.model.except_ import RowDecodeError
    from primer.model.tool_approval import RequiredApprovalConfig

    monkeypatch.setattr(approval_module, '_CANDIDATES', 1)
    sp, registry, providers, resolver = await _world(tmp_path, 'fresh')
    try:
        store = sp.get_storage(ToolApprovalPolicy)
        await store.delete(default_policy_id('system', 'publish_service'))
        for ident in ('a-disabled', 'z-corrupt'):
            await store.create(ToolApprovalPolicy(
                id=ident, toolset_id=stored_spelling, tool_name='publish_service',
                enabled=False, approval=RequiredApprovalConfig(),
            ))
        await _stored_enabled(sp, store, 'z-corrupt', 'not-a-boolean')
        calls = _forbid_publisher_dispatch(monkeypatch, providers)
        manager = _publisher_manager(registry, providers, resolver)
        call = _call(door, 'system', 'publish_service', {'name': 'site', 'path': 'apps/site'})
        if door.startswith('direct'):
            with pytest.raises(RowDecodeError):
                await manager.execute(call)
        else:
            # The outer tool manager turns the inner call_tool PrimerError into
            # a visible tool error. Neither path may reach the publisher.
            result = await manager.execute(call)
            assert result.error is True
            assert 'z-corrupt' in str(result.output)
        assert not calls
        for model in (Service, ServiceVersion):
            assert not (await sp.get_storage(model).list(OffsetPage(offset=0, length=10))).items
        # A failed read must not poison the resolver with a cached absent gate.
        await _stored_enabled(sp, store, 'z-corrupt', 'true')
        with pytest.raises(YieldToWorker) as parked:
            await manager.execute(call)
        assert parked.value.yielded.resume_metadata['policy_id'] == 'z-corrupt'
        assert not calls
    finally:
        await sp.aclose()
