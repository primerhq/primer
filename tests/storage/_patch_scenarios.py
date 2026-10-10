"""Behavioural scenarios for ``Storage.patch_if``, run against every implementation.

``tests/storage/test_storage_contract.py`` runs each against SQLite and (gated) Postgres;
``tests/storage/test_patch_if_fake.py`` runs the same ones against the in-memory fake in
``tests/conftest.py``: a fake that diverges from the backends fails a scenario here instead of quietly
misleading every test that uses it (the fake keeps the RAW stored document, as the backends do).
Each scenario creates its own rows and takes only a ``Storage[PatchDoc]``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any

import pytest
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, ValidationError

from primer.int.storage import Storage
from primer.model.common import Identifiable
from primer.model.except_ import NotFoundError
from primer.storage import raw_generation
from primer.storage._patch import PatchSpecError


class Phase(str, Enum):
    ALPHA = "alpha"
    BETA = "beta"


class PatchDoc(Identifiable):
    """A row shaped like the ones patch_if is for: top-level flags plus a nested JSON blob."""

    model_config = ConfigDict(extra="allow")  # so a created-by-patch key is visible on read

    status: str = "created"
    flag: bool = False
    count: int = 0
    token: str | None = None
    stamp: datetime | None = None
    state: Any = None  # may hold {}, a nested object, null or (for the parent tests) a scalar
    ratio: float = 0.0
    phase: Phase = Phase.ALPHA
    secret: SecretStr | None = None


class Sub(BaseModel):
    """A typed sub-model field: unknown nested keys are ignored on read, so the canonical dump drops them."""

    a: int = 0
    when: datetime | None = None


class StrictDoc(Identifiable):
    """The usual shape: unknown stored keys are IGNORED on read, and a validator normalises `tag`."""

    status: str = "created"
    count: int = 0
    gen: int = 0  # a defaulted generation field: documents older than it do not have the key
    flag: bool = False
    stamp: datetime | None = None
    tag: Annotated[str, AfterValidator(str.lower)] = ""
    timeout: int | None = 30  # nullable with a non-null default: a stored null and an absent key differ
    sub: Sub = Field(default_factory=Sub)


Store = Storage[PatchDoc]


async def _mk(store: Store, id_: str, **fields: Any) -> PatchDoc:
    return await store.create(PatchDoc(id=id_, **fields))


# ---- result contract -----------------------------------------------------------------------------


async def applies_when_the_guard_matches_and_leaves_other_fields(store: Store) -> None:
    await _mk(store, "a", status="running", count=3, token="t1", flag=True)
    out = await store.patch_if("a", {"status": "done"}, where={"status": ["running"]})
    assert out is not None and out.status == "done"
    fresh = await store.get("a")
    assert fresh is not None
    assert (fresh.status, fresh.count, fresh.token, fresh.flag) == ("done", 3, "t1", True)


async def a_rejected_guard_returns_none_and_changes_nothing(store: Store) -> None:
    await _mk(store, "a", status="running", count=3)
    out = await store.patch_if("a", {"status": "done", "count": 9}, where={"status": ["queued"]})
    assert out is None
    fresh = await store.get("a")
    assert fresh is not None and (fresh.status, fresh.count) == ("running", 3)


async def a_missing_row_raises_not_found_not_none(store: Store) -> None:
    with pytest.raises(NotFoundError):
        await store.patch_if("nope", {"status": "x"}, where={"status": ["running"]})


async def a_patch_does_not_overwrite_a_field_written_since_the_read(store: Store) -> None:
    """The point of the primitive: the writer owns `status`, a concurrent writer owns `token`."""
    await _mk(store, "a", status="running", token=None)
    snapshot = await store.get("a")
    assert snapshot is not None
    await store.patch_if("a", {"token": "set-by-someone-else"}, where={"status": ["running"]})
    out = await store.patch_if("a", {"status": "done"}, where={"status": [snapshot.status]})
    assert out is not None and out.token == "set-by-someone-else"


# ---- where: typed compare ------------------------------------------------------------------------


async def where_compares_typed_scalars_not_text(store: Store) -> None:
    await _mk(store, "a", flag=True, count=1, status="1")
    assert await store.patch_if("a", {"token": "x"}, where={"flag": ["true"]}) is None   # str, not bool
    assert await store.patch_if("a", {"token": "x"}, where={"count": ["1"]}) is None     # str, not int
    assert await store.patch_if("a", {"token": "x"}, where={"status": [1]}) is None      # int, not str
    assert await store.patch_if("a", {"token": "x"}, where={"flag": [1]}) is None        # int, not bool
    assert await store.patch_if("a", {"token": "y"}, where={"flag": [True], "count": [1]}) is not None


async def where_matches_any_listed_value_and_all_fields(store: Store) -> None:
    await _mk(store, "a", status="queued", count=2)
    assert await store.patch_if("a", {"token": "1"}, where={"status": ["running", "queued"]}) is not None
    assert await store.patch_if("a", {"token": "2"}, where={"status": ["queued"], "count": [3]}) is None
    assert await store.patch_if("a", {"token": "3"}, where={"status": ["queued"], "count": [2, 3]}) is not None


async def none_in_where_matches_null_and_absent(store: Store) -> None:
    await _mk(store, "a")                               # token is null
    assert await store.patch_if("a", {"count": 1}, where={"token": [None]}) is not None
    assert await store.patch_if("a", {"count": 2}, where={"token": ["x", None]}) is not None
    assert await store.patch_if("a", {"token": "set"}, where={"token": [None]}) is not None
    assert await store.patch_if("a", {"count": 3}, where={"token": [None]}) is None  # now "set"
    # a field the document never had reads as absent
    assert await store.patch_if("a", {"count": 4}, where={"never_written": [None]}) is not None
    assert await store.patch_if("a", {"count": 5}, where={"never_written": ["x"]}) is None


# ---- where: a path to a nested leaf ----------------------------------------------------------------------------------------------------------


async def a_where_path_compares_the_nested_leaf(store: Store) -> None:
    """A ``where`` key may be a path (a tuple, like a ``set_paths`` key): the leaf it names is compared by the same typed rule as a top-level field,
    and ``None`` matches a leaf that is absent, under an absent, scalar or array parent too (a path never indexes into an array), or JSON null."""
    await _mk(store, "a", state={"ps": {"k1": "x", "n": 2, "nul": None}, "scalar": 5, "arr": [1, 2]})
    ok = {("state", "ps", "w"): 1}
    assert await store.patch_if("a", None, where={("state", "ps", "k1"): ["x"]}, set_paths=ok) is not None
    assert await store.patch_if("a", None, where={("state", "ps", "k1"): ["y"]}, set_paths=ok) is None
    assert await store.patch_if("a", None, where={("state", "ps", "n"): [2.0]}, set_paths=ok) is not None   # numbers by value
    assert await store.patch_if("a", None, where={("state", "ps", "n"): ["2"]}, set_paths=ok) is None      # typed, not text
    assert await store.patch_if("a", None, where={("state", "ps", "k1"): [None]}, set_paths=ok) is None   # set: not absent
    for absent in (("state", "ps", "nul"), ("state", "ps", "missing"), ("state", "none", "x"), ("state", "scalar", "x"), ("state", "arr", "0")):
        assert await store.patch_if("a", None, where={absent: [None]}, set_paths=ok) is not None, absent
    # a path and a field guard combine like two fields: both must match
    assert await store.patch_if("a", {"count": 1}, where={"status": ["created"], ("state", "ps", "missing"): [None]}) is not None
    assert await store.patch_if("a", {"count": 2}, where={"status": ["other"], ("state", "ps", "missing"): [None]}) is None
    fresh = await store.get("a")
    assert fresh is not None and fresh.count == 1 and fresh.state["ps"] == {"k1": "x", "n": 2, "nul": None, "w": 1}


async def a_leaf_guarded_absent_is_written_once(store: Store) -> None:
    """The use it exists for: two writers that each set a leaf only while it is absent. The first lands; the second, on the same guard, is refused
    and leaves the first one's leaf and fields as they are (ticket 01a12606: one decision per gate)."""
    await _mk(store, "a", state={})
    leaf = ("state", "ps", "k")
    first = await store.patch_if("a", {"token": "one"}, where={leaf: [None]}, set_paths={leaf: {"v": 1}})
    second = await store.patch_if("a", {"token": "two"}, where={leaf: [None]}, set_paths={leaf: {"v": 2}})
    fresh = await store.get("a")
    assert (first is not None, second, fresh.token if fresh else None, fresh.state if fresh else None) == (True, None, "one", {"ps": {"k": {"v": 1}}})


