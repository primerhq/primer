"""A stored row that no longer decodes into the model: classified, and never naming the stored values.

Ticket 01a1261f-07e0: a stored row whose shape no longer validates (stored-shape drift, or a hand edit)
raised a raw pydantic ``ValidationError`` out of the storage read paths -- escaping every error arm
(a bare 500 with the generic detail) and naming the stored values (pydantic's text carries the input,
and the input is the stored document: a Base URL with its password, a config with its key; the same
leak the m008 migration's docstring records for the embedding-provider rows).

The backends now classify the decode failure as ``primer.model.except_.RowDecodeError`` (a
``PrimerError``: 500 ``/errors/internal`` through the base row). The message names the model and the
row id and NEVER the stored values; the decode failure is kept on ``cause`` for the server-side
traceback only. ``patch_if`` stays exempt: its unreadable document is the caller's own patch, reported
as the raw ``ValidationError``, as itself (pinned by the patch error-type tests).
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError as PydanticValidationError

from primer.api.errors import register_error_handlers
from primer.model.common import Identifiable
from primer.model.except_ import PrimerError, RowDecodeError
from primer.model.storage import CursorPage, OffsetPage
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider
from tests.pg_gate import needs_postgres

_SECRET = "s3cret-stored-value-07e0"

pg = needs_postgres("the row-decode-error Postgres tests")


class _Drift(Identifiable):
    """A minimal model to drift: ``title`` is required, ``secret`` carries the value that used to leak."""

    title: str
    secret: SecretStr | None = None


async def _drift_sqlite_row(provider: SqliteStorageProvider, storage, id_: str, data: str) -> None:
    """Overwrite the row's data column with ``data``, bypassing validation."""
    table = storage._table  # noqa: SLF001
    await provider.connection.execute(f'UPDATE "{table}" SET data = ? WHERE id = ?', (data, id_))
    # Commit: a raw UPDATE on the shared connection otherwise leaves an implicit transaction open,
    # and the next read that takes its own snapshot (list) cannot BEGIN inside it.
    await provider.connection.commit()


async def _drift_postgres_row(provider: PostgresStorageProvider, storage, id_: str, data: str) -> None:
    qualified = storage._qualified  # noqa: SLF001
    async with provider.pool.acquire() as conn:
        await conn.execute(f"UPDATE {qualified} SET data = $1::jsonb WHERE id = $2", data, id_)


async def _fresh_drift_table(provider: PostgresStorageProvider, storage) -> None:
    """The model table persists in the throwaway schema between runs: start each test from an empty one."""
    await storage._ensure_table()  # noqa: SLF001
    qualified = storage._qualified  # noqa: SLF001
    async with provider.pool.acquire() as conn:
        await conn.execute(f"DELETE FROM {qualified}")


def _assert_redacted(err: RowDecodeError, *, row_id: str) -> None:
    """The decode-failure contract: classified, names the model and the row id, never the stored value."""
    assert isinstance(err, PrimerError)
    assert _Drift.__name__ in str(err)
    assert row_id in str(err)
    assert _SECRET not in str(err)


# ===========================================================================
# SQLite: the read paths
# ===========================================================================


@pytest.mark.asyncio
async def test_get_a_row_the_model_no_longer_validates_raises_rowdecode_error(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Drift)
    await s.create(_Drift(id="d1", title="ok", secret=SecretStr(_SECRET)))
    # Drift: drop the required ``title`` and keep the secret (the value that used to leak into the 500).
    await _drift_sqlite_row(sqlite_provider, s, "d1", json.dumps({"secret": _SECRET}))
    with pytest.raises(RowDecodeError) as excinfo:
        await s.get("d1")
    _assert_redacted(excinfo.value, row_id="d1")
    # The raw pydantic failure is kept for the server-side traceback only, never in the message.
    assert isinstance(excinfo.value.cause, PydanticValidationError)
    assert _SECRET in str(excinfo.value.cause)  # the cause still carries it; the message must not


@pytest.mark.asyncio
async def test_get_a_row_holding_json_that_is_not_an_object_raises_rowdecode_error(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Drift)
    await s.create(_Drift(id="d2", title="ok"))
    await _drift_sqlite_row(sqlite_provider, s, "d2", json.dumps([1, 2, 3]))
    with pytest.raises(RowDecodeError) as excinfo:
        await s.get("d2")
    _assert_redacted(excinfo.value, row_id="d2")
    assert isinstance(excinfo.value.cause, TypeError)


