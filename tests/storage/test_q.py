"""Unit tests for the Q typed predicate builder (primer.storage.q)."""

import pytest
from pydantic import BaseModel

from primer.storage.q import Q
from primer.model.storage import FieldRef, Op, Predicate, Value


class _M(BaseModel):
    a: str = ""
    b: int = 0


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


def test_where_validates_field_at_call_time():
    with pytest.raises(KeyError) as exc:
        Q(_M).where("nonexistent", 1)
    assert "nonexistent" in str(exc.value)


def test_known_field_builds_predicate():
    p = Q(_M).where("a", "x").build()
    assert p == Predicate(left=FieldRef(name="a"), op=Op.EQ, right=Value(value="x"))


def test_chained_where_builds_and_tree():
    p = Q(_M).where("a", "x").where("b", 1).build()
    assert p.op == Op.AND


def test_where_in_builds_list_value():
    p = Q(_M).where_in("b", [1, 2, 3]).build()
    assert p.right.value == [1, 2, 3]


def test_where_null_validated():
    # Valid field — should build without raising.
    Q(_M).where_null("a").build()
    # Invalid field — should raise KeyError.
    with pytest.raises(KeyError):
        Q(_M).where_null("missing")


def test_where_not_null_validated():
    Q(_M).where_not_null("b").build()
    with pytest.raises(KeyError):
        Q(_M).where_not_null("missing")


def test_where_op_validates_field():
    Q(_M).where_op("b", Op.GT, 5).build()
    with pytest.raises(KeyError):
        Q(_M).where_op("nonexistent", Op.GT, 5)


# ---------------------------------------------------------------------------
# OR combinator
# ---------------------------------------------------------------------------


def test_or_combines_qs():
    q1 = Q(_M).where("a", "x")
    q2 = Q(_M).where("a", "y")
    p = Q.or_(q1, q2).build()
    assert p.op == Op.OR


def test_or_requires_at_least_two_qs():
    with pytest.raises(ValueError):
        Q.or_(Q(_M).where("a", "x"))


def test_or_raises_on_mismatched_model():
    class _Other(BaseModel):
        x: str = ""

    with pytest.raises(TypeError):
        Q.or_(Q(_M).where("a", "x"), Q(_Other).where("x", "y"))


# ---------------------------------------------------------------------------
# Empty Q
# ---------------------------------------------------------------------------


def test_empty_q_raises_on_build():
    with pytest.raises(ValueError):
        Q(_M).build()


# ---------------------------------------------------------------------------
# Dotted JSONB path safety
# ---------------------------------------------------------------------------


def test_jsonb_path_rejects_injection_chars():
    class M2(BaseModel):
        data: dict = {}

    with pytest.raises(ValueError):
        Q(M2).where("data.foo';drop table--", "x")


def test_jsonb_path_valid_accepts():
    class M3(BaseModel):
        meta: dict = {}

    # Valid dotted path — should not raise.
    Q(M3).where("meta.author", "alice").build()


def test_jsonb_path_rejects_whitespace_segment():
    class M4(BaseModel):
        data: dict = {}

    with pytest.raises(ValueError):
        Q(M4).where("data. injection", "x")


# ---------------------------------------------------------------------------
# Fuzz: mangled field names must never silently produce a predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_field",
    [
        "a; DROP TABLE chats",
        "a' OR '1'='1",
        "a/**/UNION",
        "a UNION SELECT",
        "a\nDROP",
        "a\0",
        " ",
        "",
        "1=1",
    ],
)
def test_fuzz_field_names(bad_field):
    class _M(BaseModel):  # noqa: F811  (shadow outer _M intentionally)
        a: str = ""

    with pytest.raises((KeyError, ValueError)):
        Q(_M).where(bad_field, "x")


# ---------------------------------------------------------------------------
# Structural correctness of built predicates
# ---------------------------------------------------------------------------


def test_single_where_returns_leaf_predicate():
    p = Q(_M).where("b", 42).build()
    assert isinstance(p, Predicate)
    assert p.op == Op.EQ
    assert isinstance(p.left, FieldRef)
    assert p.left.name == "b"
    assert isinstance(p.right, Value)
    assert p.right.value == 42


def test_three_wheres_left_leaning_and_tree():
    class _M3(BaseModel):
        x: str = ""
        y: int = 0
        z: float = 0.0

    p = Q(_M3).where("x", "a").where("y", 1).where("z", 2.5).build()
    # Root AND
    assert p.op == Op.AND
    # Left subtree is another AND (left-leaning)
    assert isinstance(p.left, Predicate)
    assert p.left.op == Op.AND
    # Right leaf is the third predicate
    assert isinstance(p.right, Predicate)
    assert p.right.op == Op.EQ


def test_where_in_uses_in_op():
    p = Q(_M).where_in("a", ["x", "y"]).build()
    assert p.op == Op.IN
    assert isinstance(p.right, Value)
    assert p.right.value == ["x", "y"]


def test_or_three_qs_left_leaning():
    q1 = Q(_M).where("a", "x")
    q2 = Q(_M).where("a", "y")
    q3 = Q(_M).where("b", 0)
    p = Q.or_(q1, q2, q3).build()
    # Root should be OR
    assert p.op == Op.OR
    # Left should also be OR (left-leaning fold)
    assert isinstance(p.left, Predicate)
    assert p.left.op == Op.OR
