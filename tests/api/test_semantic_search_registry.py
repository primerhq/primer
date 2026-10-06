"""Unit tests for SemanticSearchRegistry."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from primer.api.registries.semantic_search_registry import (
    SemanticSearchRegistry,
)
from primer.model.except_ import NotFoundError
from primer.model.provider import (
    PgVectorConfig,
    PoolConfig,
    SemanticSearchProvider,
    SemanticSearchProviderType,
)


class _StubStorage:
    """Minimal Storage[SemanticSearchProvider] stand-in."""

    def __init__(self, rows: dict[str, SemanticSearchProvider]):
        self._rows = rows
        self.gets = 0

    async def get(self, entity_id: str, *, principal=None):
        self.gets += 1
        if entity_id not in self._rows:
            raise NotFoundError(f"SemanticSearchProvider {entity_id!r} not found")
        return self._rows[entity_id]


class _StubProvider:
    """VectorStoreProvider stand-in."""

    def __init__(self, row):
        self.row = row
        self.initialized = False
        self.closed = False

    async def initialize(self):
        self.initialized = True

    async def aclose(self):
        self.closed = True


def _make_row(rid: str) -> SemanticSearchProvider:
    return SemanticSearchProvider(
        id=rid,
        provider=SemanticSearchProviderType.PGVECTOR,
        config=PgVectorConfig(
            hostname="localhost",
            port=5432,
            username="u",
            password=SecretStr("p"),
            database="db",
            db_schema="public",
            pool=PoolConfig(),
        ),
    )


@pytest.mark.asyncio
async def test_registry_caches_instance_per_id():
    row = _make_row("ssp-a")
    storage = _StubStorage({"ssp-a": row})
    instances: list[_StubProvider] = []

    def factory(r):
        inst = _StubProvider(r)
        instances.append(inst)
        return inst

    reg = SemanticSearchRegistry(storage=storage, factory=factory)
    p1 = await reg.get_provider("ssp-a")
    p2 = await reg.get_provider("ssp-a")
    assert p1 is p2
    assert len(instances) == 1
    assert instances[0].initialized is True


@pytest.mark.asyncio
async def test_registry_invalidate_closes_instance():
    storage = _StubStorage({"ssp-a": _make_row("ssp-a")})
    instances = []
    def factory(r):
        inst = _StubProvider(r); instances.append(inst); return inst

    reg = SemanticSearchRegistry(storage=storage, factory=factory)
    await reg.get_provider("ssp-a")
    await reg.invalidate("ssp-a")
    assert instances[0].closed is True
    # Next get re-constructs
    await reg.get_provider("ssp-a")
    assert len(instances) == 2


class _FailingCloseProvider(_StubProvider):
    async def aclose(self):
        self.closed = True
        raise RuntimeError("the pool is already gone")


@pytest.mark.asyncio
async def test_registry_invalidate_survives_an_instance_whose_close_fails():
    """``invalidate`` runs AFTER the provider row was updated or deleted (the REST hook and the system tool both call it
    post-commit), so an ``aclose`` error must not surface as a failure of a write that already happened: the caller
    would be told the update failed when it landed. Every other close in this registry already swallows and logs."""
    storage = _StubStorage({"ssp-a": _make_row("ssp-a")})
    instances = []

    def factory(r):
        inst = _FailingCloseProvider(r)
        instances.append(inst)
        return inst

    reg = SemanticSearchRegistry(storage=storage, factory=factory)
    await reg.get_provider("ssp-a")

    await reg.invalidate("ssp-a")  # must not raise

    assert instances[0].closed is True, "the close was never attempted"
    await reg.get_provider("ssp-a")
    assert len(instances) == 2, "the stale instance stayed cached after a failed close"


@pytest.mark.asyncio
async def test_registry_invalidate_logs_a_failed_close(caplog):
    storage = _StubStorage({"ssp-a": _make_row("ssp-a")})
    reg = SemanticSearchRegistry(storage=storage, factory=lambda r: _FailingCloseProvider(r))
    await reg.get_provider("ssp-a")

    with caplog.at_level("WARNING", logger="primer.api.registries.semantic_search_registry"):
        await reg.invalidate("ssp-a")

    assert any("ssp-a" in r.getMessage() and "the pool is already gone" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_registry_invalidate_of_an_unknown_id_is_a_no_op():
    reg = SemanticSearchRegistry(storage=_StubStorage({}), factory=lambda r: _StubProvider(r))

    await reg.invalidate("never-built")  # nothing cached, nothing to close


@pytest.mark.asyncio
async def test_registry_get_missing_row_raises_not_found():
    storage = _StubStorage({})
    reg = SemanticSearchRegistry(storage=storage, factory=lambda r: _StubProvider(r))
    with pytest.raises(NotFoundError):
        await reg.get_provider("missing")


class _NoneReturningStorage:
    """Storage stand-in that returns ``None`` for a missing id.

    Mirrors the real ``Storage.get`` contract (a dangling/bogus id
    resolves to ``None`` rather than raising), which the sibling
    registries (artifact, web-search) all model. The other stub in this
    file *raises* on a miss, which masked the registry's missing
    None-guard -- so this stub is what exercises it.
    """

    async def get(self, entity_id: str, *, principal=None):
        return None


@pytest.mark.asyncio
async def test_registry_get_none_row_raises_not_found_not_attribute_error():
    """A None row from storage must surface NotFoundError, not the
    AttributeError the factory would raise dereferencing ``None``."""
    reg = SemanticSearchRegistry(
        storage=_NoneReturningStorage(),
        factory=lambda r: _StubProvider(r),
    )
    with pytest.raises(NotFoundError):
        await reg.get_provider("ssp-dangling")


@pytest.mark.asyncio
async def test_registry_aclose_closes_all_cached():
    storage = _StubStorage({"a": _make_row("a"), "b": _make_row("b")})
    instances = []
    def factory(r):
        inst = _StubProvider(r); instances.append(inst); return inst
    reg = SemanticSearchRegistry(storage=storage, factory=factory)
    await reg.get_provider("a")
    await reg.get_provider("b")
    await reg.aclose()
    assert all(i.closed for i in instances)


@pytest.mark.asyncio
async def test_registry_aclose_continues_after_exception():
    """aclose() must close every cached instance even if one raises."""
    class _FailingProvider(_StubProvider):
        async def aclose(self):
            await super().aclose()
            raise RuntimeError("boom")
    storage = _StubStorage({"a": _make_row("a"), "b": _make_row("b")})
    instances: list[_StubProvider] = []
    def factory(r):
        # First instance raises on aclose; second succeeds.
        inst = _FailingProvider(r) if r.id == "a" else _StubProvider(r)
        instances.append(inst)
        return inst
    reg = SemanticSearchRegistry(storage=storage, factory=factory)
    await reg.get_provider("a")
    await reg.get_provider("b")
    # Must not raise; both must have aclose() called.
    await reg.aclose()
    assert all(i.closed for i in instances)


# ---------- Factory dispatch to lance backend ---------------------------


@pytest.mark.asyncio
async def test_default_factory_dispatches_lance(tmp_path):
    """Verify SemanticSearchRegistry._default_factory dispatches a
    lance-backed row to LanceVectorStoreProvider."""
    pytest.importorskip("lancedb")  # type: ignore[arg-type]
    from primer.api.registries.semantic_search_registry import _default_factory
    from primer.model.provider import (
        LanceConfig,
        SemanticSearchProvider,
        SemanticSearchProviderType,
    )
    from primer.vector.lance import LanceVectorStoreProvider

    row = SemanticSearchProvider(
        id="ssp-lance",
        provider=SemanticSearchProviderType.LANCE,
        config=LanceConfig(path=tmp_path / "lance"),
    )
    instance = _default_factory(row)
    assert isinstance(instance, LanceVectorStoreProvider)
