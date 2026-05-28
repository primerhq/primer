"""Unit tests for primer.api.pagination — query-param translators + FindRequest."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from primer.api.errors import register_error_handlers
from primer.api.pagination import FindRequest, parse_order_by, parse_page
from primer.model.except_ import BadRequestError
from primer.model.storage import (
    CursorPage,
    OffsetPage,
    Op,
    OrderBy,
    Predicate,
)


class TestParsePage:
    def test_offset_default(self) -> None:
        page = parse_page(limit=10, offset=None, cursor=None)
        assert isinstance(page, OffsetPage)
        assert page.offset == 0
        assert page.length == 10

    def test_explicit_offset(self) -> None:
        page = parse_page(limit=10, offset=20, cursor=None)
        assert isinstance(page, OffsetPage)
        assert page.offset == 20

    def test_cursor(self) -> None:
        page = parse_page(limit=10, offset=None, cursor="abc")
        assert isinstance(page, CursorPage)
        assert page.cursor == "abc"
        assert page.length == 10

    def test_both_offset_and_cursor_raises(self) -> None:
        with pytest.raises(BadRequestError, match="either"):
            parse_page(limit=10, offset=0, cursor="abc")


class TestParseOrderBy:
    def test_none_returns_none(self) -> None:
        assert parse_order_by(None) is None

    def test_empty_list_returns_none(self) -> None:
        assert parse_order_by([]) is None

    def test_single_field_default_asc(self) -> None:
        result = parse_order_by(["name"])
        assert result == [OrderBy(field="name", direction="asc")]

    def test_explicit_directions(self) -> None:
        result = parse_order_by(["name:asc", "id:desc"])
        assert result == [
            OrderBy(field="name", direction="asc"),
            OrderBy(field="id", direction="desc"),
        ]

    def test_invalid_direction_raises(self) -> None:
        with pytest.raises(BadRequestError, match="direction"):
            parse_order_by(["name:sideways"])

    def test_empty_field_raises(self) -> None:
        with pytest.raises(BadRequestError, match="non-empty"):
            parse_order_by([":asc"])


class TestFindRequest:
    def test_minimal_offset_pagination(self) -> None:
        req = FindRequest.model_validate(
            {"page": {"kind": "offset", "offset": 0, "length": 20}}
        )
        assert req.predicate is None
        assert isinstance(req.page, OffsetPage)
        assert req.order_by is None

    def test_with_predicate(self) -> None:
        req = FindRequest.model_validate(
            {
                "predicate": {
                    "kind": "predicate",
                    "left": {"kind": "field", "name": "id"},
                    "op": "=",
                    "right": {"kind": "value", "value": "x"},
                },
                "page": {"kind": "cursor", "cursor": None, "length": 10},
                "order_by": [{"field": "id", "direction": "asc"}],
            }
        )
        assert isinstance(req.predicate, Predicate)
        assert req.predicate.op == Op.EQ
        assert isinstance(req.page, CursorPage)
        assert req.order_by is not None
        assert req.order_by[0].field == "id"


class TestEndToEnd:
    def test_query_params_round_trip(self) -> None:
        app = FastAPI()
        register_error_handlers(app)

        @app.get("/items")
        def _list(page=Depends(parse_page), order_by=Depends(parse_order_by)) -> dict:
            return {
                "kind": page.kind,
                "length": page.length,
                "order_by": [
                    {"field": o.field, "direction": o.direction}
                    for o in (order_by or [])
                ],
            }

        client = TestClient(app)
        response = client.get(
            "/items?limit=5&offset=10&order_by=name:asc&order_by=id:desc"
        )
        assert response.status_code == 200
        body = response.json()
        assert body == {
            "kind": "offset",
            "length": 5,
            "order_by": [
                {"field": "name", "direction": "asc"},
                {"field": "id", "direction": "desc"},
            ],
        }

    def test_both_offset_and_cursor_returns_400(self) -> None:
        app = FastAPI()
        register_error_handlers(app)

        @app.get("/items")
        def _list(page=Depends(parse_page)) -> dict:
            return {"kind": page.kind}

        client = TestClient(app)
        response = client.get("/items?limit=5&offset=0&cursor=abc")
        assert response.status_code == 400
        body = response.json()
        assert body["type"] == "/errors/bad-request"
