"""The tree's read-write-read diff against the READ COMMITTED sub-statement window (review
2026-11-23 X2.3, the M1 residual).

A child committed on a peer connection between the in-transaction subtree walk and the write slips
past every read the walk does: under READ COMMITTED each statement takes a fresh snapshot, so a
child committed after the walk's last statement is not in the set the write operates on. On
Postgres the window opens; on SQLite the single connection serialises it shut, so this runs on
Postgres only (gate: ``PRIMER_TEST_POSTGRES_URL``, see ``tests/pg_gate.py``). move and delete now
re-read the children of the walked set after their write - on the TRANSACTION'S OWN connection (a
fresh pooled connection on Postgres cannot see the transaction's own uncommitted writes, so a
fresh-connection re-read after the delete's writes sees the just-deleted children still present
and conflicts on every non-leaf delete) - and a changed child set is a ConflictError
(rollback, retry the operation), not a row left addressed by a path whose prefix moved, or a child
orphaned under a deleted parent.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from primer.knowledge.tree import DocumentTreeService
from primer.model.except_ import ConflictError
from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider
from tests.pg_gate import explicit_port, needs_postgres, require_postgres_url

_WHAT = "the tree mid-walk conflict tests"
pg = needs_postgres(_WHAT)

_C = "c1"


@pytest_asyncio.fixture
async def provider():
    """A Postgres provider on a throwaway schema of the gated database, dropped afterwards.

    Pool max 4: a move/delete holds one connection in its transaction while the injected peer
    create takes a second one.
    """
    url = urlparse(require_postgres_url(_WHAT))
    schema = f"treemid{uuid.uuid4().hex[:16]}"
    sp = PostgresStorageProvider(PostgresConfig(
        hostname=url.hostname or "localhost",
        port=explicit_port(url),
        username=url.username or "primer",
        password=url.password or "",  # type: ignore[arg-type]
        database=(url.path or "/").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4),
    ))
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    try:
        yield sp
    finally:
        async with sp.pool.acquire() as c:
            await c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await sp.aclose()


@pytest_asyncio.fixture
async def tree(provider):
    yield DocumentTreeService(provider)


@pg
@pytest.mark.asyncio
async def test_move_conflicts_when_a_peer_child_lands_under_the_subtree(tree):
    """A peer commits a grandchild under the (not-yet-patched) descendant between the move's
    in-transaction walk and its write: the move is a conflict with nothing moved, not a child whose
    path keeps the old prefix (the 3.6 symptom narrowed to the sub-statement window)."""
    await tree.create(collection_id=_C, parent="", slug="dst", body="")
    await tree.create(collection_id=_C, parent="", slug="a", body="A")
    await tree.create(collection_id=_C, parent="a", slug="kid", body="K")
    original = tree._descendants
    late = []

    async def walk(collection_id, root, conn=None):
        out = await original(collection_id, root, conn=conn)
        # The move's single walk runs INSIDE its transaction; this create lands on a second pool
        # connection and commits there, after the walk's last statement and before the write loop.
        late.append(await tree.create(collection_id=_C, parent="a/kid", slug="late", body="L"))
        return out

    tree._descendants = walk
    with pytest.raises(ConflictError) as ei:
        await tree.move(collection_id=_C, path="a", new_parent="dst")
    # The conflict is caused by the peer's committed child - the re-read saw it
    # (a conflict that names nothing would let a re-read that never saw the
    # peer commit pass this test).
    assert late[0].id in str(ei.value)
    res = await tree.read(collection_id=_C, path="a")
    assert res.document.path == "a"  # the move rolled back whole
    res = await tree.read(collection_id=_C, path="a/kid/late")
    assert res.body == "L"  # the peer's child is where the peer put it


@pg
@pytest.mark.asyncio
async def test_recursive_delete_conflicts_when_a_peer_child_lands(tree):
    """A peer commits a child between the delete's in-transaction re-read and its write: a conflict
    with nothing deleted, not a child orphaned under a deleted parent."""
    parent = await tree.create(collection_id=_C, parent="", slug="a", body="A")
    await tree.create(collection_id=_C, parent="a", slug="kid", body="K")
    original = tree._descendants
    calls = 0

    async def walk(collection_id, root, conn=None):
        nonlocal calls
        calls += 1
        out = await original(collection_id, root, conn=conn)
        if calls == 2:  # the in-transaction re-read (the first call is the pre-write check)
            await tree.create(collection_id=_C, parent="a", slug="late", body="L")
        return out

    tree._descendants = walk
    with pytest.raises(ConflictError) as ei:
        await tree.delete(collection_id=_C, path="a", recursive=True)
    # The conflict is caused by the peer's committed child - the re-read saw it.
    # (Before the re-read ran on the transaction's own connection it saw the
    # un-deleted child instead and conflicted for every non-leaf delete.)
    assert "a/late" in str(ei.value)
    res = await tree.read(collection_id=_C, path="a")
    assert res.document.id == parent.id  # nothing was deleted
    res = await tree.read(collection_id=_C, path="a/late")
    assert res.body == "L"


@pg
@pytest.mark.asyncio
async def test_recursive_delete_of_a_subtree_succeeds_and_removes_everything(tree):
    """The green path: a recursive delete of a parent with a child and a grandchild
    removes all of them (before the re-read ran on the transaction's own connection,
    the fresh-connection re-read could not see the transaction's own uncommitted
    deletes and this raised a ConflictError for every non-leaf delete)."""
    from primer.model.except_ import NotFoundError

    await tree.create(collection_id=_C, parent="", slug="a", body="A")
    await tree.create(collection_id=_C, parent="a", slug="kid", body="K")
    await tree.create(collection_id=_C, parent="a/kid", slug="grand", body="G")
    removed = await tree.delete(collection_id=_C, path="a", recursive=True)
    assert len(removed) == 3
    for path in ("a", "a/kid", "a/kid/grand"):
        with pytest.raises(NotFoundError):
            await tree.resolve(collection_id=_C, path=path)


@pg
@pytest.mark.asyncio
async def test_move_carrying_a_subtree_succeeds(tree):
    """The green-path guard for the move's post-write re-read: moving a subtree with a
    grandchild carries the whole subtree and the re-read does not conflict with the
    move's own (path-changed) writes."""
    await tree.create(collection_id=_C, parent="", slug="dst", body="")
    await tree.create(collection_id=_C, parent="", slug="a", body="A")
    await tree.create(collection_id=_C, parent="a", slug="kid", body="K")
    await tree.create(collection_id=_C, parent="a/kid", slug="grand", body="G")
    moved = await tree.move(collection_id=_C, path="a", new_parent="dst")
    assert moved.path == "dst/a"
    res = await tree.read(collection_id=_C, path="dst/a/kid/grand")
    assert res.body == "G"
