"""PostgresStorage.patch_if's canonical rewrite, driven through a scripted connection (no database).

The Postgres backend cannot run in the local unit lane, and the contract scenarios that prove "stored equals
canonical" run there only in the CI Postgres lane. This pins the statement sequence the backend issues, so a
regression in the rewrite step shows up here too: one UPDATE for the patch, a second one only when what the
patch wrote differs from the model's canonical dump, both on the caller's connection, one CRUD event with the
FINAL document.
"""

from __future__ import annotations

import json

import pytest

from primer.storage._patch import PatchSpecError
from primer.storage.postgres import PostgresStorage, _table_ensured
from tests.storage._patch_scenarios import StrictDoc


TXN_BEGIN = "<transaction begin>"
TXN_END = "<transaction end>"


class _Row(dict):
    pass


class _Provider:
    schema = "public"

    class pool:  # noqa: N801 - must never be reached: the caller supplies the connection
        @staticmethod
        def acquire():
            raise AssertionError("pool.acquire() must not be called when conn is supplied")

    async def _ensure_events_schema(self) -> None:
        return


class _ScriptedConn:
    def __init__(self, *rows: _Row | None) -> None:
        self._rows = list(rows)
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.calls.append((sql, args))
        return self._rows.pop(0)

    async def fetchval(self, sql: str, *args):
        self.calls.append((sql, args))
        return 1

    async def execute(self, sql: str, *args):
        self.calls.append((sql, args))
        return "INSERT 0 1"

    def transaction(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def _txn():
            self.calls.append((TXN_BEGIN, ()))
            try:
                yield
            finally:
                self.calls.append((TXN_END, ()))

        return _txn()


def _storage() -> PostgresStorage[StrictDoc]:
    storage = PostgresStorage[StrictDoc](provider=_Provider(), model_class=StrictDoc)
    _table_ensured.add((id(storage._provider), StrictDoc))
    return storage


def _row(**data) -> _Row:
    return _Row(id="a", data=json.dumps({"status": "created", **data}))


@pytest.mark.asyncio
async def test_a_loosely_typed_patch_is_followed_by_one_canonical_rewrite_on_the_same_connection():
    conn = _ScriptedConn(
        _row(count="5", tag="MiXeD"),                  # what the patch statement produced: the caller's JSON
        _row(count=5, tag="mixed"),                    # what the rewrite statement returns
    )
    out = await _storage().patch_if(
        "a", {"count": "5", "tag": "MiXeD"}, where={"status": ["created"]}, conn=conn,
    )

    assert out is not None and (out.count, out.tag) == (5, "mixed")
    updates = [(sql, args) for sql, args in conn.calls if sql.lstrip().startswith("UPDATE")]
    assert len(updates) == 2, "the patch, then ONE rewrite of the touched fields"
    assert json.loads(updates[0][1][1]) == {"count": "5", "tag": "MiXeD"}
    rewrite_sql, rewrite_args = updates[1]
    assert "WHERE id = $1 RETURNING id, data" in rewrite_sql, "the rewrite is unguarded: the row lock is already held"
    assert rewrite_args[0] == "a"
    assert json.loads(rewrite_args[1]) == {"count": 5, "tag": "mixed"}, "only the touched fields, in canonical form"


@pytest.mark.asyncio
async def test_a_patch_that_is_already_canonical_issues_no_rewrite():
    conn = _ScriptedConn(_row(count=5, tag="mixed"))
    out = await _storage().patch_if("a", {"count": 5, "tag": "mixed"}, where={"status": ["created"]}, conn=conn)

    assert out is not None and out.count == 5
    assert len([sql for sql, _ in conn.calls if sql.lstrip().startswith("UPDATE")]) == 1


@pytest.mark.asyncio
async def test_a_rejected_patch_issues_one_update_and_no_rewrite():
    conn = _ScriptedConn(None)
    out = await _storage().patch_if("a", {"count": "5"}, where={"status": ["running"]}, conn=conn)

    assert out is None
    assert len([sql for sql, _ in conn.calls if sql.lstrip().startswith("UPDATE")]) == 1


def _first_update(conn: "_ScriptedConn") -> str:
    return next(sql for sql, _ in conn.calls if sql.lstrip().startswith("UPDATE"))


@pytest.mark.asyncio
async def test_a_guard_naming_a_default_also_matches_the_absent_field_in_the_compiled_sql():
    names_default = _ScriptedConn(_row())
    await _storage().patch_if("a", {"status": "x"}, where={"gen": [0]}, conn=names_default)
    names_other = _ScriptedConn(_row())
    await _storage().patch_if("a", {"status": "x"}, where={"gen": [1]}, conn=names_other)

    assert "IS NULL" in _first_update(names_default), "gen defaults to 0, so a guard on 0 also matches an absent gen"
    assert "IS NULL" not in _first_update(names_other), "a guard on another value must not"


@pytest.mark.asyncio
async def test_an_unknown_field_is_refused_before_any_statement():
    conn = _ScriptedConn()
    with pytest.raises(PatchSpecError):
        await _storage().patch_if("a", {"cnt": 1}, where={"status": ["created"]}, conn=conn)
    assert conn.calls == []


def _trace(conn: _ScriptedConn) -> list[str]:
    out: list[str] = []
    for sql, _ in conn.calls:
        if sql == TXN_BEGIN:
            out.append("begin")
        elif sql == TXN_END:
            out.append("end")
        elif sql.lstrip().startswith("UPDATE"):
            out.append("update")
        elif sql.lstrip().startswith("INSERT INTO") and ".events" in sql:
            out.append("event")
        else:
            out.append("other")
    return out


@pytest.mark.asyncio
async def test_both_updates_and_the_event_fall_inside_one_transaction(monkeypatch):
    """The atomicity the canonical rewrite depends on: the patch, the rewrite and the CRUD event are ONE transaction on the
    caller's connection, so a reader never sees the loose form and a rollback undoes all three together."""
    monkeypatch.setattr("primer.storage.postgres.kind_for_model", lambda model: "strictdoc")
    conn = _ScriptedConn(_row(count="5", tag="MiXeD"), _row(count=5, tag="mixed"))

    out = await _storage().patch_if(
        "a", {"count": "5", "tag": "MiXeD"}, where={"status": ["created"]}, conn=conn,
    )

    assert out is not None
    assert _trace(conn) == ["begin", "update", "update", "event", "end"]


@pytest.mark.asyncio
async def test_a_rejected_patch_opens_and_closes_its_transaction_around_the_one_update(monkeypatch):
    monkeypatch.setattr("primer.storage.postgres.kind_for_model", lambda model: "strictdoc")
    conn = _ScriptedConn(None)

    out = await _storage().patch_if("a", {"count": "5"}, where={"status": ["running"]}, conn=conn)

    assert out is None
    # the existence probe that tells "rejected" from "missing" comes after the transaction, and writes nothing
    assert _trace(conn) == ["begin", "update", "end", "other"]
