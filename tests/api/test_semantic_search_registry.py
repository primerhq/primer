"""Unit tests for SemanticSearchRegistry."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from matrix.api.registries.semantic_search_registry import (
    SemanticSearchRegistry,
)
from matrix.model.except_ import NotFoundError
from matrix.model.provider import (
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


@pytest.mark.asyncio
async def test_registry_get_missing_row_raises_not_found():
    storage = _StubStorage({})
    reg = SemanticSearchRegistry(storage=storage, factory=lambda r: _StubProvider(r))
    with pytest.raises(NotFoundError):
        await reg.get_provider("missing")


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
