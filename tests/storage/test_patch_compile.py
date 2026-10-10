"""The pure parts of ``patch_if``: the compilers' shape and the ABC wiring. No database."""

from __future__ import annotations

import itertools
import json
import re
import sqlite3

import pytest

from primer.int.storage import Storage
from primer.storage import raw_generation
from primer.storage._patch import (
    PatchSpecError,
    compile_postgres,
    compile_sqlite,
    document_matches,
    parent_paths,
    validate_patch,
)
from primer.storage.postgres import PostgresStorage
from primer.storage.sqlite import SqliteStorage
from tests.storage import _patch_reference as ref
from tests.storage._patch_scenarios import PatchDoc

_PATCHES = [{}, {"status": "x"}, {"status": "x", "token": None}]
_PATHS = [
    {},
    {("state", "a"): 1},
    {("state", "ps", "k1"): 1, ("state", "ps", "k2"): {"n": [1]}},
    {("state", "ps", "k"): 1, ("other", "x", "y"): "v", ("state", "q"): 2},
    {("state", "ps", "k"): 1, ("other", "x"): "v"},                              # three parents
]
_WHERES = [
    {"status": ["zzz"]},                         # does not match _DOC: the rejected-guard leg
    {"status": ["a"]},
    {"status": ["a", None], "flag": [True], "count": [1, 2.5]},
    {("state", "ps", "existing"): [1], "status": ["a"]},      # a path to a nested leaf, beside a field
    {("state", "ps", "missing"): [None]},                     # an absent leaf
    {("state", "ps", "existing"): [None]},                    # a set leaf is not absent: the rejected leg
    {("other", "x"): [None, 3]},                              # under a scalar parent: absent
]


def _specs():
    for patch, paths, where in itertools.product(_PATCHES, _PATHS, _WHERES):
        if not patch and not paths:
            continue
        yield patch, paths, where


@pytest.mark.parametrize("patch,paths,where", list(_specs()))
def test_sqlite_placeholder_and_parameter_counts_agree(patch, paths, where):
    """SQLite binds positionally: a count mismatch is an immediate error. (Order is pinned by
    ``test_sqlite_compiled_update_matches_the_oracle`` below, which executes every spec.)"""
    p, sp, w = validate_patch(patch, paths, where)
    set_expr, set_params, where_sql, where_params = compile_sqlite(p, sp, w)
    assert set_expr.count("?") == len(set_params)
    assert where_sql.count("?") == len(where_params)


@pytest.mark.parametrize("patch,paths,where", list(_specs()))
def test_postgres_placeholders_are_dense_from_the_first_param(patch, paths, where):
    p, sp, w = validate_patch(patch, paths, where)
    set_expr, where_sql, params = compile_postgres(p, sp, w, first_param=2)
    used = {int(n) for n in re.findall(r"\$(\d+)::", set_expr + " " + where_sql)}
    # every placeholder in the text is a parameter we return, and every parameter is used
    assert used == set(range(2, 2 + len(params)))


def test_parents_are_ensured_shallowest_first_and_deduplicated():
    paths = {("a", "b", "c"): 1, ("a", "x"): 2, ("z", "y"): 3}
    assert parent_paths(paths) == [("a",), ("z",), ("a", "b")]


def test_the_ensure_order_in_the_postgres_text_is_shallowest_first():
    _, _, params = compile_postgres(
        {}, {("a", "b", "c"): 1}, {"status": ["x"]}, first_param=2,
    )
    # parameters are appended in emission order: ensure ("a",), ensure ("a","b"), then the leaf
    assert params[:3] == [["a"], ["a", "b"], ["a", "b", "c"]]


def test_a_postgres_where_list_is_a_filter_on_the_row_never_an_in_sub_select():
    """``(data -> f) IN (SELECT jsonb_array_elements(...))`` is planned as a semi-join, and when the UPDATE waits on a
    concurrent writer's row lock, the READ COMMITTED re-check compares the committed row with only the list element the first
    pass matched: a row moved to ANOTHER allowed value is refused. The live proof is
    ``test_patch_if_postgres_multi_value_guard.py`` (Postgres lane only); this pins the filter form in every lane."""
    p, sp, w = validate_patch({"x": 1}, {}, {"status": ["a", "b"], "token": ["t", None], "count": [1]})
    _, where_sql, _ = compile_postgres(p, sp, w, first_param=2)
    assert "IN (SELECT" not in where_sql
    assert where_sql.count("= ANY(ARRAY(SELECT jsonb_array_elements(") == 3, where_sql


def test_a_where_value_is_typed_json_never_spliced():
    p, sp, w = validate_patch({"x": 1}, {}, {"status": ["a'; DROP TABLE t; --"]})
    set_expr, where_sql, params = compile_postgres(p, sp, w, first_param=2)
    assert "DROP" not in set_expr + where_sql
    assert any("DROP TABLE" in str(v) for v in params)


