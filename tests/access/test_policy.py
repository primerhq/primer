"""The access policy: levels, enforcement and the list predicate (access-control spec 5.1, 6.2, 9; plan Task 3).

``level_of`` decides what level a person holds on a row; ``require``
enforces a minimum level (no level: ``NotFoundError``, because an
out-of-access read answers exactly like a missing row; insufficient
level: ``AccessDeniedError``, rendered as 403 problem+json with code
``access_denied``); ``visible_predicate`` compiles the list filter a
person runs against storage over ``access.members`` (unfiltered for
admins, a match-nothing predicate for everyone else); ``tool_is_public``
is the per-tool public flag for Phase 4.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from primer.access.person import Person
from primer.access.policy import level_of, require, tool_is_public, visible_predicate
from primer.api.errors import PROBLEM_JSON_MEDIA_TYPE, register_error_handlers
from primer.model.access import Access, AccessLevel, Owned
from primer.model.common import Identifiable
from primer.model.except_ import AccessDeniedError, NotFoundError
from primer.model.provider import SqliteConfig
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.storage.sqlite import SqliteStorageProvider


class _Widget(Identifiable, Owned):
    """The smallest owned row: enough to exercise the predicate in storage."""

    name: str = ""


@pytest.fixture
async def widget_store(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    """A SQLite provider with three widgets: u1-only, u1+u2-read, public."""
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    storage = provider.get_storage(_Widget)
    await storage.create(_Widget(id="w-owned", name="u1 only", access=Access(owners=["u1"])))
    await storage.create(
        _Widget(id="w-shared", name="u1 + u2 read", access=Access(owners=["u1"], grants={"u2": "read"}))
    )
    await storage.create(_Widget(id="w-public", name="everyone", access=Access(owners=["u1"], public=True)))
    try:
        yield provider
    finally:
        await provider.aclose()


async def _visible_ids(provider: SqliteStorageProvider, person: Person) -> list[str]:
    predicate = visible_predicate(person)
    page = await provider.get_storage(_Widget).find(predicate, OffsetPage(offset=0, length=10))
    return sorted(item.id for item in page.items)


# ---------------------------------------------------------------------------
# level_of
# ---------------------------------------------------------------------------


def test_admin_is_owner_on_any_row():
    assert level_of(Person(None, "admin"), Access()) is AccessLevel.OWNER


def test_system_is_owner_on_any_row():
    assert level_of(Person(None, "system"), Access()) is AccessLevel.OWNER


def test_owner_is_owner():
    assert level_of(Person("u1", "user"), Access(owners=["u1"])) is AccessLevel.OWNER


def test_grant_gives_its_level():
    shared = Access(owners=["u1"], grants={"u2": "read", "u3": "full"})
    assert level_of(Person("u2", "user"), shared) is AccessLevel.READ
    assert level_of(Person("u3", "user"), shared) is AccessLevel.FULL


def test_public_is_read_for_any_full_user():
    public = Access(owners=["u1"], public=True)
    assert level_of(Person("u9", "user"), public) is AccessLevel.READ


def test_no_relationship_is_none():
    assert level_of(Person("u9", "user"), Access(owners=["u1"])) is None


def test_restricted_reads_nothing_not_even_public():
    assert level_of(Person(None, "restricted"), Access(owners=["u1"], public=True)) is None


# ---------------------------------------------------------------------------
# require
# ---------------------------------------------------------------------------


def test_require_returns_the_held_level_when_sufficient():
    held = require(Person("u1", "user"), Access(owners=["u1"]), AccessLevel.FULL, what="agent 'a1'")
    assert held is AccessLevel.OWNER, "the held level is returned, not the requested one"
    assert (
        require(Person("u2", "user"), Access(owners=["u1"], grants={"u2": "read"}), AccessLevel.READ, what="a")
        is AccessLevel.READ
    )


def test_require_no_level_is_not_found_like_a_missing_row():
    with pytest.raises(NotFoundError) as exc:
        require(Person("u9", "user"), Access(owners=["u1"]), AccessLevel.READ, what="agent 'a1'")
    assert exc.value.message == "agent 'a1' does not exist"


def test_require_insufficient_level_is_access_denied():
    with pytest.raises(AccessDeniedError) as exc:
        require(
            Person("u2", "user"),
            Access(owners=["u1"], grants={"u2": "read"}),
            AccessLevel.FULL,
            what="agent 'a1'",
        )
    assert exc.value.message == "agent 'a1' needs full access"


# ---------------------------------------------------------------------------
# visible_predicate (predicate shape)
# ---------------------------------------------------------------------------


def _contains(value: str) -> Predicate:
    return Predicate(left=FieldRef(name="access.members"), op=Op.CONTAINS, right=Value(value=value))


def test_admin_predicate_is_none():
    assert visible_predicate(Person(None, "admin")) is None
    assert visible_predicate(Person(None, "system")) is None


def test_full_user_predicate_is_membership_or_public():
    predicate = visible_predicate(Person("u1", "user"))
    assert predicate.op is Op.OR
    assert predicate.left == _contains("u:u1")
    assert predicate.right == _contains("public")


def test_full_user_without_user_id_gets_public_only():
    assert visible_predicate(Person(None, "user")) == _contains("public")


def test_everyone_else_matches_nothing():
    assert visible_predicate(Person(None, "restricted")) == _contains("__none__")


# ---------------------------------------------------------------------------
# visible_predicate (against real storage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_visible_predicate_against_real_storage(widget_store):
    assert await _visible_ids(widget_store, Person(None, "admin")) == ["w-owned", "w-public", "w-shared"]
    assert await _visible_ids(widget_store, Person("u1", "user")) == ["w-owned", "w-public", "w-shared"]
    assert await _visible_ids(widget_store, Person("u2", "user")) == ["w-public", "w-shared"]
    assert await _visible_ids(widget_store, Person("u3", "user")) == ["w-public"]
    assert await _visible_ids(widget_store, Person(None, "restricted")) == []


@pytest.mark.asyncio
async def test_list_and_get_agree_on_every_row_and_person(widget_store):
    """The list predicate and level_of must agree per (row, person): a row appears
    in a person's list if and only if level_of gives them a level on it. Otherwise
    a listed row 404s on GET (public_tools-only rows must not be listable)."""
    storage = widget_store.get_storage(_Widget)
    rows = [
        ("m-private", Access(owners=["u1"])),
        ("m-shared-read", Access(owners=["u1"], grants={"u2": "read"})),
        ("m-shared-full", Access(owners=["u1"], grants={"u2": "full"})),
        ("m-public", Access(owners=["u1"], public=True)),
        ("m-public-tools", Access(owners=["u1"], public_tools=["t"])),
        ("m-multi-owner", Access(owners=["u1", "u2"])),
    ]
    for row_id, access in rows:
        await storage.create(_Widget(id=row_id, name="matrix", access=access))
    people = [
        Person(None, "admin"),
        Person("u1", "user"),
        Person("u2", "user"),
        Person("u3", "user"),
        Person(None, "user"),
        Person(None, "restricted"),
    ]
    for person in people:
        page = await storage.find(visible_predicate(person), OffsetPage(offset=0, length=10))
        listed = {item.id for item in page.items if item.id.startswith("m-")}
        expected = {row_id for row_id, access in rows if level_of(person, access) is not None}
        assert listed == expected, f"{person} sees {sorted(listed)}, level_of says {sorted(expected)}"


# ---------------------------------------------------------------------------
# tool_is_public
# ---------------------------------------------------------------------------


def test_tool_is_public():
    assert tool_is_public(Access(owners=["u1"]), "t") is False
    assert tool_is_public(Access(owners=["u1"], public=True), "t") is True
    assert tool_is_public(Access(owners=["u1"], public_tools=["t"]), "t") is True
    assert tool_is_public(Access(owners=["u1"], public_tools=["t"]), "other") is False


# ---------------------------------------------------------------------------
# the 403 envelope
# ---------------------------------------------------------------------------


def test_access_denied_renders_403_problem_json_with_code():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raise")
    def _raise() -> None:
        raise AccessDeniedError("agent 'a1' needs full access")

    response = TestClient(app, raise_server_exceptions=False).get("/raise")
    assert response.status_code == 403
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    body = response.json()
    assert body["type"] == "/errors/forbidden"
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == "agent 'a1' needs full access"
    assert body["extensions"]["code"] == "access_denied"
