"""Unit tests for ClaimEngineFactory bus-type dispatch."""

from __future__ import annotations

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.claim.factory import ClaimEngineFactory
from primer.claim.in_memory import InMemoryClaimEngine
from primer.claim.postgres import PostgresClaimEngine
from primer.int.claim import ClaimKind


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------


class _FakeStorageProvider:
    """Minimal storage-provider stub for factory tests."""

    def get_storage(self, model_class):
        return None

    @property
    def leases_table(self) -> str:
        return '"test"."leases"'

    @property
    def schema(self) -> str:
        return "test"

    # asyncpg pool stub — never actually used in unit tests
    pool = None


class _FakePostgresEventBus:
    """Non-InMemory bus — factory should choose PostgresClaimEngine."""
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_factory_returns_in_memory_engine_for_in_memory_bus():
    engine = ClaimEngineFactory.create(
        storage_provider=_FakeStorageProvider(),
        event_bus=InMemoryEventBus(),
    )
    assert isinstance(engine, InMemoryClaimEngine)


def test_factory_returns_postgres_engine_for_non_in_memory_bus():
    engine = ClaimEngineFactory.create(
        storage_provider=_FakeStorageProvider(),
        event_bus=_FakePostgresEventBus(),
    )
    assert isinstance(engine, PostgresClaimEngine)


def test_factory_in_memory_engine_has_all_three_adapters():
    engine = ClaimEngineFactory.create(
        storage_provider=_FakeStorageProvider(),
        event_bus=InMemoryEventBus(),
    )
    assert isinstance(engine, InMemoryClaimEngine)
    assert ClaimKind.SESSION in engine._adapters
    assert ClaimKind.CHAT in engine._adapters
    assert ClaimKind.HARNESS in engine._adapters


def test_factory_postgres_engine_has_all_three_adapters():
    engine = ClaimEngineFactory.create(
        storage_provider=_FakeStorageProvider(),
        event_bus=_FakePostgresEventBus(),
    )
    assert isinstance(engine, PostgresClaimEngine)
    assert ClaimKind.SESSION in engine._adapters
    assert ClaimKind.CHAT in engine._adapters
    assert ClaimKind.HARNESS in engine._adapters
