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
from typing import Any

import pytest
import pytest_asyncio

from primer.model.common import Identifiable
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage, OffsetPageResponse
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


async def test_offset_count_and_page_are_one_snapshot(
    provider: SqliteStorageProvider,
) -> None:
    """A paginated ``list()``'s ``total`` and its page must be ONE consistent
    snapshot: a write that races the pagination must not land BETWEEN the page
    ``SELECT`` and the ``COUNT(*)`` and make them disagree (BE10a).

    The race is made deterministic by wrapping the shared connection so the
    competitor is released right before the ``COUNT(*)`` runs -- i.e. exactly
    "between" the two statements. Under the read snapshot the competitor blocks
    on the write lock and only lands after the pair has been read together.
    """
    things = provider.get_storage(_Thing)
    for i in range(5):
        await things.create(_Thing(id=f"t{i}", value=str(i)))

    real_conn = provider._conn  # noqa: SLF001
    released = asyncio.Event()

    class _ReleaseBeforeCount:
        """Wrap the shared aiosqlite connection: when the paginated
        ``COUNT(*)`` is about to run, release the competitor and yield so it is
        scheduled (and blocks on the write lock) 'between' the page ``SELECT``
        and the ``COUNT``."""

        def __init__(self, real: Any) -> None:
            self._real = real

        async def execute(self, sql: str, *args: Any, **kwargs: Any):  # noqa: ANN201
            if "count(*)" in sql.lower() and not released.is_set():
                released.set()
                # Yield repeatedly so the competitor task runs and parks on the
                # write lock before we read the count.
                for _ in range(10):
                    await asyncio.sleep(0)
            return await self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

    provider._conn = _ReleaseBeforeCount(real_conn)  # noqa: SLF001

    async def competitor() -> None:
        await released.wait()
        # These inserts block on the write lock held by the list's read
        # snapshot, so they can only land AFTER the page + count are read
        # together -- never interleaved between them.
        await things.create(_Thing(id="t5", value="5"))
        await things.create(_Thing(id="t6", value="6"))

    comp = asyncio.ensure_future(competitor())
    try:
        page = await things.list(OffsetPage(offset=0, length=100))
        await comp
    finally:
        provider._conn = real_conn  # noqa: SLF001

    assert isinstance(page, OffsetPageResponse)
    # The page and its total saw the SAME world: neither reflects the
    # competitor's two inserts (serialised behind the snapshot's write lock).
    assert page.total == 5
    assert page.length == 5
    assert len(page.items) == 5
    assert page.total == page.length

    # The competitor's writes DID happen (the race was real): a fresh,
    # post-snapshot list sees all seven rows -- the guard reordered the write to
    # AFTER the snapshot, it did not drop it.
    after = await things.list(OffsetPage(offset=0, length=100))
    assert after.total == 7
    assert after.length == 7


# ===========================================================================
# 01a06cb3: a failed standalone write must not leave an implicit
# transaction open on the shared connection.
#
# aiosqlite's default isolation_level opens an IMPLICIT transaction on the
# first DML statement. _write_guard's standalone branch used to yield the
# caller straight through with no except: a write that failed before its
# own commit() (a UNIQUE violation -> the expected ConflictError dedup
# path, a plain bug, or a CancelledError landing between execute and
# commit) left that implicit transaction open, untracked by _txn_task
# (only transaction()/read_snapshot() set that). The very next
# read_snapshot -- ANY unrelated paginated read on the shared connection,
# not just a retry of the same write -- then failed its own BEGIN with
# "cannot start a transaction within a transaction".
# ===========================================================================


async def test_duplicate_create_conflict_does_not_wedge_the_connection(
    provider: SqliteStorageProvider,
) -> None:
    """A standalone create() that fails with the expected ConflictError
    (e.g. a raced gate_event_key dedup write) must not wedge the shared
    connection for the very next read."""
    from primer.model.except_ import ConflictError

    things = provider.get_storage(_Thing)
    await things.create(_Thing(id="dup", value="first"))

    with pytest.raises(ConflictError):
        await things.create(_Thing(id="dup", value="second"))

    # Before the fix: this read_snapshot's own BEGIN raised "cannot start
    # a transaction within a transaction".
    page = await things.list(OffsetPage(offset=0, length=50))
    assert {t.id for t in page.items} == {"dup"}

    # The connection is genuinely usable again, not just readable.
    await things.create(_Thing(id="after-conflict", value="ok"))
    assert (await things.get("after-conflict")) is not None


