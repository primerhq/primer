"""Fresh schema baselining must not turn a swallowed seed failure into permission."""
import pytest

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.model.provider import SqliteConfig
from primer.model.scheduler import RuntimeMode
from primer.model.service import Service, ServiceVersion
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalPolicy
from primer.storage.sqlite import SqliteStorageProvider


async def test_fresh_seed_failure_refuses_worker_start_and_healthy_retry_recovers(tmp_path, monkeypatch):
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
    def forbidden_worker(*args, **kwargs):
        started.append(True)
        raise AssertionError('worker constructed without publisher policy')
    monkeypatch.setenv('PRIMER_WIKI_DIR', str(tmp_path))
    monkeypatch.setattr('primer.api.app._build_storage_provider', lambda cfg: sp)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(store, 'create', fail_publisher)
            patch.setattr('primer.worker.pool.WorkerPool', forbidden_worker)
            app = create_app(AppConfig(runtime_mode=RuntimeMode.WORKER, auto_bootstrap=False))
            with pytest.raises(RuntimeError, match='workspace publisher approval policy is missing'):
                async with app.router.lifespan_context(app):
                    pytest.fail('fresh startup served without publisher policy')
        assert failed and started == []
        await sp.initialize()
        assert (await sp.get_system_state()).schema_version == 35
        assert await sp.get_storage(ToolApprovalPolicy).get('tool-approval-policy-system-publish-service') is None
        for model in (Service, ServiceVersion):
            assert not (await sp.get_storage(model).list(OffsetPage(offset=0, length=10))).items
        await sp.aclose()
        # Retry on the same stamped database: no migration applies, so the real
        # ensure pass must repair the policy before the lifespan can yield.
        recovered = create_app(AppConfig(runtime_mode=RuntimeMode.API, auto_bootstrap=False))
        async with recovered.router.lifespan_context(recovered):
            assert (await sp.get_system_state()).schema_version == 35
            policy = await recovered.state.approval_resolver.find(toolset_id='system', tool_name='publish_service')
            assert policy is not None and policy.enabled and policy.approval.type.value == 'required'
            assert policy.approvers.model_dump() == {'kind': 'roles', 'roles': [], 'users': []}
    finally:
        await sp.aclose()
