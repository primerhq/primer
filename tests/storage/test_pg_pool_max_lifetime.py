"""PoolConfig.max_lifetime is accepted but NOT enforced, and must say so.

The field was documented as "seconds a connection may live before being
recycled (defends against leaks)" and nothing ever read it: asyncpg has no
maximum connection age, and the connections that matter most, the LISTEN
connections a worker holds checked out for its whole life, would never be
recycled by an age limit anyway (it could only ever touch idle pooled
connections, which ``max_idle`` already closes). So an operator who tuned it
was relying on a defence that does not exist.

The field is kept, so a saved config that sets it still validates, but its
description now says plainly that it does nothing, and a non-default value logs
one warning when a pool is created. These tests drive both providers with a
stand-in ``asyncpg.create_pool`` that raises straight away, so no database is
needed.
"""

from __future__ import annotations

import logging

import asyncpg
import pytest
from pydantic import ValidationError

from primer.model.except_ import ProviderError
from primer.model.provider import PgVectorConfig, PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider
from primer.vector.pgvector import PgVectorStoreProvider

_CONN = dict(hostname="db.invalid", port=5432, username="u", password="p", database="d")
_DEFAULT = PoolConfig().max_lifetime


@pytest.fixture(autouse=True)
def _no_real_pool(monkeypatch):
    async def fake_create_pool(*args, **kwargs):
        raise RuntimeError("stop: no database in this test")

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)


async def _open_storage(pool: PoolConfig) -> None:
    with pytest.raises(ProviderError):
        await PostgresStorageProvider(PostgresConfig(**_CONN, pool=pool)).initialize()


async def _open_pgvector(pool: PoolConfig) -> None:
    with pytest.raises(ProviderError):
        await PgVectorStoreProvider(PgVectorConfig(**_CONN, pool=pool)).initialize()


def _lifetime_warnings(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "max_lifetime" in r.getMessage()
    ]


def test_the_description_says_plainly_that_it_is_not_enforced():
    description = PoolConfig.model_fields["max_lifetime"].description

    assert "NOT ENFORCED" in description
    assert "defends against leaks" not in description  # the claim that was never true


def test_the_field_is_kept_so_saved_configs_still_validate():
    assert PoolConfig(max_lifetime=7200).max_lifetime == 7200
    assert PoolConfig.model_validate({"max_lifetime": 1800.0}).max_lifetime == 1800.0
    with pytest.raises(ValidationError):
        PoolConfig(max_lifetime=0)  # the existing constraint is unchanged


@pytest.mark.parametrize("open_pool", [_open_storage, _open_pgvector], ids=["storage", "pgvector"])
async def test_a_non_default_value_logs_one_warning_when_the_pool_is_created(open_pool, caplog):
    with caplog.at_level(logging.DEBUG):
        await open_pool(PoolConfig(max_lifetime=7200.0))

    (warning,) = _lifetime_warnings(caplog)
    message = warning.getMessage()
    assert "7200" in message
    assert "not enforced" in message.lower()


@pytest.mark.parametrize("open_pool", [_open_storage, _open_pgvector], ids=["storage", "pgvector"])
async def test_the_default_value_says_nothing(open_pool, caplog):
    with caplog.at_level(logging.DEBUG):
        await open_pool(PoolConfig(max_lifetime=_DEFAULT))

    assert _lifetime_warnings(caplog) == []


async def test_each_pool_creation_warns_once(caplog):
    with caplog.at_level(logging.DEBUG):
        await _open_storage(PoolConfig(max_lifetime=7200.0))
        await _open_storage(PoolConfig(max_lifetime=7200.0))

    assert len(_lifetime_warnings(caplog)) == 2  # one per pool, not one per process or per connection