async def a_malformed_where_path_is_rejected(store: Store) -> None:
    await _mk(store, "a")
    for bad in (
        {(): [None]},                                  # an empty path
        {("state", 'a"b'): [None]},                    # a quote in an element
        {("state", "a\\b"): [None]},                   # a backslash
        {("state", ""): [None]},                       # an empty element
        {("state", 1): [None]},                        # an element that is not a string
        {("a", "b", "c", "d", "e"): [None]},           # deeper than the cap
        {("id", "x"): [None]},                         # rooted at the id
        {("state", "x"): "absent"},                    # a bare string, not a list
        {("state", "x"): [{"v": 1}]},                  # not a scalar
    ):
        with pytest.raises(PatchSpecError):
            await store.patch_if("a", {"count": 1}, where=bad)  # type: ignore[arg-type]
    fresh = await store.get("a")
    assert fresh is not None and fresh.count == 0


# ---- raw_generation: a read value matches the stored value ---------------------------------------


async def raw_generation_round_trips_for_microseconds_and_a_utc_offset(store: Store) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    cases = {
        "micro": datetime(2026, 10, 4, 12, 30, 45, 123456, tzinfo=timezone.utc),
        "offset": datetime(2026, 10, 4, 18, 0, 0, 7, tzinfo=ist),
    }
    for id_, stamp in cases.items():
        await _mk(store, id_, stamp=stamp, token=f"tok-{id_}")
        row = await store.get(id_)
        assert row is not None
        gen = raw_generation(row, "stamp")
        assert isinstance(gen, str), "raw_generation must be the stored JSON value, not a datetime"
        out = await store.patch_if(
            id_, {"count": 1}, where={"stamp": [gen], "token": [raw_generation(row, "token")]},
        )
        assert out is not None, f"read -> raw_generation -> patch_if did not match for {id_}"
        # the formatting a naive caller would write does NOT match, which is why raw_generation exists
        assert await store.patch_if(id_, {"count": 2}, where={"stamp": [stamp.replace(tzinfo=None).isoformat()]}) is None


