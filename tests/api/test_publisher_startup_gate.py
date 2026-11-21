"""Fresh schema baselining must not turn a swallowed seed failure into permission."""
import asyncio

import pytest

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.model.provider import SqliteConfig
from primer.model.scheduler import RuntimeMode
from primer.model.service import Service, ServiceVersion
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalPolicy
from primer.storage.sqlite import SqliteStorageProvider


@pytest.mark.parametrize("failure", ["create", "read"])
async def test_fresh_seed_failure_refuses_worker_start_and_healthy_retry_recovers(tmp_path, monkeypatch, failure):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / 'publisher.sqlite'))
    await sp.initialize()
    store = sp.get_storage(ToolApprovalPolicy)
    create = store.create
    failed, started = [], []
    async def fail_publisher(entity):
        if entity.tool_name == 'publish_service':
            failed.append(entity.id)
            raise OSError('transient publisher policy write failure')
        return await create(entity)
    async def fail_read(*args, **kwargs):
        failed.append('read')
        raise OSError('transient publisher policy read failure')
    def forbidden_worker(*args, **kwargs):
        started.append(True)
        raise AssertionError('worker constructed without publisher policy')
    monkeypatch.setenv('PRIMER_WIKI_DIR', str(tmp_path))
    monkeypatch.setattr('primer.api.app._build_storage_provider', lambda cfg: sp)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(store, 'create' if failure == 'create' else 'find',
                          fail_publisher if failure == 'create' else fail_read)
            patch.setattr('primer.observability.tracing.setup', forbidden_worker)
            patch.setattr('primer.secret.factory.SecretProviderFactory.create', forbidden_worker)
            patch.setattr('primer.worker.pool.WorkerPool', forbidden_worker)
            app = create_app(AppConfig(runtime_mode=RuntimeMode.WORKER, auto_bootstrap=False))
            before = set(asyncio.all_tasks())
            with pytest.raises(OSError, match='transient publisher policy'):
                async with app.router.lifespan_context(app):
                    pytest.fail('fresh startup served without publisher policy')
        assert failed and started == []
        assert sp._conn is None, 'refused startup must close storage without test assistance'
        await asyncio.sleep(0)
        assert not [t for t in asyncio.all_tasks() if t not in before and not t.done()]
        assert not hasattr(app.state, 'approval_resolver')
        assert not hasattr(app.state, 'worker_pool')
        await sp.initialize()
        assert (await sp.get_system_state()).schema_version == 35
        assert await sp.get_storage(ToolApprovalPolicy).get('tool-approval-policy-system-publish-service') is None
        for model in (Service, ServiceVersion):
            assert not (await sp.get_storage(model).list(OffsetPage(offset=0, length=10))).items
        await sp.aclose()
        # Retry on the same stamped database: no migration applies, so the critical
        # seed must repair the policy before the lifespan can yield.
        recovered = create_app(AppConfig(runtime_mode=RuntimeMode.API, auto_bootstrap=False))
        async with recovered.router.lifespan_context(recovered):
            assert (await sp.get_system_state()).schema_version == 35
            policy = await recovered.state.approval_resolver.find(toolset_id='system', tool_name='publish_service')
            assert policy is not None and policy.enabled and policy.approval.type.value == 'required'
            assert policy.approvers.model_dump() == {'kind': 'roles', 'roles': [], 'users': []}
    finally:
        await sp.aclose()
