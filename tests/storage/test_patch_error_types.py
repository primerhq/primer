"""``PatchSpecError`` is the caller's mistake; every OTHER ``ValueError`` under a ``patch_if`` write is a backend failure.

The backends used to re-raise ANY ``ValueError`` as itself (so that a malformed spec, a leaf the model drops and a pydantic
``ValidationError`` reach the caller unwrapped). That also re-raised a ``ValueError`` from a corrupt stored document or a
driver quirk as if the caller had made a mistake. The spec errors are now a distinct ``ValueError`` subclass, the backends
re-raise only that and ``ValidationError``, and anything else is wrapped like every other backend failure.

Each case runs on the real SQLite backend and on the Postgres backend through a scripted connection.
"""

from __future__ import annotations

import contextlib
import json

import pytest
import pytest_asyncio
from pydantic import BaseModel, Field, ValidationError

from primer.model.common import Identifiable
from primer.model.except_ import ProviderError
from primer.storage._patch import PatchSpecError, document_matches, json_equal
from primer.storage.postgres import PostgresStorage, _table_ensured
from tests.storage._patch_scenarios import StrictDoc
from tests.storage.test_patch_if_postgres_fixup import TXN_BEGIN, TXN_END, _Provider, _ScriptedConn, _row


def _postgres_storage(model_class=StrictDoc) -> PostgresStorage:
    storage = PostgresStorage(provider=_Provider(), model_class=model_class)
    _table_ensured.add((id(storage._provider), model_class))
    return storage


@pytest_asyncio.fixture
async def sqlite_storage(sqlite_provider):
    storage = sqlite_provider.get_storage(StrictDoc)
    await storage.create(StrictDoc(id="a"))
    return storage