# ---- set_paths -----------------------------------------------------------------------------------


async def three_paths_sharing_a_parent_all_land_and_siblings_survive(store: Store) -> None:
    await _mk(store, "a", state={"keep": 1, "ps": {"existing": True}})
    out = await store.patch_if(
        "a", None, where={"status": ["created"]},
        set_paths={
            ("state", "ps", "k1"): 1,
            ("state", "ps", "k2"): {"nested": [1, 2]},
            ("state", "ps", "k3"): "three",
        },
    )
    assert out is not None
    assert out.state == {
        "keep": 1,
        "ps": {"existing": True, "k1": 1, "k2": {"nested": [1, 2]}, "k3": "three"},
    }


async def a_null_parent_is_replaced_and_the_leaves_set(store: Store) -> None:
    await _mk(store, "a", state=None)
    out = await store.patch_if(
        "a", None, where={"status": ["created"]},
        set_paths={("state", "ps", "k1"): 1, ("state", "ps", "k2"): 2},
    )
    assert out is not None and out.state == {"ps": {"k1": 1, "k2": 2}}


async def a_scalar_parent_is_replaced_and_the_leaf_set(store: Store) -> None:
    await _mk(store, "a", state=5)
    out = await store.patch_if(
        "a", None, where={"status": ["created"]}, set_paths={("state", "ps", "k"): 1},
    )
    assert out is not None and out.state == {"ps": {"k": 1}}


async def a_missing_parent_is_created(store: Store) -> None:
    await _mk(store, "a")
    out = await store.patch_if(
        "a", None, where={"status": ["created"]},
        set_paths={("brand_new", "ps", "k1"): 1, ("brand_new", "ps", "k2"): 2},
    )
    assert out is not None
    assert (out.model_extra or {}).get("brand_new") == {"ps": {"k1": 1, "k2": 2}}


async def keys_with_dots_and_colons_are_one_key_each(store: Store) -> None:
    await _mk(store, "a", state={})
    key = "tool_wait:sess/1.x:y"
    out = await store.patch_if(
        "a", None, where={"status": ["created"]},
        set_paths={("state", "payloads", key): {"payload": 1}},
    )
    assert out is not None and out.state == {"payloads": {key: {"payload": 1}}}


async def patch_and_set_paths_apply_together(store: Store) -> None:
    await _mk(store, "a", state={"x": 1})
    out = await store.patch_if(
        "a", {"status": "running", "token": "t"}, where={"status": ["created"]},
        set_paths={("state", "y"): 2},
    )
    assert out is not None
    assert (out.status, out.token, out.state) == ("running", "t", {"x": 1, "y": 2})


