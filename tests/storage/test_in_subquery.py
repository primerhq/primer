"""``field IN IdsOf(model, where)``: the semi-join both SQL backends render (perf plan Task 4 stage B, finding H1).

``IN (SELECT id FROM <model's table> WHERE <where>)``: the id set stays in the database, so SQLite binds no id (no bound-variable limit) and
Postgres ships no id array. The binds of the subquery sit in text order among the others, in ``find`` (both page kinds and the COUNT),
``find_ids`` and ``count_by``; the parent table is created first; a request body cannot build one.
"""
from __future__ import annotations

import pytest

from primer.access.person import Person
from primer.access.policy import visible_predicate
from primer.model.access import Access
from primer.model.collection import Collection, Document
from primer.model.except_ import BadRequestError
from primer.model.provider import SqliteConfig
from primer.model.storage import CursorPage, FieldRef, IdsOf, OffsetPage, Op, Predicate, Value
from primer.storage._predicate import _PredicateTranslator
from primer.storage._sqlite_predicate import _SqlitePredicateTranslator
from primer.storage.sqlite import SqliteStorageProvider
from tests.pg_gate import needs_postgres

pg = needs_postgres("the IN subquery on Postgres")
READER = Person("u-b-in-subquery", "user")


def _eq(field: str, value) -> Predicate:
    return Predicate(left=FieldRef(name=field), op=Op.EQ, right=Value(value=value))


def _scoped(prefix: str) -> Predicate:
    """A bind before the subquery, the subquery (two binds), a bind after it."""
    semi = Predicate(left=FieldRef(name="collection_id"), op=Op.IN, right=IdsOf(model=Collection, where=visible_predicate(READER)))
    before = Predicate(left=_eq("title", "t"), op=Op.AND, right=semi)
    return Predicate(left=before, op=Op.AND, right=Predicate(left=FieldRef(name="slug"), op=Op.NE, right=Value(value=f"{prefix}-skip")))


async def _seed(sp, prefix: str) -> list[str]:
    """7 collections (READER reads 1, 3, 5 by grant and 6 as public), 2 documents each, one of them excluded by the trailing bind."""
    for i in range(7):
        await sp.get_storage(Collection).create(Collection(
            id=f"{prefix}-kb{i}", description="d", access=Access(owners=["u-a"], grants={READER.user_id: "read"} if i % 2 else {}, public=i == 6),
        ))
        for j in range(2):
            slug = f"{prefix}-skip" if (i, j) == (3, 1) else f"{prefix}-s{i}{j}"
            await sp.get_storage(Document).create(Document(id=f"{prefix}-d{i}{j}", collection_id=f"{prefix}-kb{i}", slug=slug, path=slug, title="t"))
    return [f"{prefix}-d{i}{j}" for i in (1, 3, 5, 6) for j in range(2) if (i, j) != (3, 1)]


async def _check(sp, prefix: str, want: list[str]) -> None:
    docs = sp.get_storage(Document)
    predicate = _scoped(prefix)
    page = await docs.find(predicate, OffsetPage(offset=0, length=200))
    assert [d.id for d in page.items if d.id.startswith(prefix)] == want
    full = await docs.find(predicate, OffsetPage(offset=0, length=2))  # a full page: its total comes from the COUNT, with the same binds
    assert full.total == page.total and len(full.items) == 2
    seen, cursor = [], None
    while True:
        cpage = await docs.find(predicate, CursorPage(cursor=cursor, length=3))
        seen += [d.id for d in cpage.items]
        if cpage.next_cursor is None:
            break
        cursor = cpage.next_cursor
    assert [i for i in seen if i.startswith(prefix)] == want
    assert [i for i, _ in await docs.find_ids(predicate) if i.startswith(prefix)] == want
    counts = await docs.count_by("collection_id", [f"{prefix}-kb{i}" for i in range(7)], predicate)
    assert counts == {f"{prefix}-kb1": 2, f"{prefix}-kb3": 1, f"{prefix}-kb5": 2, f"{prefix}-kb6": 2}


async def _sqlite(tmp_path) -> SqliteStorageProvider:
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await sp.initialize()
    return sp


@pytest.mark.asyncio
async def test_sqlite_semi_join_in_every_read(tmp_path):
    sp = await _sqlite(tmp_path)
    want = await _seed(sp, "p")
    await _check(sp, "p", want)
    await sp.aclose()


@pytest.mark.asyncio
async def test_a_child_read_creates_the_parent_table_first(tmp_path):
    sp = await _sqlite(tmp_path)
    await sp.get_storage(Document).create(Document(id="d1", collection_id="kb-x", slug="s", path="s", title="t"))
    assert (await sp.get_storage(Document).find(_scoped("p"), OffsetPage(offset=0, length=20))).items == []
    await sp.aclose()


def test_sqlite_renders_the_subquery_and_binds_no_id():
    sql, params = _SqlitePredicateTranslator(Document, lambda model: f'"{model.__name__.lower()}"').translate(_scoped("p"))
    assert "(json_extract(data, '$.collection_id') IN (SELECT id FROM \"collection\" WHERE " in sql
    assert params == ["t", f"u:{READER.user_id}", "public", "p-skip"]


def test_postgres_renders_the_same_semi_join_with_continuous_placeholders():
    sql, params = _PredicateTranslator(Document, lambda model: f'"s"."{model.__name__.lower()}"').translate(_scoped("p"))
    assert sql == (
        "(((data->>'title' = $1) AND (data->>'collection_id' IN (SELECT id FROM \"s\".\"collection\" WHERE "
        "((data->'access'->'members' ? $2) OR (data->'access'->'members' ? $3))))) AND (data->>'slug' != $4))"
    )
    assert params == ["t", f"u:{READER.user_id}", "public", "p-skip"]


@pytest.mark.parametrize("translator", [_SqlitePredicateTranslator, _PredicateTranslator])
def test_a_translator_without_table_names_refuses_a_subquery(translator):
    with pytest.raises(BadRequestError):
        translator(Document).translate(_scoped("p"))


def test_a_request_body_cannot_build_one():
    """The ``Operand`` union has no tag for it: the same JSON parses to a plain empty ``IN``, which matches nothing."""
    body = {"left": {"kind": "field", "name": "collection_id"}, "op": "in", "right": {"kind": "value", "value": [], "model": "Collection", "where": None}}
    right = Predicate.model_validate(body).right
    assert type(right) is Value and right.value == []


@pg
@pytest.mark.asyncio
async def test_postgres_semi_join_in_every_read(postgres_storage_provider):
    prefix = "perf-t4b"
    try:
        want = await _seed(postgres_storage_provider, prefix)
        await _check(postgres_storage_provider, prefix, want)
    finally:
        for i in range(7):
            for j in range(2):
                if await postgres_storage_provider.get_storage(Document).get(f"{prefix}-d{i}{j}") is not None:
                    await postgres_storage_provider.get_storage(Document).delete(f"{prefix}-d{i}{j}")
            if await postgres_storage_provider.get_storage(Collection).get(f"{prefix}-kb{i}") is not None:
                await postgres_storage_provider.get_storage(Collection).delete(f"{prefix}-kb{i}")
