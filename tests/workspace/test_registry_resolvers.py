import pytest
from datetime import UTC, datetime
from pydantic import SecretStr

from primer.api.registries.workspace_registry import WorkspaceRegistry
from primer.model.workspace import Workspace as WorkspaceRow
from primer.model.workspace import WorkspaceRuntimeMeta, WorkspaceTemplate

# A real template: materialise reads its env and sizes before it asks the backend for anything.
_TEMPLATE = WorkspaceTemplate(id="t", provider_id="prov1", description="d")


class _StubSecretProvider:
    async def get_secret(self, name):
        return SecretStr("v") if name == "known" else None


class _RecordingBackend:
    def __init__(self):
        self.received_resolvers = None

    async def create(
        self, template, *, overrides=None, workspace_id=None, resolvers=None
    ):
        self.received_resolvers = resolvers
        return "workspace-handle"


class _ReattachBackend:
    def __init__(self):
        self.template = None

    async def get(self, workspace_id, *, template=None):
        self.template = template
        return "reattached-handle"


@pytest.mark.asyncio
async def test_materialise_passes_resolvers(monkeypatch):
    backend = _RecordingBackend()
    reg = WorkspaceRegistry(
        storage_provider=object(),
        secret_provider=_StubSecretProvider(),
    )

    async def _fake_get_backend(provider_id):
        return backend

    monkeypatch.setattr(reg, "get_backend", _fake_get_backend)

    result = await reg.materialise(template=_TEMPLATE)
    assert result == "workspace-handle"
    assert backend.received_resolvers is not None
    assert backend.received_resolvers.document_resolver is not None
    assert backend.received_resolvers.secret_resolver is not None


@pytest.mark.asyncio
async def test_materialise_without_secret_provider_omits_secret_resolver(monkeypatch):
    backend = _RecordingBackend()
    reg = WorkspaceRegistry(storage_provider=object())  # no secret provider

    async def _fake_get_backend(provider_id):
        return backend

    monkeypatch.setattr(reg, "get_backend", _fake_get_backend)

    await reg.materialise(template=_TEMPLATE)
    assert backend.received_resolvers is not None
    assert backend.received_resolvers.document_resolver is not None
    assert backend.received_resolvers.secret_resolver is None


@pytest.mark.asyncio
async def test_reattach_uses_the_workspace_template_snapshot(
    fake_storage_provider, monkeypatch,
):
    snapshot = WorkspaceTemplate(
        id="t", provider_id="prov1", description="create-time recipe",
    )
    edited = snapshot.model_copy(update={"description": "later edit"})
    row = WorkspaceRow(
        id="w1",
        template_id=snapshot.id,
        provider_id=snapshot.provider_id,
        created_at=datetime.now(UTC),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://runtime.test:5959/", token=SecretStr("token"),
        ),
    ).model_copy(update={"template_snapshot": snapshot})
    await fake_storage_provider.get_storage(WorkspaceRow).create(row)
    await fake_storage_provider.get_storage(WorkspaceTemplate).create(edited)

    backend = _ReattachBackend()
    registry = WorkspaceRegistry(fake_storage_provider)

    async def _fake_get_backend(provider_id):
        return backend

    monkeypatch.setattr(registry, "get_backend", _fake_get_backend)

    assert await registry.get_workspace("w1") == "reattached-handle"
    assert backend.template is snapshot
