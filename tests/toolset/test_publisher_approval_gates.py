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
