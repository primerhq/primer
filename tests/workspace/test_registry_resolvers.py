import pytest
from pydantic import SecretStr

from primer.api.registries.workspace_registry import WorkspaceRegistry
from primer.model.workspace import WorkspaceTemplate

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