async def a_rejected_set_paths_write_changes_nothing(store: Store) -> None:
    await _mk(store, "a", state={"x": 1})
    assert await store.patch_if(
        "a", None, where={"status": ["running"]}, set_paths={("state", "y"): 2},
    ) is None
    fresh = await store.get("a")
    assert fresh is not None and fresh.state == {"x": 1}


# ---- argument validation (identical on every backend, before any SQL) ----------------------------


async def malformed_specs_are_rejected_with_value_error(store: Store) -> None:
    await _mk(store, "a", state={})
    ok = {"status": ["created"]}
    bad: list[dict[str, Any]] = [
        {"patch": None, "set_paths": None},                                       # nothing to write
        {"patch": {"id": "z"}},                                                    # cannot change id
        {"patch": {'we"ird': 1}},                                                  # quote in a key
        {"patch": None, "set_paths": {("state", 'a"b'): 1}},                       # quote in a path element
        {"patch": None, "set_paths": {("state", "a\\b"): 1}},                      # backslash
        {"patch": None, "set_paths": {("state", "a\nb"): 1}},                      # control character
        {"patch": None, "set_paths": {("a", "b", "c", "d", "e"): 1}},              # deeper than the cap
        {"patch": None, "set_paths": {(): 1}},                                     # empty path
        {"patch": None, "set_paths": {("state",): 1, ("state", "x"): 2}},          # prefix conflict
        {"patch": {"state": {}}, "set_paths": {("state", "x"): 1}},                # same field both ways
        {"patch": {"count": float("nan")}},                                        # not JSON-ready
        {"patch": {"count": 1}, "where": {"status": []}},                          # can never match
        {"patch": {"count": 1}, "where": {"status": [{"a": 1}]}},                  # not a scalar
    ]
    for case in bad:
        with pytest.raises(PatchSpecError):
            await store.patch_if(
                "a", case.get("patch"), where=case.get("where", ok),
                set_paths=case.get("set_paths"),
            )
    fresh = await store.get("a")
    assert fresh is not None and fresh.state == {} and fresh.count == 0


# ---- numbers, enums, secrets -------------------------------------------------------------------


async def numbers_compare_by_value_across_int_and_float(store: Store) -> None:
    """One numeric rule on every backend: 1 equals 1.0, but a number is never a string or a bool."""
    await _mk(store, "a", ratio=3.0, count=1)
    assert await store.patch_if("a", {"token": "1"}, where={"ratio": [3]}) is not None        # float 3.0 vs int 3
    assert await store.patch_if("a", {"token": "2"}, where={"count": [1.0]}) is not None      # int 1 vs float 1.0
    assert await store.patch_if("a", {"token": "3"}, where={"count": [True]}) is None         # not a bool
    assert await store.patch_if("a", {"token": "4"}, where={"ratio": ["3.0"]}) is None        # not a string
    assert await store.patch_if("a", {"token": "5"}, where={"ratio": [3.5]}) is None


async def raw_generation_round_trips_for_float_enum_and_secret(store: Store) -> None:
    await _mk(store, "a", ratio=2.5, phase=Phase.BETA, secret=SecretStr("hunter2"), token="t")
    row = await store.get("a")
    assert row is not None
    where = {f: [raw_generation(row, f)] for f in ("ratio", "phase", "secret", "token")}
    assert where["phase"] == ["beta"] and where["secret"] == ["hunter2"], "the stored form, unmasked"
    assert await store.patch_if("a", {"count": 1}, where=where) is not None


# ---- the document the write produces must still be a valid model ----------------------------------


async def a_patch_that_leaves_the_row_unreadable_is_rejected_and_rolled_back(store: Store) -> None:
    await _mk(store, "a", count=2)
    with pytest.raises(ValidationError):
        await store.patch_if("a", {"count": "not-a-number"}, where={"status": ["created"]})
    fresh = await store.get("a")                       # still readable, still the old value
    assert fresh is not None and fresh.count == 2


