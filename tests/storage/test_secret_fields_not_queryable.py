"""A field that holds a secret cannot be searched, sorted or sought through a cursor (ticket 01a1212a), on both backends.

A row is stored with its secrets in clear (``dump_for_storage``) and served masked, so a predicate / sort key / cursor seek key on such a field
compares against the clear value the read hides. The classifier that refuses those paths is :mod:`primer.storage.secret_fields`; these tests pin:

* ``find`` with a predicate on a secret-bearing field is refused (422 ``validation-error``);
* ``order_by`` on a secret-bearing field is refused;
* a cursor from the server still pages every row once, and a cursor whose keys do not match the request's ``order_by`` is refused;
* a cursor whose seek value has the wrong type for an allowed, non-secret key is refused as a bad cursor on both backends (not a Postgres 5xx);
* plain fields keep working, on SQLite and (when gated) Postgres.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from primer.int.storage_provider import StorageProvider
from primer.model.except_ import BadRequestError, ValidationError
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.model.providers.llm import LLMProvider
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
from tests.pg_gate import explicit_port, postgres_url, require_postgres_url


_BACKENDS: list = ["sqlite"]
if postgres_url():
    _BACKENDS.append(pytest.param("postgres", marks=pytest.mark.postgres))


def _pg_config_for_test() -> StorageProviderConfig:
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
            config=SqliteConfig(path=tmp_path / "secq.sqlite"),
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
            from primer.storage import postgres as _pg
            for key in [k for k in _pg._table_ensured if k[0] == id(p)]:
                _pg._table_ensured.discard(key)
        await p.aclose()


def _llm(i: int, host_pw: str) -> LLMProvider:
    return LLMProvider.model_validate(
        {
            "id": f"llm-{i}",
            "provider": "openchat",
            "models": [{"name": "m", "context_length": 8192}],
            "config": {"url": f"http://svc:{host_pw}@px.lan/v1", "api_key": f"sk-{host_pw}-1234", "flavor": "other"},
            "limits": {"max_concurrency": 1},
        }
    )


_SECRET_PATHS = ["config.url", "config.api_key", "config"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _SECRET_PATHS)
async def test_find_like_on_a_secret_field_is_refused(provider: StorageProvider, path: str) -> None:
    st = provider.get_storage(LLMProvider)
    await st.create(_llm(0, "alpha"))
    pred = Predicate(left=FieldRef(name=path), op=Op.LIKE, right=Value(value="http://svc:a%"))
    with pytest.raises(ValidationError):
        await st.find(pred, OffsetPage(offset=0, length=1))


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _SECRET_PATHS)
async def test_order_by_on_a_secret_field_is_refused(provider: StorageProvider, path: str) -> None:
    st = provider.get_storage(LLMProvider)
    await st.create(_llm(0, "alpha"))
    with pytest.raises(ValidationError):
        await st.list(OffsetPage(offset=0, length=1), order_by=[OrderBy(field=path, direction="asc")])
    with pytest.raises(ValidationError):
        await st.find(None, CursorPage(cursor=None, length=1), order_by=[OrderBy(field=path, direction="asc")])


@pytest.mark.asyncio
async def test_find_predicate_anywhere_in_the_tree_is_refused(provider: StorageProvider) -> None:
    st = provider.get_storage(LLMProvider)
    await st.create(_llm(0, "alpha"))
    tree = Predicate(
        left=Predicate(left=FieldRef(name="provider"), op=Op.EQ, right=Value(value="openchat")),
        op=Op.AND,
        right=Predicate(left=FieldRef(name="config.api_key"), op=Op.LIKE, right=Value(value="sk-a%")),
    )
    with pytest.raises(ValidationError):
        await st.find(tree, OffsetPage(offset=0, length=50))


@pytest.mark.asyncio
async def test_plain_field_find_and_order_still_work(provider: StorageProvider) -> None:
    st = provider.get_storage(LLMProvider)
    for i, pw in enumerate(["alpha", "bravo", "charlie"]):
        await st.create(_llm(i, pw))
    page = await st.find(
        Predicate(left=FieldRef(name="provider"), op=Op.EQ, right=Value(value="openchat")),
        OffsetPage(offset=0, length=50),
        order_by=[OrderBy(field="id", direction="asc")],
    )
    assert [r.id for r in page.items] == ["llm-0", "llm-1", "llm-2"]


@pytest.mark.asyncio
async def test_cursor_pagination_on_a_plain_field_visits_every_row_once(provider: StorageProvider) -> None:
    st = provider.get_storage(LLMProvider)
    for i, pw in enumerate(["e", "d", "c", "b", "a"]):
        await st.create(_llm(i, pw))
    order = [OrderBy(field="provider", direction="asc")]  # same for every row, so the id tiebreaker drives it
    seen: list[str] = []
    page = await st.list(CursorPage(cursor=None, length=2), order_by=order)
    for _ in range(10):
        seen.extend(r.id for r in page.items)
        if not page.next_cursor:
            break
        page = await st.list(CursorPage(cursor=page.next_cursor, length=2), order_by=order)
    assert sorted(seen) == ["llm-0", "llm-1", "llm-2", "llm-3", "llm-4"]
    assert len(seen) == len(set(seen))


@pytest.mark.asyncio
async def test_default_id_order_cursor_visits_every_row_once(provider: StorageProvider) -> None:
    st = provider.get_storage(LLMProvider)
    for i, pw in enumerate(["e", "d", "c", "b", "a"]):
        await st.create(_llm(i, pw))
    seen: list[str] = []
    page = await st.list(CursorPage(cursor=None, length=2))
    for _ in range(10):
        seen.extend(r.id for r in page.items)
        if not page.next_cursor:
            break
        page = await st.list(CursorPage(cursor=page.next_cursor, length=2))
    assert seen == ["llm-0", "llm-1", "llm-2", "llm-3", "llm-4"]


def _forge_cursor(keys: list[dict]) -> str:
    payload = json.dumps({"keys": keys}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()


@pytest.mark.asyncio
async def test_a_wrongly_typed_cursor_value_on_an_allowed_key_is_a_bad_cursor(provider: StorageProvider) -> None:
    # ``last_probe_ok`` is a top-level bool field, not a secret, that the
    # request may legitimately sort by. A cursor that carries a string for it
    # would bind against the backend's typed column expression and answer a
    # 5xx on Postgres (and 0 rows on SQLite); it must be refused as a bad
    # cursor on both backends instead.
    st = provider.get_storage(LLMProvider)
    await st.create(_llm(0, "alpha"))
    order = [OrderBy(field="last_probe_ok", direction="asc")]
    forged = _forge_cursor(
        [
            {"field": "last_probe_ok", "value": "not-a-bool", "direction": "asc", "is_null": False},
            {"field": "id", "value": "llm-0", "direction": "asc", "is_null": False},
        ]
    )
    with pytest.raises(BadRequestError):
        await st.list(CursorPage(cursor=forged, length=1), order_by=order)
    # The server's own cursor for the same sort (a real bool) is accepted and
    # pages normally.
    await st.create(_llm(1, "bravo"))
    page = await st.list(CursorPage(cursor=None, length=1), order_by=order)
    assert page.next_cursor is not None
    nxt = await st.list(CursorPage(cursor=page.next_cursor, length=1), order_by=order)
    assert nxt is not None


@pytest.mark.asyncio
async def test_an_explicit_id_sort_key_must_carry_a_string(provider: StorageProvider) -> None:
    # order_by=[id desc] gives a cursor with TWO id keys (the sort key and the
    # tiebreaker). Both compare against the text id column, so both must carry
    # a string: an int in the first one is a bad cursor on both backends, not a
    # Postgres 5xx or a silent empty page on SQLite.
    st = provider.get_storage(LLMProvider)
    for i, pw in enumerate(["a", "b", "c"]):
        await st.create(_llm(i, pw))
    order = [OrderBy(field="id", direction="desc")]
    forged = _forge_cursor(
        [
            {"field": "id", "value": 5, "direction": "desc", "is_null": False},
            {"field": "id", "value": "llm-2", "direction": "asc", "is_null": False},
        ]
    )
    with pytest.raises(BadRequestError):
        await st.list(CursorPage(cursor=forged, length=1), order_by=order)
    # The server's own cursor for the same sort walks every row once, newest id first.
    seen: list[str] = []
    page = await st.list(CursorPage(cursor=None, length=1), order_by=order)
    for _ in range(10):
        seen.extend(r.id for r in page.items)
        if not page.next_cursor:
            break
        page = await st.list(CursorPage(cursor=page.next_cursor, length=1), order_by=order)
    assert seen == ["llm-2", "llm-1", "llm-0"]


@pytest.mark.asyncio
async def test_a_cursor_not_matching_the_requests_order_by_is_refused(provider: StorageProvider) -> None:
    st = provider.get_storage(LLMProvider)
    for i, pw in enumerate(["e", "d", "c"]):
        await st.create(_llm(i, pw))
    order = [OrderBy(field="provider", direction="asc")]
    page = await st.list(CursorPage(cursor=None, length=1), order_by=order)
    cursor = page.next_cursor
    assert cursor is not None
    # Same token, but the follow-up request sorts by nothing (id only): the
    # cursor's keys no longer match what this request would emit.
    with pytest.raises(BadRequestError):
        await st.list(CursorPage(cursor=cursor, length=1))
    # And it is accepted when the order_by matches.
    ok = await st.list(CursorPage(cursor=cursor, length=1), order_by=order)
    assert ok is not None
