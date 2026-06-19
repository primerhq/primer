"""Concurrency-safety tests for SQLite multi-write transactions.

The SQLite backend uses ONE shared aiosqlite connection for every
``Storage`` handle, the claim engine, the scheduler, and every concurrent
request. ``SqliteStorageProvider.transaction()`` groups a multi-write unit
(used by :class:`~primer.knowledge.document_service.DocumentService`) into
one atomic BEGIN..COMMIT.

These tests pin the invariants the transaction MUST satisfy on that shared
connection:

1. A successful transaction commits BOTH its writes.
2. A failed transaction rolls back BOTH its writes.
3. An UNRELATED write by another coroutine, interleaved between the
   transaction's two writes, is NEVER (a) lost when the transaction rolls
   back, (b) silently captured into the transaction, nor (c) made to raise
   a spurious "not re-entrant" error. Unrelated writes keep their own
   independent durability.
4. Two concurrent transactional units do not corrupt each other or raise a
   spurious re-entrancy error.

The interleaving is made deterministic by pausing the transaction at an
``await`` point between its first and second write (via an injected
``asyncio.Event``) so the competing write is scheduled "in the middle".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from primer.model.common import Identifiable
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


pytestmark = pytest.mark.asyncio


class _Thing(Identifiable):
    """A trivial unrelated entity for the competing-write tests."""

    value: str = "v"


@pytest_asyncio.fixture
async def provider(tmp_path: Path):
    cfg = SqliteConfig(path=tmp_path / "txn_safety.sqlite")
    p = SqliteStorageProvider(cfg)
    await p.initialize()
    try:
        yield p
    finally:
        await p.aclose()


async def test_commit_persists_both_writes(provider: SqliteStorageProvider) -> None:
    things = provider.get_storage(_Thing)
    content = provider.get_content_store()
    await content.ensure_schema()

    async with provider.transaction() as conn:
        await things.create(_Thing(id="t-commit", value="a"), conn=conn)
        await content.upsert(
            document_id="d-commit",
            collection_id="c",
            path="x.md",
            content="body",
            conn=conn,
        )

    assert (await things.get("t-commit")) is not None
    assert (await content.get("d-commit")) == "body"


async def test_rollback_discards_both_writes(provider: SqliteStorageProvider) -> None:
    things = provider.get_storage(_Thing)
    content = provider.get_content_store()
    await content.ensure_schema()

    with pytest.raises(RuntimeError):
        async with provider.transaction() as conn:
            await things.create(_Thing(id="t-roll", value="a"), conn=conn)
            await content.upsert(
                document_id="d-roll",
                collection_id="c",
                path="x.md",
                content="body",
                conn=conn,
            )
            raise RuntimeError("boom after both writes")

    # Neither write survived the rollback.
    assert (await things.get("t-roll")) is None
    assert (await content.get("d-roll")) is None
    # And the connection is not stuck in skip-commit state.
    await things.create(_Thing(id="t-after", value="b"))
    assert (await things.get("t-after")) is not None


async def test_unrelated_write_survives_txn_rollback(
    provider: SqliteStorageProvider,
) -> None:
    """An independent write that races a transaction is durable even when the
    transaction rolls back, and is never captured into it (BUG #1: data loss /
    capture).

    The competitor's write is launched WHILE the doomed transaction is open
    (so it would, under the old global-flag scheme, be swept into the txn and
    lost on rollback). With the serialising write lock the competitor instead
    blocks until the transaction releases, then commits independently. Either
    way the invariant is identical: the competitor's row survives the doomed
    txn's rollback, and the doomed txn's own row does not.
    """
    things = provider.get_storage(_Thing)
    content = provider.get_content_store()
    await content.ensure_schema()
    # Pre-create the tables so no DDL runs mid-transaction.
    await things.create(_Thing(id="t-seed", value="seed"))

    midpoint = asyncio.Event()
    competitor_launched = asyncio.Event()

    async def doomed_txn() -> None:
        with pytest.raises(RuntimeError):
            async with provider.transaction() as conn:
                await things.create(_Thing(id="t-doomed", value="d"), conn=conn)
                # We are mid-transaction (one write done). Release the
                # competitor so its write races the open transaction...
                midpoint.set()
                # ...and make sure it has actually been scheduled (and is now
                # blocked on the shared write lock) before we roll back.
                await competitor_launched.wait()
                raise RuntimeError("rollback the doomed txn")

    async def competitor() -> None:
        await midpoint.wait()
        competitor_launched.set()
        # This independent write is issued while the txn is still open; it
        # blocks on the write lock and commits once the txn rolls back.
        await things.create(_Thing(id="t-indep", value="independent"))

    await asyncio.gather(doomed_txn(), competitor())

    # The doomed transaction's write is gone...
    assert (await things.get("t-doomed")) is None
    # ...but the unrelated write is DURABLE (not captured / rolled back).
    indep = await things.get("t-indep")
    assert indep is not None and indep.value == "independent"


async def test_concurrent_txns_no_spurious_reentrancy(
    provider: SqliteStorageProvider,
) -> None:
    """Two concurrent transactional units must each commit cleanly without a
    spurious 'not re-entrant' ConfigError (BUG #1)."""
    things = provider.get_storage(_Thing)
    content = provider.get_content_store()
    await content.ensure_schema()
    await things.create(_Thing(id="t-seed", value="seed"))

    async def unit(n: int) -> None:
        async with provider.transaction() as conn:
            await things.create(_Thing(id=f"t-{n}", value=str(n)), conn=conn)
            await content.upsert(
                document_id=f"d-{n}",
                collection_id="c",
                path=f"p{n}.md",
                content=str(n),
                conn=conn,
            )
            # Yield control to give the sibling unit a chance to interleave.
            await asyncio.sleep(0)

    await asyncio.gather(*(unit(i) for i in range(5)))

    for i in range(5):
        assert (await things.get(f"t-{i}")) is not None
        assert (await content.get(f"d-{i}")) == str(i)