async def a_value_the_model_coerces_to_a_non_finite_float_is_refused_and_the_row_is_unchanged(store: Store) -> None:
    """The strings "nan" and "inf" are fine JSON and a non-finite float once the model reads them, which no backend can store: the call
    is a PatchSpecError that names the field and never echoes the value, and a field patched in the same call is not written."""
    await _mk(store, "a", ratio=1.5, count=2)
    for value in ("nan", "inf"):
        with pytest.raises(PatchSpecError) as excinfo:
            await store.patch_if("a", {"ratio": value, "count": 9}, where={"status": ["created"]})
        message = str(excinfo.value)
        assert "PatchDoc.ratio" in message and value not in message.lower()
        fresh = await store.get("a")
        assert fresh is not None and (fresh.ratio, fresh.count) == (1.5, 2), f"{value!r}: the refused call wrote something"


async def an_empty_or_malformed_where_is_rejected(store: Store) -> None:
    await _mk(store, "a")
    for bad_where in ({}, {"status": "running"}, {"status": b"running"}, {"id": ["a"]}):
        with pytest.raises(PatchSpecError):
            await store.patch_if("a", {"count": 1}, where=bad_where)  # type: ignore[arg-type]
    # more distinct parent objects than the cap, and more leaves than the cap
    many_parents = {(f"p{i}", "k"): 1 for i in range(6)}
    with pytest.raises(PatchSpecError):
        await store.patch_if("a", None, where={"status": ["created"]}, set_paths=many_parents)
    many_leaves = {("state", f"k{i}"): i for i in range(40)}
    with pytest.raises(PatchSpecError):
        await store.patch_if("a", None, where={"status": ["created"]}, set_paths=many_leaves)


async def an_array_parent_is_replaced_like_any_non_object(store: Store) -> None:
    await _mk(store, "a", state=[1, 2, 3])
    out = await store.patch_if(
        "a", None, where={"status": ["created"]}, set_paths={("state", "k"): 1},
    )
    assert out is not None and out.state == {"k": 1}


# ---- canonical storage: stored == what a read re-dumps -------------------------------------------
#
# These take an ``Env`` (a storage factory plus a way to plant a RAW stored document), because the cases are
# about what the database holds, which no write through the model can produce.