@pytest.mark.parametrize(
    "doc,where,expected",
    [
        ({"a": True}, {"a": [True]}, True),
        ({"a": True}, {"a": [1]}, False),
        ({"a": True}, {"a": ["true"]}, False),
        ({"a": 1}, {"a": [1.0]}, True),
        ({"a": None}, {"a": [None]}, True),
        ({}, {"a": [None]}, True),
        ({}, {"a": ["x"]}, False),
        ({"a": "x", "b": 2}, {"a": ["x"], "b": [1, 2]}, True),
        ({"a": "x", "b": 3}, {"a": ["x"], "b": [1, 2]}, False),
        ({"s": {"a": 1}}, {("s", "a"): [1.0]}, True),                 # a path names the nested leaf
        ({"s": {"a": 1}}, {("s", "a"): [None]}, False),
        ({"s": {"a": None}}, {("s", "a"): [None]}, True),
        ({"s": {}}, {("s", "a"): [None]}, True),
        ({}, {("s", "a"): [None]}, True),
        ({"s": 5}, {("s", "a"): [None]}, True),                       # under a scalar parent
        ({"s": [7]}, {("s", "0"): [None]}, True),                     # a path never indexes into an array
        ({"s": {"a": 1}, "b": 2}, {("s", "a"): [1], "b": [3]}, False),
    ],
)
def test_document_matches_agrees_with_the_independent_oracle(doc, where, expected):
    """The production cross-check and the test oracle (which the fake uses) must say the same thing."""
    assert document_matches(doc, where) is expected
    assert ref.doc_matches(doc, where) is expected


@pytest.mark.parametrize(
    "patch,paths,where",
    [
        ({"": 1}, None, {"status": ["a"]}),                       # an empty patch key
        ({1: 1}, None, {"status": ["a"]}),                        # a patch key that is not a string
        (None, {("state", ""): 1}, {"status": ["a"]}),            # an empty path element
        ({"count": 1}, None, {"": ["a"]}),                        # an empty where field
        (None, {("id", "x"): 1}, {"status": ["a"]}),              # a nested path rooted at the id
        ({"count": 1}, None, {(): ["a"]}),                        # an empty where path
        ({"count": 1}, None, {("state", ""): ["a"]}),             # an empty where path element
        ({"count": 1}, None, {("id", "x"): ["a"]}),               # a where path rooted at the id
    ],
)
def test_validate_patch_refuses_an_empty_or_non_string_key_and_a_path_rooted_at_the_id(patch, paths, where):
    with pytest.raises(PatchSpecError):
        validate_patch(patch, paths, where)


def test_a_one_element_where_path_is_the_field_itself():
    """So a guard spelled as a path is normalised for a defaulted field exactly as the field name is."""
    assert validate_patch({"count": 1}, None, {("status",): ["a"], ("state", "k"): [None]})[2] == {"status": ["a"], ("state", "k"): [None]}


def test_raw_generation_is_the_stored_json_value_not_a_python_object():
    from datetime import datetime, timezone

    row = PatchDoc(id="a", stamp=datetime(2026, 10, 4, 12, 0, 0, 5, tzinfo=timezone.utc), token="t")
    assert raw_generation(row, "token") == "t"
    gen = raw_generation(row, "stamp")
    assert isinstance(gen, str) and gen.startswith("2026-10-04T12:00:00.000005")
    with pytest.raises(PatchSpecError):
        raw_generation(row, "no_such_field")


def test_every_real_storage_backend_implements_patch_if():
    """`patch_if` is abstract on the ABC; a backend that forgot it could not be instantiated, but
    pin the overrides so a refactor that moves them onto a mixin is a conscious change."""
    assert getattr(Storage.patch_if, "__isabstractmethod__", False)
    for backend in (SqliteStorage, PostgresStorage):
        assert backend.patch_if is not Storage.patch_if


_DOC = {
    "status": "a", "flag": True, "count": 1, "token": None,
    "state": {"ps": {"existing": 1}, "keep": 2},
    "other": 5,                       # a scalar parent
}


@pytest.mark.parametrize("patch,paths,where", list(_specs()))
def test_sqlite_compiled_update_matches_the_oracle(patch, paths, where):
    """Execute every compiled spec against a real SQLite JSON column and compare with the pure-Python
    oracle: this pins the POSITIONAL parameter order for 0..4 distinct parents with patch keys, which the
    scenarios (at most two parents) do not. A spec whose guard does not match must leave the row alone."""
    p, sp, w = validate_patch(patch, paths, where)
    set_expr, set_params, where_sql, where_params = compile_sqlite(p, sp, w)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    db.execute("INSERT INTO t VALUES ('r', ?)", (json.dumps(_DOC),))
    sql = f"UPDATE t SET data = {set_expr} WHERE id = ?" + (f" AND {where_sql}" if where_sql else "")
    cur = db.execute(sql, (*set_params, "r", *where_params))
    stored = json.loads(db.execute("SELECT data FROM t").fetchone()[0])
    if ref.doc_matches(_DOC, w):
        assert cur.rowcount == 1
        assert stored == ref.apply_patch(_DOC, p, sp)
    else:
        assert cur.rowcount == 0 and stored == _DOC