@pytest.mark.asyncio
async def test_get_a_row_holding_invalid_json_raises_rowdecode_error(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Drift)
    await s.create(_Drift(id="d3", title="ok"))
    await _drift_sqlite_row(sqlite_provider, s, "d3", "{not json")
    with pytest.raises(RowDecodeError) as excinfo:
        await s.get("d3")
    _assert_redacted(excinfo.value, row_id="d3")
    assert isinstance(excinfo.value.cause, json.JSONDecodeError)


@pytest.mark.asyncio
async def test_list_a_page_holding_an_undecodable_row_raises_rowdecode_error(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Drift)
    await s.create(_Drift(id="l1", title="a"))
    await s.create(_Drift(id="l2", title="b", secret=SecretStr(_SECRET)))
    await _drift_sqlite_row(sqlite_provider, s, "l2", json.dumps({"secret": _SECRET}))
    # One unreadable row fails the whole page (the backend decodes the page before it returns it),
    # classified and redacted rather than as a raw ValidationError that names the stored value.
    with pytest.raises(RowDecodeError) as excinfo:
        await s.list(OffsetPage(offset=0, length=10))
    _assert_redacted(excinfo.value, row_id="l2")


@pytest.mark.asyncio
async def test_find_a_page_holding_an_undecodable_row_raises_rowdecode_error(sqlite_provider: SqliteStorageProvider):
    s = sqlite_provider.get_storage(_Drift)
    await s.create(_Drift(id="f1", title="a"))
    await s.create(_Drift(id="f2", title="b", secret=SecretStr(_SECRET)))
    await _drift_sqlite_row(sqlite_provider, s, "f2", json.dumps({"secret": _SECRET}))
    with pytest.raises(RowDecodeError) as excinfo:
        await s.find(None, CursorPage(cursor=None, length=10))
    _assert_redacted(excinfo.value, row_id="f2")


# ===========================================================================
# Postgres: the same contract on the JSONB backend (gated)
# ===========================================================================


# The throwaway Postgres schema keeps model tables between runs (the fixture only wipes the lease
# tables), so the Postgres tests write under ids unique to this run rather than cleaning up.
_RUN = uuid.uuid4().hex[:8]


@pg
@pytest.mark.asyncio
async def test_postgres_get_a_row_the_model_no_longer_validates_raises_rowdecode_error(postgres_storage_provider: PostgresStorageProvider):
    s = postgres_storage_provider.get_storage(_Drift)
    await _fresh_drift_table(postgres_storage_provider, s)
    row_id = f"d1-{_RUN}"
    await s.create(_Drift(id=row_id, title="ok", secret=SecretStr(_SECRET)))
    await _drift_postgres_row(postgres_storage_provider, s, row_id, json.dumps({"secret": _SECRET}))
    with pytest.raises(RowDecodeError) as excinfo:
        await s.get(row_id)
    _assert_redacted(excinfo.value, row_id=row_id)
    assert isinstance(excinfo.value.cause, PydanticValidationError)


@pg
@pytest.mark.asyncio
async def test_postgres_list_a_page_holding_an_undecodable_row_raises_rowdecode_error(postgres_storage_provider: PostgresStorageProvider):
    s = postgres_storage_provider.get_storage(_Drift)
    await _fresh_drift_table(postgres_storage_provider, s)
    good_id = f"l1-{_RUN}"
    bad_id = f"l2-{_RUN}"
    await s.create(_Drift(id=good_id, title="a"))
    await s.create(_Drift(id=bad_id, title="b", secret=SecretStr(_SECRET)))
    await _drift_postgres_row(postgres_storage_provider, s, bad_id, json.dumps({"secret": _SECRET}))
    with pytest.raises(RowDecodeError) as excinfo:
        await s.list(OffsetPage(offset=0, length=10))
    _assert_redacted(excinfo.value, row_id=bad_id)


# ===========================================================================
# The API surface: a classified 500 with the stored value redacted from the envelope
# ===========================================================================


@pytest.mark.asyncio
async def test_api_answers_a_500_internal_with_the_stored_value_redacted(sqlite_provider: SqliteStorageProvider):
    """The decode failure reaches the REST surface as a problem+json 500: the classified detail
    (model + row id) replaces the raw pydantic error that used to escape every error arm."""
    s = sqlite_provider.get_storage(_Drift)
    await s.create(_Drift(id="d1", title="ok", secret=SecretStr(_SECRET)))
    await _drift_sqlite_row(sqlite_provider, s, "d1", json.dumps({"secret": _SECRET}))

    app = FastAPI()
    register_error_handlers(app)

    @app.get("/row")
    async def read_row():
        return await s.get("d1")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/row")
    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["type"] == "/errors/internal"
    assert body["title"] == "Internal Error"
    assert _Drift.__name__ in body["detail"]
    assert "d1" in body["detail"]
    # The stored value is nowhere in the envelope.
    assert _SECRET not in resp.text
