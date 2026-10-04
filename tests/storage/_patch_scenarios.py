"""Behavioural scenarios for ``Storage.patch_if``, run against every implementation.

``tests/storage/test_storage_contract.py`` runs each against SQLite and (gated) Postgres;
``tests/storage/test_patch_if_fake.py`` runs the same ones against the in-memory fake in
``tests/conftest.py``, so the fake that the rest of the suite leans on cannot drift from the backends.
Each scenario creates its own rows and takes only a ``Storage[PatchDoc]``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import pytest
from pydantic import ConfigDict, SecretStr, ValidationError

from primer.int.storage import Storage
from primer.model.common import Identifiable
from primer.model.except_ import NotFoundError
from primer.storage import raw_generation


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
        with pytest.raises(ValueError):
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


async def an_empty_or_malformed_where_is_rejected(store: Store) -> None:
    await _mk(store, "a")
    for bad_where in ({}, {"status": "running"}, {"status": b"running"}, {"id": ["a"]}):
        with pytest.raises(ValueError):
            await store.patch_if("a", {"count": 1}, where=bad_where)  # type: ignore[arg-type]
    # more distinct parent objects than the cap, and more leaves than the cap
    many_parents = {(f"p{i}", "k"): 1 for i in range(6)}
    with pytest.raises(ValueError):
        await store.patch_if("a", None, where={"status": ["created"]}, set_paths=many_parents)
    many_leaves = {("state", f"k{i}"): i for i in range(40)}
    with pytest.raises(ValueError):
        await store.patch_if("a", None, where={"status": ["created"]}, set_paths=many_leaves)


async def an_array_parent_is_replaced_like_any_non_object(store: Store) -> None:
    await _mk(store, "a", state=[1, 2, 3])
    out = await store.patch_if(
        "a", None, where={"status": ["created"]}, set_paths={("state", "k"): 1},
    )
    assert out is not None and out.state == {"k": 1}


ALL = [
    applies_when_the_guard_matches_and_leaves_other_fields,
    a_rejected_guard_returns_none_and_changes_nothing,
    a_missing_row_raises_not_found_not_none,
    a_patch_does_not_overwrite_a_field_written_since_the_read,
    where_compares_typed_scalars_not_text,
    where_matches_any_listed_value_and_all_fields,
    none_in_where_matches_null_and_absent,
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
    an_empty_or_malformed_where_is_rejected,
    an_array_parent_is_replaced_like_any_non_object,
]