async def test_failing_update_does_not_wedge_the_connection(
    provider: SqliteStorageProvider,
) -> None:
    """Same shape as the create() case, for update() -- proves the fix
    lives at the _write_guard level (every standalone write method), not
    a create()-specific patch. Simulates a generic failure (update()'s
    own SQL has no natural UNIQUE trigger to reach for), matching the
    fix's BaseException scope: ANY failure, not just IntegrityError.

    The wrapper calls through to the REAL execute() first, so the
    UPDATE's implicit transaction genuinely opens on the real
    connection, and only THEN raises -- a wrapper that raises without
    ever reaching the real driver never opens a transaction in the first
    place, so it would prove nothing (the failure has to land AFTER real
    DML, same as a driver error between execute and commit would).
    """
    import sqlite3

    things = provider.get_storage(_Thing)
    await things.create(_Thing(id="upd", value="before"))

    real_conn = provider._conn  # noqa: SLF001

    class _FailAfterRealUpdate:
        def __init__(self, real: Any) -> None:
            self._real = real

        async def execute(self, sql: str, *args: Any, **kwargs: Any):  # noqa: ANN201
            cur = await self._real.execute(sql, *args, **kwargs)
            if sql.strip().upper().startswith("UPDATE"):
                raise sqlite3.OperationalError(
                    "simulated failure after the real write landed"
                )
            return cur

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

    provider._conn = _FailAfterRealUpdate(real_conn)  # noqa: SLF001
    try:
        with pytest.raises(Exception):
            await things.update(_Thing(id="upd", value="after"))
    finally:
        provider._conn = real_conn  # noqa: SLF001

    # Before the fix: this read_snapshot's own BEGIN raised "cannot start
    # a transaction within a transaction".
    page = await things.list(OffsetPage(offset=0, length=50))
    assert page.items[0].value == "before"  # the update never committed


async def test_failing_create_inside_transaction_rolls_back_cleanly(
    provider: SqliteStorageProvider,
) -> None:
    """A create() that fails INSIDE an explicit transaction() block, when
    the failure PROPAGATES out of the block, still rolls back the whole
    unit and leaves the connection usable afterward.

    This does NOT by itself prove the reentrant branch stayed untouched:
    once the exception escapes transaction()'s own body, transaction()'s
    existing rollback fires regardless of whether _write_guard's
    reentrant branch wrongly rolled back too -- the observable end state
    here is identical either way. See
    test_txn_catch_and_continue_keeps_the_prior_write_in_the_same_unit
    below for the case that actually discriminates the two (the failure
    caught and handled INSIDE the transaction, which only a wrongly-
    reentrant guard rollback can corrupt).
    """
    from primer.model.except_ import ConflictError

    things = provider.get_storage(_Thing)
    # A write from BEFORE the doomed transaction: proves only the
    # transaction's own writes are lost, unrelated prior data is not.
    await things.create(_Thing(id="before-txn", value="prior"))

    with pytest.raises(ConflictError):
        async with provider.transaction() as conn:
            await things.create(_Thing(id="txn-ok", value="a"), conn=conn)
            # Fails at the SQL level (duplicate id) INSIDE the transaction.
            await things.create(_Thing(id="txn-ok", value="b"), conn=conn)

    # The whole transaction rolled back -- including the FIRST write, even
    # though it succeeded on its own, since it's part of the same atomic
    # unit as the one that failed.
    assert (await things.get("txn-ok")) is None
    # The prior, unrelated write is untouched -- it committed BEFORE this
    # transaction ever opened, not because anything here was caught.
    assert (await things.get("before-txn")) is not None

    # The connection is not wedged: both a read and a fresh write work.
    page = await things.list(OffsetPage(offset=0, length=50))
    assert {t.id for t in page.items} == {"before-txn"}
    await things.create(_Thing(id="after-txn-fail", value="ok"))
    assert (await things.get("after-txn-fail")) is not None


async def test_txn_catch_and_continue_keeps_the_prior_write_in_the_same_unit(
    provider: SqliteStorageProvider,
) -> None:
    """Discriminates a CORRECT guard-level fix from a WRONG one that also
    rolls back in the reentrant branch.

    The previous test (exception propagates out of the whole
    transaction() block) can't tell the two apart: either way the
    transaction rolls back once the exception escapes it, so both an
    unscoped fix and a wrongly-reentrant one produce the same observable
    result there. This test instead CATCHES the failing create()'s
    ConflictError INSIDE the transaction body and continues -- exactly
    the shape a caller doing conditional/idempotent writes inside one
    atomic unit would use. If _write_guard's reentrant (yield False)
    branch wrongly rolled back on its own, write A would be silently
    destroyed the instant create() B fails, before the caller's except
    even runs -- and the subsequent commit() would only ever have B to
    commit. The correct fix leaves the reentrant branch untouched: the
    failed statement's error surfaces to the caller, who decides what to
    do with it, and the transaction commits whatever is left in it.
    """
    from primer.model.except_ import ConflictError

    things = provider.get_storage(_Thing)

    async with provider.transaction() as conn:
        await things.create(_Thing(id="txn-a", value="a"), conn=conn)
        try:
            await things.create(_Thing(id="txn-a", value="dup"), conn=conn)
        except ConflictError:
            pass
        await things.create(_Thing(id="txn-b", value="b"), conn=conn)

    a = await things.get("txn-a")
    b = await things.get("txn-b")
    assert a is not None and a.value == "a"
    assert b is not None and b.value == "b"