MALFORMED = [
    dict(patch={}, where={"status": ["created"]}),                              # nothing to write
    dict(patch={"id": "b"}, where={"status": ["created"]}),                     # cannot change the id
    dict(patch={"nope": 1}, where={"status": ["created"]}),                     # a field the model does not have
    dict(patch={"count": 1}, where={}),                                         # no guard
    dict(patch={"count": 1}, where={"status": []}),                             # a guard that can never match
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", MALFORMED)
async def test_a_malformed_spec_is_a_patch_spec_error_on_both_backends(sqlite_storage, kwargs):
    for storage, conn in ((sqlite_storage, None), (_postgres_storage(), _ScriptedConn(_row()))):
        with pytest.raises(PatchSpecError) as excinfo:
            await storage.patch_if("a", **kwargs, **({} if conn is None else {"conn": conn}))
        assert isinstance(excinfo.value, ValueError), "existing handlers that catch ValueError keep working"


LONE_SURROGATE = "\ud800"   # passes json.dumps (escaped) and then cannot be UTF-8 encoded by a driver

SURROGATE_SPECS = [
    pytest.param(dict(patch={"count": 1}, where={"status": [LONE_SURROGATE]}), id="where-value"),
    pytest.param(dict(patch={"status": LONE_SURROGATE}, where={"status": ["created"]}), id="patch-value"),
    pytest.param(dict(patch={"sub": {"a": [LONE_SURROGATE]}}, where={"status": ["created"]}), id="nested-patch-value"),
    pytest.param(
        dict(patch=None, set_paths={("sub", "a"): LONE_SURROGATE}, where={"status": ["created"]}), id="set-paths-value",
    ),
    pytest.param(dict(patch={LONE_SURROGATE: 1}, where={"status": ["created"]}), id="patch-key"),
    pytest.param(
        dict(patch=None, set_paths={("sub", LONE_SURROGATE): 1}, where={"status": ["created"]}), id="set-paths-element",
    ),
    pytest.param(dict(patch={"count": 1}, where={LONE_SURROGATE: ["created"]}), id="where-field"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", SURROGATE_SPECS)
async def test_a_string_that_cannot_be_encoded_is_a_patch_spec_error_and_writes_nothing_on_both_backends(
    sqlite_storage, kwargs,
):
    """A string holding a surrogate code point used to pass the JSON check and then either fail as a backend error (differently
    per backend) or be silently stored (a patch value, on SQLite), for what is the caller's own bad
    input. It is rejected up front, the same on both, and the message names the field without echoing the value."""
    before = await sqlite_storage.get("a")
    conn = _ScriptedConn(_row())
    for storage, scripted in ((sqlite_storage, None), (_postgres_storage(), conn)):
        with pytest.raises(PatchSpecError) as excinfo:
            await storage.patch_if("a", **kwargs, **({} if scripted is None else {"conn": scripted}))
        assert "surrogate" in str(excinfo.value)
        # the raw character shows in str(), its escaped form in repr(): neither may carry the value
        for rendered in (str(excinfo.value), repr(excinfo.value)):
            assert LONE_SURROGATE not in rendered and "ud800" not in rendered.lower()
    assert conn.calls == [], "Postgres: refused before any statement"
    assert await sqlite_storage.get("a") == before, "SQLite: the row is untouched"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", SURROGATE_SPECS)
async def test_a_string_that_cannot_be_encoded_is_refused_inside_a_sqlite_transaction_which_carries_on(
    sqlite_provider, sqlite_storage, kwargs,
):
    """The same refusal when the call runs inside the caller's own transaction (where SQLite wraps the statement in a
    savepoint): it is raised before any statement, so the transaction is untouched, carries on and commits its own
    writes, and the refused value is nowhere in the row."""
    async with sqlite_provider.transaction():
        assert await sqlite_storage.patch_if("a", {"count": 1}, where={"status": ["created"]}) is not None
        with pytest.raises(PatchSpecError) as excinfo:
            await sqlite_storage.patch_if("a", **kwargs)
        assert "surrogate" in str(excinfo.value)
        assert await sqlite_storage.patch_if("a", {"gen": 1}, where={"status": ["created"]}) is not None
    row = await sqlite_storage.get("a")
    assert (row.status, row.count, row.gen) == ("created", 1, 1)


class FloatSub(BaseModel):
    x: float = 0.0


class FloatDoc(Identifiable):
    """Float fields, top-level and under a typed sub-model: lax validation reads the strings "nan" and "inf" as floats."""

    status: str = "created"
    ratio: float = 0.0
    sub: FloatSub = Field(default_factory=FloatSub)


TXN_ROLLBACK = "<transaction left by an exception>"


class _TxnConn(_ScriptedConn):
    """A scripted connection that also records a transaction block being left by an exception (a real one rolls back)."""

    def transaction(self):
        @contextlib.asynccontextmanager
        async def _txn():
            self.calls.append((TXN_BEGIN, ()))
            try:
                yield
            except BaseException:
                self.calls.append((TXN_ROLLBACK, ()))
                raise
            finally:
                self.calls.append((TXN_END, ()))

        return _txn()


@pytest.fixture
def floatdoc_event_kind(monkeypatch):
    """Give FloatDoc an event kind on the Postgres backend (as the sibling trace tests do), so an event append that slips
    ahead of the refusal would show in the statement trace."""
    monkeypatch.setattr("primer.storage.postgres.kind_for_model", lambda model: "floatdoc")


def _trace(conn: _ScriptedConn) -> list[str]:
    """The scripted connection's calls: transaction markers as they are, a statement as its first word."""
    return [sql if sql in (TXN_BEGIN, TXN_END, TXN_ROLLBACK) else sql.lstrip().split()[0] for sql, _ in conn.calls]


@pytest_asyncio.fixture
async def float_storage(sqlite_provider):
    storage = sqlite_provider.get_storage(FloatDoc)
    await storage.create(FloatDoc(id="a"))
    return storage


#: (id, the spec, the field the error must name, the document the first statement leaves behind on the scripted Postgres)
NON_FINITE_SPECS = [
    pytest.param(dict(patch={"ratio": value}), "ratio", {"ratio": value}, value, id=f"patch-{value}")
    for value in ("nan", "inf")
] + [
    pytest.param(
        dict(patch=None, set_paths={("sub", "x"): value}), "sub", {"sub": {"x": value}}, value, id=f"set-paths-leaf-{value}",
    )
    for value in ("nan", "inf")
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs, field, stored, value", NON_FINITE_SPECS)
async def test_a_value_the_model_coerces_to_a_non_finite_float_is_a_patch_spec_error_and_writes_nothing_on_both_backends(
    float_storage, floatdoc_event_kind, kwargs, field, stored, value,
):
    """The strings "nan" and "inf" pass the spec check; the model then reads them as non-finite floats, and the
    canonical rewrite used to hand NaN to the JSON encoder, which failed as a ProviderError (SQLite) or a different backend
    error (Postgres) for what is the caller's own bad value. Now the rewrite refuses it as the spec error it is, naming the
    field and never echoing the value, and the write rolls back."""
    before = await float_storage.get("a")
    conn = _TxnConn(_row(**stored))
    for storage, scripted in ((float_storage, None), (_postgres_storage(FloatDoc), conn)):
        with pytest.raises(PatchSpecError) as excinfo:
            await storage.patch_if("a", where={"status": ["created"]}, **kwargs, **({} if scripted is None else {"conn": scripted}))
        message = str(excinfo.value)
        assert f"FloatDoc.{field} " in message and value not in message.lower()
    assert await float_storage.get("a") == before, "SQLite: the row is untouched"
    # Postgres: the refusal can only come after the guarded statement (the model has to validate its result), so it is
    # one UPDATE inside the transaction, no rewrite and no event (the model has a registered kind, so an INSERT would show),
    # and the transaction is left by the exception: a rollback.
    assert _trace(conn) == [TXN_BEGIN, "UPDATE", TXN_ROLLBACK, TXN_END]


@pytest.mark.asyncio
async def test_a_legal_patch_on_the_same_model_and_kind_appends_its_event(floatdoc_event_kind):
    """The control for the refusal trace: the kind is registered, so a patch the model can store ends in its one UPDATE and
    the event INSERT, in one transaction, and nothing is rolled back."""
    conn = _TxnConn(_row(ratio=0.5))
    out = await _postgres_storage(FloatDoc).patch_if("a", {"ratio": 0.5}, where={"status": ["created"]}, conn=conn)
    assert out is not None and out.ratio == 0.5
    assert _trace(conn) == [TXN_BEGIN, "UPDATE", "INSERT", TXN_END]


@pytest.mark.asyncio
async def test_a_value_error_that_is_not_a_spec_error_is_wrapped_on_sqlite(sqlite_storage, monkeypatch):
    """A corrupt stored document, say: ``_from_row`` raises a plain ValueError after the UPDATE returned a row."""
    def corrupt(*_a, **_k):
        raise ValueError("stored document is not valid JSON")

    monkeypatch.setattr(sqlite_storage, "_from_row", corrupt)
    with pytest.raises(ProviderError) as excinfo:
        await sqlite_storage.patch_if("a", {"count": 1}, where={"status": ["created"]})
    assert not isinstance(excinfo.value, ValueError) and isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_a_value_error_that_is_not_a_spec_error_is_wrapped_on_postgres(monkeypatch):
    storage = _postgres_storage()

    def corrupt(*_a, **_k):
        raise ValueError("stored document is not valid JSON")

    monkeypatch.setattr(storage, "_from_row", corrupt)
    with pytest.raises(ProviderError) as excinfo:
        await storage.patch_if("a", {"count": 1}, where={"status": ["created"]}, conn=_ScriptedConn(_row()))
    assert not isinstance(excinfo.value, ValueError) and isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_a_validation_error_is_still_reported_as_itself_on_both_backends(sqlite_storage):
    """A patch that leaves the row unreadable is the caller's mistake too (pydantic's ValidationError, a ValueError
    subclass that is NOT a PatchSpecError), and it is re-raised unwrapped."""
    with pytest.raises(ValidationError):
        await sqlite_storage.patch_if("a", {"count": "not a number"}, where={"status": ["created"]})
    conn = _ScriptedConn(_row(count="not a number"))
    with pytest.raises(ValidationError):
        await _postgres_storage().patch_if(
            "a", {"count": "not a number"}, where={"status": ["created"]}, conn=conn,
        )


def test_the_python_guard_check_uses_the_same_equality_as_where():
    """``document_matches`` (the drift tripwire's check) and ``where`` share ONE typed-equality helper, ``json_equal``: a
    private scalar-only copy of it was deleted."""
    assert document_matches({"n": 5}, {"n": [5.0]}) and json_equal(5, 5.0)
    assert not document_matches({"flag": True}, {"flag": [1]}) and not json_equal(True, 1)
    assert not document_matches({"a": None}, {"a": [0]})
    import primer.storage._patch as patch_module

    assert not hasattr(patch_module, "_typed_equal")
