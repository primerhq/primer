"""Parametrised :class:`Storage` contract — runs against every backend.

Each scenario is asserted on both Postgres (when ``PRIMER_TEST_POSTGRES_URL``
is set) and SQLite. The point is to catch a semantic divergence the
moment it appears, not to re-test the per-backend translator.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.model.storage import (
    CursorPage,
    FieldRef,
    OffsetPage,
    Op,
    OrderBy,
    Predicate,
    Value,
)
from primer.storage.factory import StorageProviderFactory
from primer.storage.postgres import PostgresStorageProvider
from tests.pg_gate import explicit_port, postgres_url, require_postgres_url


class _Thing(Identifiable):
    name: str
    count: int = 0
    status: str = "created"
    workspace_id: str = "w"


_BACKENDS: list = ["sqlite"]
if postgres_url():
    _BACKENDS.append(pytest.param("postgres", marks=pytest.mark.postgres))


def _pg_config_for_test() -> "StorageProviderConfig":
    """Build a Postgres config from PRIMER_TEST_POSTGRES_URL with a unique schema.

    Each test gets its own schema (created by ``initialize``) so the
    contract runs in isolation against a shared test database.
    """
    import uuid
    from urllib.parse import urlparse

    from primer.model.provider import PoolConfig, PostgresConfig

    u = urlparse(require_postgres_url())
    return StorageProviderConfig(
        provider=StorageProviderType.POSTGRES,
        config=PostgresConfig(
            hostname=u.hostname or "localhost",
            port=explicit_port(u),
            username=u.username or "primer",
            password=u.password or "primer",  # type: ignore[arg-type]
            database=(u.path or "/primer_pgtest").lstrip("/") or "primer_pgtest",
            db_schema=f"t{uuid.uuid4().hex[:16]}",
            pool=PoolConfig(min_size=1, max_size=4),
        ),
    )


@pytest_asyncio.fixture(params=_BACKENDS)
async def provider(
    request: pytest.FixtureRequest, tmp_path: Path,
) -> AsyncIterator[StorageProvider]:
    backend = request.param
    if backend == "sqlite":
        cfg = StorageProviderConfig(
            provider=StorageProviderType.SQLITE,
            config=SqliteConfig(path=tmp_path / "contract.sqlite"),
        )
    else:
        cfg = _pg_config_for_test()
    p = StorageProviderFactory.create(cfg)
    await p.initialize()
    try:
        yield p
    finally:
        if backend == "postgres":
            schema = cfg.config.db_schema
            try:
                async with p.pool.acquire() as c:
                    await c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            except Exception:
                pass
            # The "table ensured" cache is keyed by ``id(provider)``. With a
            # fresh per-test schema, a later provider can be allocated at the
            # same address as this one once it is GC'd, aliasing onto a stale
            # entry and skipping the CREATE for the new schema. Evict this
            # provider's entries so the next test always re-creates its table.
            from primer.storage import postgres as _pg
            for key in [k for k in _pg._table_ensured if k[0] == id(p)]:
                _pg._table_ensured.discard(key)
        await p.aclose()


@pytest.mark.asyncio
async def test_get_create_update_delete(provider: StorageProvider) -> None:
    s = provider.get_storage(_Thing)
    assert await s.get("x") is None
    await s.create(_Thing(id="x", name="a", count=1))
    fetched = await s.get("x")
    assert fetched is not None and fetched.name == "a"
    with pytest.raises(ConflictError):
        await s.create(_Thing(id="x", name="dup"))
    updated = await s.update(_Thing(id="x", name="b", count=2))
    assert updated.name == "b"
    await s.delete("x")
    with pytest.raises(NotFoundError):
        await s.delete("x")


@pytest.mark.asyncio
async def test_create_and_delete_accept_conn(provider: StorageProvider) -> None:
    """``create`` and ``delete`` accept a ``conn`` kwarg (mirrors ``update``).

    Passing ``conn=None`` must behave exactly like omitting it: pool-less
    backends (SQLite) ignore it; pooled backends acquire their own.
    """
    store = provider.get_storage(_Thing)
    created = await store.create(_Thing(id="thing-conn", name="x"), conn=None)
    assert created.id == "thing-conn"
    assert (await store.get("thing-conn")) is not None
    await store.delete("thing-conn", conn=None)
    assert (await store.get("thing-conn")) is None

    # Postgres only: open a real transaction and thread it through create.
    if isinstance(provider, PostgresStorageProvider):
        async with provider.pool.acquire() as conn:
            async with conn.transaction():
                await store.create(_Thing(id="thing-tx", name="y"), conn=conn)
        assert (await store.get("thing-tx")) is not None
        await store.delete("thing-tx")


@pytest.mark.asyncio
async def test_update_unless_applies_when_current_field_differs(
    provider: StorageProvider,
) -> None:
    s = provider.get_storage(_Thing)
    await s.create(_Thing(id="uu-1", name="a", status="created"))
    result = await s.update_unless(
        _Thing(id="uu-1", name="b", status="running"),
        field="status", forbidden="ended",
    )
    assert result is not None and result.name == "b" and result.status == "running"
    assert (await s.get("uu-1")).name == "b"


@pytest.mark.asyncio
async def test_update_unless_rejects_when_current_field_matches(
    provider: StorageProvider,
) -> None:
    """The guard checks the row's CURRENT stored value, not the value on
    the `entity` argument being written - that is the whole point (a
    caller passing a stale snapshot must not be able to bypass it)."""
    s = provider.get_storage(_Thing)
    await s.create(_Thing(id="uu-2", name="a", status="ended"))
    result = await s.update_unless(
        _Thing(id="uu-2", name="b", status="running"),
        field="status", forbidden="ended",
    )
    assert result is None
    unchanged = await s.get("uu-2")
    assert unchanged.name == "a" and unchanged.status == "ended", (
        "a rejected update_unless must leave the row completely untouched"
    )


@pytest.mark.asyncio
async def test_update_unless_raises_not_found_for_a_missing_id(
    provider: StorageProvider,
) -> None:
    s = provider.get_storage(_Thing)
    with pytest.raises(NotFoundError):
        await s.update_unless(
            _Thing(id="uu-missing", name="x"), field="status", forbidden="ended",
        )


@pytest.mark.asyncio
async def test_find_predicate_eq_and_in(provider: StorageProvider) -> None:
    s = provider.get_storage(_Thing)
    for i, name in enumerate(["a", "b", "c"]):
        await s.create(_Thing(id=f"t{i}", name=name, count=i))
    hits = await s.find(
        Predicate(left=FieldRef(name="name"), op=Op.EQ, right=Value(value="b")),
        OffsetPage(offset=0, length=10),
    )
    assert {t.name for t in hits.items} == {"b"}
    hits = await s.find(
        Predicate(
            left=FieldRef(name="count"),
            op=Op.IN,
            right=Value(value=[0, 2]),
        ),
        OffsetPage(offset=0, length=10),
    )
    assert sorted(t.count for t in hits.items) == [0, 2]


@pytest.mark.asyncio
async def test_orderby_and_pagination(provider: StorageProvider) -> None:
    s = provider.get_storage(_Thing)
    for i in range(5):
        await s.create(_Thing(id=f"r{i:02d}", name="x", count=i))
    page1 = await s.list(
        OffsetPage(offset=0, length=2),
        order_by=[OrderBy(field="count", direction="desc")],
    )
    page2 = await s.list(
        OffsetPage(offset=2, length=2),
        order_by=[OrderBy(field="count", direction="desc")],
    )
    counts = [t.count for t in page1.items] + [t.count for t in page2.items]
    assert counts == [4, 3, 2, 1]


@pytest.mark.asyncio
async def test_cursor_walk_terminates_and_visits_each_once(
    provider: StorageProvider,
) -> None:
    """Regression for e2e T0730: a no-predicate cursor walk must
    TERMINATE (final page returns ``next_cursor=None``) and visit
    every row exactly once -- no infinite loop, no repeats, no gaps.

    Pins the keyset-seek invariant: ``next_cursor`` is only emitted
    when a look-ahead row beyond the requested page exists, so the
    final page closes the walk.
    """
    s = provider.get_storage(_Thing)
    seeded = {f"r{i:02d}" for i in range(5)}
    for i in range(5):
        await s.create(_Thing(id=f"r{i:02d}", name="x", count=i))

    seen: list[str] = []
    cursor: str | None = None
    for _page in range(40):  # bounded safety net, mirrors the e2e test
        resp = await s.find(None, CursorPage(cursor=cursor, length=2))
        seen.extend(t.id for t in resp.items)
        cursor = resp.next_cursor
        if cursor is None:
            break
    else:  # pragma: no cover - only hit on the regression we're guarding
        pytest.fail("cursor walk did not terminate within 40 pages")

    # Each seeded id appears exactly once -- no repeats (cursor advanced
    # past the last item) and none missed (the walk covered everything).
    assert sorted(seen) == sorted(seeded)
    assert len(seen) == len(set(seen)), f"cursor walk repeated ids: {seen!r}"


@pytest.mark.asyncio
async def test_find_multi_clause_and_predicate_matches(
    provider: StorageProvider,
) -> None:
    """Regression for e2e T0802: a binary AND predicate
    (``workspace_id == X AND status == S``) must return rows
    matching BOTH clauses and exclude rows matching only one.

    Pins the predicate-composition path / query builder: the AND is
    translated to ``(left) AND (right)`` and matching rows are
    returned (the e2e failure was a stale test premise -- sessions
    never reached the asserted status -- not a query-builder bug;
    this locks the builder's correctness for matching rows).
    """
    s = provider.get_storage(_Thing)
    # Three rows on workspace "wa" with the target status; one on "wa"
    # with a different status; one on "wb" with the target status.
    await s.create(_Thing(id="m0", name="x", status="ended", workspace_id="wa"))
    await s.create(_Thing(id="m1", name="x", status="ended", workspace_id="wa"))
    await s.create(_Thing(id="m2", name="x", status="ended", workspace_id="wa"))
    await s.create(_Thing(id="other-status", name="x", status="created", workspace_id="wa"))
    await s.create(_Thing(id="other-ws", name="x", status="ended", workspace_id="wb"))

    pred = Predicate(
        left=Predicate(
            left=FieldRef(name="workspace_id"),
            op=Op.EQ,
            right=Value(value="wa"),
        ),
        op=Op.AND,
        right=Predicate(
            left=FieldRef(name="status"),
            op=Op.EQ,
            right=Value(value="ended"),
        ),
    )
    hits = await s.find(pred, OffsetPage(offset=0, length=100))
    got = {t.id for t in hits.items}
    assert got == {"m0", "m1", "m2"}, got
    assert "other-status" not in got  # matched only the workspace clause
    assert "other-ws" not in got      # matched only the status clause


# ---------------------------------------------------------------------------
# Storage.patch_if: field-scoped compare-and-set, on every backend
# ---------------------------------------------------------------------------

from tests.storage import _patch_scenarios as _ps  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _ps.ALL, ids=lambda f: f.__name__)
async def test_patch_if_contract(provider: StorageProvider, scenario: Any) -> None:
    await scenario(provider.get_storage(_ps.PatchDoc))


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _ps.RAW, ids=lambda f: f.__name__)
async def test_patch_if_raw_document_contract(provider: StorageProvider, scenario: Any) -> None:
    await scenario(_ps.ProviderEnv(provider))


@pytest.mark.asyncio
async def test_patch_if_appends_the_updated_event_for_a_registered_kind(
    provider: StorageProvider, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same CRUD event as ``update``: ``<kind>.updated``, payload = the stored document after the write."""
    from primer.events import registry

    monkeypatch.setitem(registry._EVENT_KINDS, "patchdoc", _ps.PatchDoc)
    monkeypatch.setitem(registry._KIND_BY_MODEL, _ps.PatchDoc, "patchdoc")
    store = provider.get_storage(_ps.PatchDoc)
    await store.create(_ps.PatchDoc(id="ev1", status="created", state={}))
    assert await store.patch_if("ev1", {"status": "done"}, where={"status": ["running"]}) is None
    out = await store.patch_if(
        "ev1", {"status": "done"}, where={"status": ["created"]},
        set_paths={("state", "k"): 1},
    )
    assert out is not None
    events = await provider.get_event_store().read_after(0)
    updated = [e for e in events if e.event_type == "patchdoc.updated"]
    assert len(updated) == 1, "a rejected patch must not emit an event, an applied one must emit exactly one"
    assert updated[0].entity_id == "ev1"
    assert updated[0].payload["status"] == "done" and updated[0].payload["state"] == {"k": 1}


@pytest.mark.asyncio
async def test_patch_if_event_payload_is_the_final_canonical_document_after_a_loose_patch(
    provider: StorageProvider, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical rewrite is a second statement; the event must carry ITS result, not the first statement's."""
    from primer.events import registry

    monkeypatch.setitem(registry._EVENT_KINDS, "patchdoc", _ps.PatchDoc)
    monkeypatch.setitem(registry._KIND_BY_MODEL, _ps.PatchDoc, "patchdoc")
    store = provider.get_storage(_ps.PatchDoc)
    await store.create(_ps.PatchDoc(id="ev2", status="created"))
    out = await store.patch_if("ev2", {"count": "5", "flag": 1}, where={"status": ["created"]})
    assert out is not None and (out.count, out.flag) == (5, True)
    events = await provider.get_event_store().read_after(0)
    (updated,) = [e for e in events if e.event_type == "patchdoc.updated" and e.entity_id == "ev2"]
    assert updated.payload["count"] == 5 and updated.payload["flag"] is True, updated.payload


@pytest.mark.asyncio
async def test_patch_if_accepts_conn_and_rolls_back_with_the_caller_transaction(
    provider: StorageProvider,
) -> None:
    store = provider.get_storage(_ps.PatchDoc)
    await store.create(_ps.PatchDoc(id="tx1", status="created"))
    assert await store.patch_if("tx1", {"count": 1}, where={"status": ["created"]}, conn=None) is not None
    if isinstance(provider, PostgresStorageProvider):
        async with provider.pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            out = await store.patch_if(
                "tx1", {"status": "inside"}, where={"status": ["created"]}, conn=conn,
            )
            assert out is not None and out.status == "inside"
            await tx.rollback()
        fresh = await store.get("tx1")
        assert fresh is not None and fresh.status == "created"


@pytest.mark.asyncio
async def test_patch_if_two_writers_to_different_leaves_of_one_object_both_survive(
    provider: StorageProvider,
) -> None:
    """Two connections write different leaves of the SAME nested object; the second blocks on the
    first's row lock and then re-evaluates against the first's committed version, so neither erases the
    other (a whole-document write from a snapshot would)."""
    if not isinstance(provider, PostgresStorageProvider):
        pytest.skip("two real connections are a Postgres property")
    import asyncio

    store = provider.get_storage(_ps.PatchDoc)
    await store.create(_ps.PatchDoc(id="race", state={"ps": {}}))
    async with provider.pool.acquire() as c1, provider.pool.acquire() as c2:
        tx1 = c1.transaction()
        await tx1.start()
        first = await store.patch_if(
            "race", None, where={"status": ["created"]}, set_paths={("state", "ps", "a"): 1}, conn=c1,
        )
        assert first is not None

        async def second() -> Any:
            return await store.patch_if(
                "race", None, where={"status": ["created"]},
                set_paths={("state", "ps", "b"): 2}, conn=c2,
            )

        task = asyncio.create_task(second())
        await _wait_until_blocked_by(provider, c1)
        assert not task.done(), "the second writer should be waiting on the first's row lock"
        await tx1.commit()
        out = await asyncio.wait_for(task, timeout=5)
    assert out is not None
    fresh = await store.get("race")
    assert fresh is not None and fresh.state == {"ps": {"a": 1, "b": 2}}


async def _wait_until_blocked_by(provider: PostgresStorageProvider, blocker_conn: Any) -> None:
    """Poll until some backend is waiting on a lock held by ``blocker_conn``'s backend (not a sleep,
    and not any unrelated lock wait on the server)."""
    import asyncio

    blocker_pid = blocker_conn.get_server_pid()
    async with provider.pool.acquire() as probe:
        for _ in range(100):
            waiting = await probe.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE $1 = ANY(pg_blocking_pids(pid)) AND pid <> pg_backend_pid()",
                blocker_pid,
            )
            if waiting:
                return
            await asyncio.sleep(0.05)
    raise AssertionError("no writer ever blocked on the first writer's row lock")


@pytest.mark.asyncio
async def test_patch_if_reevaluates_the_guard_against_the_version_a_blocked_writer_waited_for(
    provider: StorageProvider,
) -> None:
    """The CAS property itself: writer 1 changes the GUARDED field and commits while writer 2 waits
    on the row lock holding the OLD guard. Postgres re-checks writer 2's WHERE against writer 1's
    committed version, so writer 2 is rejected (None) instead of overwriting."""
    if not isinstance(provider, PostgresStorageProvider):
        pytest.skip("two real connections are a Postgres property")
    import asyncio

    store = provider.get_storage(_ps.PatchDoc)
    await store.create(_ps.PatchDoc(id="flip", status="created", count=0))
    async with provider.pool.acquire() as c1, provider.pool.acquire() as c2:
        tx1 = c1.transaction()
        await tx1.start()
        first = await store.patch_if(
            "flip", {"status": "done"}, where={"status": ["created"]}, conn=c1,
        )
        assert first is not None

        async def second() -> Any:
            return await store.patch_if(
                "flip", {"count": 99}, where={"status": ["created"]}, conn=c2,
            )

        task = asyncio.create_task(second())
        await _wait_until_blocked_by(provider, c1)
        assert not task.done(), "writer 2 must be blocked on writer 1's row lock, or the re-check is untested"
        await tx1.commit()
        out = await asyncio.wait_for(task, timeout=5)
    assert out is None, "the stale-guard writer overwrote the committed change"
    fresh = await store.get("flip")
    assert fresh is not None and (fresh.status, fresh.count) == ("done", 0)


@pytest.mark.asyncio
async def test_patch_if_inside_the_sqlite_transaction_rolls_back_with_it(tmp_path: Path) -> None:
    """SQLite-only (its own provider, not the parametrised one: a skip under the Postgres parameter
    would trip the lane's anti-silent-skip guard)."""
    from primer.storage.sqlite import SqliteStorageProvider

    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "txn.sqlite"))
    await provider.initialize()
    try:
        store = provider.get_storage(_ps.PatchDoc)
        await store.create(_ps.PatchDoc(id="tx-sqlite", status="created"))
        with pytest.raises(RuntimeError):
            async with provider.transaction():
                out = await store.patch_if(
                    "tx-sqlite", {"status": "inside"}, where={"status": ["created"]},
                )
                assert out is not None and out.status == "inside"
                raise RuntimeError("abort the surrounding transaction")
        fresh = await store.get("tx-sqlite")
        assert fresh is not None and fresh.status == "created"
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_patch_if_with_a_registered_kind_inside_a_rolled_back_transaction_emits_no_event(
    provider: StorageProvider, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not isinstance(provider, PostgresStorageProvider):
        pytest.skip("conn passthrough is a Postgres property")
    from primer.events import registry

    monkeypatch.setitem(registry._EVENT_KINDS, "patchdoc", _ps.PatchDoc)
    monkeypatch.setitem(registry._KIND_BY_MODEL, _ps.PatchDoc, "patchdoc")
    store = provider.get_storage(_ps.PatchDoc)
    await store.create(_ps.PatchDoc(id="evtx", status="created"))
    async with provider.pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        assert await store.patch_if(
            "evtx", {"status": "x"}, where={"status": ["created"]}, conn=conn,
        ) is not None
        await tx.rollback()
    events = await provider.get_event_store().read_after(0)
    assert [e for e in events if e.event_type == "patchdoc.updated"] == []
    fresh = await store.get("evtx")
    assert fresh is not None and fresh.status == "created"


@pytest.mark.asyncio
async def test_patch_if_failing_validation_inside_a_sqlite_transaction_leaves_no_trace(tmp_path: Path) -> None:
    """A caller may catch the ValidationError and carry on inside its transaction: the bad document
    must not survive into the commit, and the transaction's other writes must."""
    from pydantic import ValidationError

    from primer.storage.sqlite import SqliteStorageProvider

    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "np1.sqlite"))
    await provider.initialize()
    try:
        store = provider.get_storage(_ps.PatchDoc)
        await store.create(_ps.PatchDoc(id="np1", status="created", count=1))
        async with provider.transaction():
            assert await store.patch_if("np1", {"token": "before"}, where={"status": ["created"]}) is not None
            with pytest.raises(ValidationError):
                await store.patch_if("np1", {"count": "not-a-number"}, where={"status": ["created"]})
            assert await store.patch_if("np1", {"flag": True}, where={"status": ["created"]}) is not None
        fresh = await store.get("np1")                 # reads fine: the bad write was undone
        assert fresh is not None
        assert (fresh.count, fresh.token, fresh.flag) == (1, "before", True)
    finally:
        await provider.aclose()