class Env:
    """What a raw scenario needs from an implementation."""

    def store(self, model: type[Identifiable]) -> Storage[Any]:  # pragma: no cover - interface
        raise NotImplementedError

    async def seed_raw(self, model: type[Identifiable], id_: str, doc: dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError


class ProviderEnv(Env):
    """SQLite or Postgres: the raw document is inserted with SQL, bypassing the model."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def store(self, model):
        return self._provider.get_storage(model)

    async def seed_raw(self, model, id_, doc):
        store = self.store(model)
        await store._ensure_table()
        if hasattr(self._provider, "pool"):          # Postgres
            async with self._provider.pool.acquire() as conn:
                await conn.execute(
                    f"INSERT INTO {store._qualified} (id, data) VALUES ($1, $2::jsonb)", id_, json.dumps(doc),
                )
        else:                                         # SQLite
            conn = self._provider.connection
            await conn.execute(f'INSERT INTO "{store._table}" (id, data) VALUES (?, ?)', (id_, json.dumps(doc)))
            await conn.commit()


class FakeEnv(Env):
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def store(self, model):
        return self._provider.get_storage(model)

    async def seed_raw(self, model, id_, doc):
        self.store(model).seed_raw(id_, doc)


async def a_loosely_typed_patch_is_stored_canonical_so_a_guard_built_from_a_read_matches(env: Env) -> None:
    """The write path stores what the caller wrote; a read re-dumps the validated model. Without the
    canonicalising rewrite, patch {count: "5"} stored a string and ``raw_generation`` (5) never matched it."""
    store = env.store(StrictDoc)
    await store.create(StrictDoc(id="a", count=1))
    out = await store.patch_if(
        "a", {"count": "5", "flag": 1, "stamp": "2026-10-04T12:30:45+00:00", "tag": "MiXeD"},
        where={"status": ["created"]},
    )
    assert out is not None and (out.count, out.flag, out.tag) == (5, True, "mixed")
    row = await store.get("a")
    assert row is not None
    guard = {f: [raw_generation(row, f)] for f in ("count", "flag", "stamp", "tag")}
    assert guard["count"] == [5] and guard["flag"] == [True] and guard["tag"] == ["mixed"]
    assert guard["stamp"][0].endswith("Z")
    assert await store.patch_if("a", {"status": "x"}, where=guard) is not None, "the read-back guard must match"
    # only the canonical spelling is stored: the loose ones never match
    assert await store.patch_if("a", {"status": "y"}, where={"count": ["5"]}) is None
    assert await store.patch_if("a", {"status": "y"}, where={"flag": [1]}) is None, "a bool is not a number"
    assert await store.patch_if("a", {"status": "y"}, where={"tag": ["MiXeD"]}) is None
    assert await store.patch_if("a", {"status": "y"}, where={"stamp": ["2026-10-04T12:30:45+00:00"]}) is None


async def a_patch_that_is_already_canonical_is_not_rewritten_or_changed(env: Env) -> None:
    store = env.store(StrictDoc)
    await store.create(StrictDoc(id="a", count=2, tag="ok"))
    out = await store.patch_if("a", {"count": 3, "tag": "fine"}, where={"count": [2]})
    assert out is not None and (out.count, out.tag, out.status) == (3, "fine", "created")
    assert await store.patch_if("a", {"status": "x"}, where={"count": [3], "tag": ["fine"]}) is not None


async def a_document_missing_a_defaulted_field_reads_as_the_default_in_a_guard(env: Env) -> None:
    """A row older than the field has no key; the model reads the default and raw_generation returns it, so a
    guard built from that read must match the document that lacks the key."""
    store = env.store(StrictDoc)
    await env.seed_raw(StrictDoc, "old", {"status": "created"})
    row = await store.get("old")
    assert row is not None and (row.gen, row.count) == (0, 0)
    guard = {"gen": [raw_generation(row, "gen")], "count": [raw_generation(row, "count")]}
    assert guard == {"gen": [0], "count": [0]}
    assert await store.patch_if("old", {"status": "x"}, where=guard) is not None
    assert await store.patch_if("old", {"status": "y"}, where={"gen": [1]}) is None
    assert await store.patch_if("old", {"status": "z"}, where={"gen": [None]}) is not None, "absent still reads as None"
    # once written, the key exists and is compared like any other
    assert await store.patch_if("old", {"gen": 1}, where={"gen": [0]}) is not None
    assert await store.patch_if("old", {"gen": 2}, where={"gen": [0]}) is None
    assert await store.patch_if("old", {"gen": 2}, where={"gen": [1]}) is not None


async def a_key_the_model_ignores_survives_a_patch_and_is_dropped_by_a_whole_document_update(env: Env) -> None:
    store = env.store(StrictDoc)
    await env.seed_raw(StrictDoc, "x", {"status": "created", "legacy": "v"})
    assert await store.patch_if("x", {"count": 1}, where={"status": ["created"]}) is not None
    assert await store.patch_if("x", {"count": 2}, where={"legacy": ["v"]}) is not None, "the patch kept the key"
    row = await store.get("x")
    assert row is not None
    await store.update(row)
    assert await store.patch_if("x", {"count": 3}, where={"legacy": ["v"]}) is None, "update re-dumps the model"


async def patching_a_field_the_model_does_not_have_is_rejected(env: Env) -> None:
    store = env.store(StrictDoc)
    await store.create(StrictDoc(id="a"))
    with pytest.raises(PatchSpecError):
        await store.patch_if("a", {"cnt": 1}, where={"status": ["created"]})
    with pytest.raises(PatchSpecError):
        await store.patch_if("a", None, where={"status": ["created"]}, set_paths={("cnt", "k"): 1})
    row = await store.get("a")
    assert row is not None and row.count == 0
    # the field names are checked before the row is looked up, so a missing row does not mask the mistake
    with pytest.raises(PatchSpecError):
        await store.patch_if("nope", {"cnt": 1}, where={"status": ["created"]})


async def a_stale_guard_on_a_nullable_field_with_a_default_is_not_applied_over_a_newer_null(env: Env) -> None:
    """A field that can hold null reads an absent key as its default, but a guard naming the default must NOT also
    match a stored JSON null: a reader saw 30, a writer set it to null, and the stale guard would apply over it."""
    store = env.store(StrictDoc)
    await store.create(StrictDoc(id="a"))
    row = await store.get("a")
    assert row is not None and row.timeout == 30
    stale = {"timeout": [raw_generation(row, "timeout")]}
    assert await store.patch_if("a", {"timeout": None}, where={"status": ["created"]}) is not None
    assert await store.patch_if("a", {"status": "x"}, where=stale) is None, "a stale guard applied over a null"
    fresh = await store.get("a")
    assert fresh is not None and fresh.timeout is None
    assert await store.patch_if("a", {"status": "y"}, where={"timeout": [raw_generation(fresh, "timeout")]}) is not None


async def a_set_paths_leaf_under_a_typed_sub_model_is_canonicalised_and_a_typo_is_refused(env: Env) -> None:
    store = env.store(StrictDoc)
    await store.create(StrictDoc(id="a"))
    out = await store.patch_if(
        "a", None, where={"status": ["created"]},
        set_paths={("sub", "a"): "7", ("sub", "when"): "2026-10-04T12:30:45+00:00"},
    )
    assert out is not None and out.sub.a == 7
    assert out.sub.when is not None and out.sub.when.utcoffset() == timedelta(0)
    # a leaf the typed sub-model does not carry would be reported written and silently dropped: refused instead, and the
    # refused call's OTHER visible change (a patched field) is rolled back with it
    with pytest.raises(PatchSpecError):
        await store.patch_if(
            "a", {"count": 99}, where={"status": ["created"]}, set_paths={("sub", "typo"): 1},
        )
    fresh = await store.get("a")
    assert fresh is not None and fresh.sub.a == 7
    assert fresh.count == 0, "the refused call's patched field landed although the call was refused"


async def a_legacy_row_missing_a_nullable_field_with_a_non_null_default_never_matches_that_default(env: Env) -> None:
    """A row older than a NULLABLE field whose default is not null: the model reads the default (30), but a nullable field
    is compared as stored, so a guard naming the default does not match the absent key. ``raw_generation`` of such a row
    returns the default, so a guard built from that read is refused for that row until the key is written; ``None``
    (which matches an absent key) is the way in."""
    store = env.store(StrictDoc)
    await env.seed_raw(StrictDoc, "old", {"status": "created"})
    row = await store.get("old")
    assert row is not None and row.timeout == 30
    guard = {"timeout": [raw_generation(row, "timeout")]}
    assert guard == {"timeout": [30]}
    assert await store.patch_if("old", {"status": "x"}, where=guard) is None, "the default matched an absent nullable key"
    assert await store.patch_if("old", {"timeout": 30}, where={"timeout": [None]}) is not None
    assert await store.patch_if("old", {"status": "y"}, where=guard) is not None, "once stored, the guard matches"


RAW = [
    a_loosely_typed_patch_is_stored_canonical_so_a_guard_built_from_a_read_matches,
    a_patch_that_is_already_canonical_is_not_rewritten_or_changed,
    a_document_missing_a_defaulted_field_reads_as_the_default_in_a_guard,
    a_key_the_model_ignores_survives_a_patch_and_is_dropped_by_a_whole_document_update,
    patching_a_field_the_model_does_not_have_is_rejected,
    a_stale_guard_on_a_nullable_field_with_a_default_is_not_applied_over_a_newer_null,
    a_set_paths_leaf_under_a_typed_sub_model_is_canonicalised_and_a_typo_is_refused,
    a_legacy_row_missing_a_nullable_field_with_a_non_null_default_never_matches_that_default,
]


ALL = [
    applies_when_the_guard_matches_and_leaves_other_fields,
    a_rejected_guard_returns_none_and_changes_nothing,
    a_missing_row_raises_not_found_not_none,
    a_patch_does_not_overwrite_a_field_written_since_the_read,
    where_compares_typed_scalars_not_text,
    where_matches_any_listed_value_and_all_fields,
    none_in_where_matches_null_and_absent,
    a_where_path_compares_the_nested_leaf,
    a_leaf_guarded_absent_is_written_once,
    a_malformed_where_path_is_rejected,
    raw_generation_round_trips_for_microseconds_and_a_utc_offset,
    three_paths_sharing_a_parent_all_land_and_siblings_survive,
    a_null_parent_is_replaced_and_the_leaves_set,
    a_scalar_parent_is_replaced_and_the_leaf_set,
    a_missing_parent_is_created,
    keys_with_dots_and_colons_are_one_key_each,
    patch_and_set_paths_apply_together,
    a_rejected_set_paths_write_changes_nothing,
    malformed_specs_are_rejected_with_value_error,
    numbers_compare_by_value_across_int_and_float,
    raw_generation_round_trips_for_float_enum_and_secret,
    a_patch_that_leaves_the_row_unreadable_is_rejected_and_rolled_back,
    a_value_the_model_coerces_to_a_non_finite_float_is_refused_and_the_row_is_unchanged,
    an_empty_or_malformed_where_is_rejected,
    an_array_parent_is_replaced_like_any_non_object,
]
