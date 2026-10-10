"""A cursor is bound to the request that issued it (ticket 01a1212a, vector 3).

``_decode_cursor_for`` accepts only the keys ``_encode_cursor_for`` emits for THIS request's ``order_by`` plus the ``id`` tiebreaker. A cursor
whose keys name other fields, are in another order, have extra/missing keys, or carry a non-scalar value is refused with ``BadRequestError``
(the same answer a malformed cursor gives). The seek compares against the stored document, so a client-chosen key would be a query on a field the
client never sorted by.
"""

from __future__ import annotations

import base64
import json

import pytest

from primer.model.common import Identifiable
from primer.model.except_ import BadRequestError
from primer.model.storage import OrderBy
from primer.storage._cursor import _decode_cursor_for, _encode_cursor_for


class _Sample(Identifiable):
    name: str
    count: int = 0


def _forge(keys: list[dict]) -> str:
    payload = json.dumps({"keys": keys}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()


def test_server_cursor_round_trips_for_the_same_order_by() -> None:
    order = [OrderBy(field="name", direction="asc")]
    cursor = _encode_cursor_for(_Sample(id="s1", name="a", count=1), order)
    state = _decode_cursor_for(cursor, order, _Sample)
    assert [k["field"] for k in state["keys"]] == ["name", "id"]


def test_default_id_order_round_trips() -> None:
    cursor = _encode_cursor_for(_Sample(id="s1", name="a"), None)
    state = _decode_cursor_for(cursor, None, _Sample)
    assert [k["field"] for k in state["keys"]] == ["id"]


def test_cursor_from_a_different_order_by_is_refused() -> None:
    cursor = _encode_cursor_for(
        _Sample(id="s1", name="a"), [OrderBy(field="name", direction="asc")]
    )
    # Follow-up request sorts by nothing (id only): keys no longer match.
    with pytest.raises(BadRequestError):
        _decode_cursor_for(cursor, None, _Sample)


def test_forged_key_naming_another_field_is_refused() -> None:
    forged = _forge(
        [
            {"field": "git_token", "value": "g", "direction": "asc", "is_null": False},
            {"field": "id", "value": "s1", "direction": "asc", "is_null": False},
        ]
    )
    with pytest.raises(BadRequestError):
        _decode_cursor_for(forged, None, _Sample)


def test_forged_key_with_no_order_by_but_an_extra_seek_key_is_refused() -> None:
    forged = _forge(
        [
            {"field": "count", "value": 5, "direction": "asc", "is_null": False},
            {"field": "id", "value": "s1", "direction": "asc", "is_null": False},
        ]
    )
    with pytest.raises(BadRequestError):
        _decode_cursor_for(forged, None, _Sample)


def test_wrong_direction_is_refused() -> None:
    forged = _forge(
        [
            {"field": "name", "value": "a", "direction": "desc", "is_null": False},
            {"field": "id", "value": "s1", "direction": "asc", "is_null": False},
        ]
    )
    with pytest.raises(BadRequestError):
        _decode_cursor_for(forged, [OrderBy(field="name", direction="asc")], _Sample)


def test_missing_id_tiebreaker_is_refused() -> None:
    forged = _forge(
        [{"field": "name", "value": "a", "direction": "asc", "is_null": False}]
    )
    with pytest.raises(BadRequestError):
        _decode_cursor_for(forged, [OrderBy(field="name", direction="asc")], _Sample)


def test_non_string_id_value_is_refused() -> None:
    forged = _forge(
        [{"field": "id", "value": 5, "direction": "asc", "is_null": False}]
    )
    with pytest.raises(BadRequestError):
        _decode_cursor_for(forged, None, _Sample)


def test_non_scalar_value_is_refused() -> None:
    forged = _forge(
        [{"field": "id", "value": ["x"], "direction": "asc", "is_null": False}]
    )
    with pytest.raises(BadRequestError):
        _decode_cursor_for(forged, None, _Sample)


def test_is_null_flag_must_agree_with_the_value() -> None:
    forged = _forge(
        [{"field": "id", "value": "s1", "direction": "asc", "is_null": True}]
    )
    with pytest.raises(BadRequestError):
        _decode_cursor_for(forged, None, _Sample)


def test_extra_top_level_key_is_refused() -> None:
    payload = json.dumps(
        {
            "keys": [
                {"field": "id", "value": "s1", "direction": "asc", "is_null": False}
            ],
            "sig": "x",
        },
        separators=(",", ":"),
    )
    forged = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    with pytest.raises(BadRequestError):
        _decode_cursor_for(forged, None, _Sample)


def test_a_value_of_the_wrong_type_for_an_allowed_key_is_refused() -> None:
    # ``count`` is an int field the request legitimately sorts by, but the
    # seek value is a string: it would bind against the ``::bigint`` cast and
    # answer a backend error, so it is refused here as a bad cursor instead.
    forged = _forge(
        [
            {"field": "count", "value": "not-a-number", "direction": "asc", "is_null": False},
            {"field": "id", "value": "s1", "direction": "asc", "is_null": False},
        ]
    )
    with pytest.raises(BadRequestError):
        _decode_cursor_for(forged, [OrderBy(field="count", direction="asc")], _Sample)


def test_a_bool_dressed_as_int_value_for_an_int_key_is_refused() -> None:
    # A JSON ``true`` for an int field is not an int bind.
    forged = _forge(
        [
            {"field": "count", "value": True, "direction": "asc", "is_null": False},
            {"field": "id", "value": "s1", "direction": "asc", "is_null": False},
        ]
    )
    with pytest.raises(BadRequestError):
        _decode_cursor_for(forged, [OrderBy(field="count", direction="asc")], _Sample)


def test_a_correctly_typed_value_for_an_allowed_key_is_accepted() -> None:
    forged = _forge(
        [
            {"field": "count", "value": 7, "direction": "asc", "is_null": False},
            {"field": "id", "value": "s1", "direction": "asc", "is_null": False},
        ]
    )
    state = _decode_cursor_for(forged, [OrderBy(field="count", direction="asc")], _Sample)
    assert state["keys"][0]["value"] == 7


def test_malformed_cursor_still_raises() -> None:
    with pytest.raises(BadRequestError):
        _decode_cursor_for("!!!not-base64!!!", None, _Sample)