#: The scenarios whose calls ``patch_if`` refuses with a ``PatchSpecError`` (or a ``ValidationError``), picked from the
#: shared tables as they are. A refusal raised AFTER the UPDATE (the model's canonical dump: a non-finite float, a leaf
#: the model drops; an unreadable document) goes through the SQLite savepoint only when the call runs inside the caller's
#: own transaction; standalone it is the write guard's rollback.
_REFUSAL_SCENARIOS = [
    _ps.malformed_specs_are_rejected_with_value_error,
    _ps.an_empty_or_malformed_where_is_rejected,
    _ps.a_patch_that_leaves_the_row_unreadable_is_rejected_and_rolled_back,
    _ps.a_value_the_model_coerces_to_a_non_finite_float_is_refused_and_the_row_is_unchanged,
]
_RAW_REFUSAL_SCENARIOS = [
    _ps.patching_a_field_the_model_does_not_have_is_rejected,
    _ps.a_set_paths_leaf_under_a_typed_sub_model_is_canonicalised_and_a_typo_is_refused,
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario, raw",
    [(s, False) for s in _REFUSAL_SCENARIOS] + [(s, True) for s in _RAW_REFUSAL_SCENARIOS],
    ids=lambda v: v.__name__ if callable(v) else ("raw" if v else "store"),
)
async def test_patch_if_refusals_inside_a_sqlite_transaction_raise_and_leave_no_trace(
    tmp_path: Path, scenario: Any, raw: bool,
) -> None:
    """The refusal scenarios, each run inside ``SqliteStorageProvider.transaction()``, the savepoint path: every refused
    call still raises (as itself), the row reads unchanged within the transaction (the scenario's own checks), and the
    transaction stays usable and commits its other writes. SQLite-only, on its own provider (see above)."""
    from primer.storage.sqlite import SqliteStorageProvider

    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "refusals.sqlite"))
    await provider.initialize()
    try:
        store = provider.get_storage(_ps.PatchDoc)
        async with provider.transaction():
            await scenario(_ps.ProviderEnv(provider) if raw else store)
            await store.create(_ps.PatchDoc(id="after-the-refusals"))
        assert await store.get("after-the-refusals") is not None, "the transaction did not commit after the refusals"
    finally:
        await provider.aclose()
