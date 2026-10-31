"""``toolset_use`` and ``may_use_tool`` (access-control P4 Task 2): which tools of which toolsets a person may use.

A matrix of owner, grantee, public row, public tool only, stranger, restricted, admin and built-in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from primer.access.person import Person
from primer.access.policy import catalogue_predicate, may_use_tool
from primer.access.tools import toolset_use
from primer.model.access import Access
from primer.model.provider import SqliteConfig, Toolset, ToolsetProviderType
from primer.model.storage import OffsetPage
from primer.storage.sqlite import SqliteStorageProvider

OWNER = Person("u-owner", "user")
GRANTEE = Person("u-grantee", "user")
STRANGER = Person("u-stranger", "user")
RESTRICTED = Person("u-restricted", "restricted")
ADMIN = Person("u-admin", "admin")


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    store = provider.get_storage(Toolset)
    for tid, access in {
        "private": Access(owners=["u-owner"], grants={"u-grantee": "read"}),
        "public-row": Access(owners=["u-owner"], public=True),
        "one-tool": Access(owners=["u-owner"], public_tools=["t1"]),
    }.items():
        await store.create(Toolset(id=tid, provider=ToolsetProviderType.INTERNAL, access=access))
    try:
        yield provider
    finally:
        await provider.aclose()


def _fields() -> dict:
    return {}


_FIELDS: dict = _fields()

IDS = ["private", "public-row", "one-tool", "builtin"]


@pytest.mark.parametrize(("who", "expected"), [
    (OWNER, {"private": None, "public-row": None, "one-tool": None, "builtin": None}),
    (GRANTEE, {"private": None, "public-row": None, "one-tool": frozenset({"t1"}), "builtin": None}),
    (STRANGER, {"private": frozenset(), "public-row": None, "one-tool": frozenset({"t1"}), "builtin": None}),
    (RESTRICTED, {i: frozenset() for i in IDS}),
    (ADMIN, dict.fromkeys(IDS)),
], ids=["owner", "grantee", "stranger", "restricted", "admin"])
async def test_toolset_use_matrix(sp, who, expected) -> None:
    assert await toolset_use(sp, who, IDS) == expected


async def test_admin_short_circuits_with_no_read() -> None:
    class _Boom:
        def get_storage(self, model):
            raise AssertionError("an admin must not read the toolset rows")

    assert await toolset_use(_Boom(), ADMIN, ["a", "b"]) == {"a": None, "b": None}


async def test_one_batched_read_for_many_toolsets(sp) -> None:
    reads: list[int] = []
    real = sp.get_storage

    class _Counting:
        def __init__(self, inner) -> None:
            self._inner = inner

        async def find(self, *a, **k):
            reads.append(1)
            return await self._inner.find(*a, **k)

    class _SP:
        def get_storage(self, model):
            return _Counting(real(model))

    await toolset_use(_SP(), STRANGER, IDS + [f"x{i}" for i in range(50)])
    assert len(reads) == 1


def test_may_use_tool_is_exactly_the_public_tool() -> None:
    access = Access(owners=["u-owner"], public_tools=["t1"])
    assert may_use_tool(STRANGER, access, "t1") is True
    assert may_use_tool(STRANGER, access, "t2") is False
    assert may_use_tool(RESTRICTED, access, "t1") is False
    assert may_use_tool(GRANTEE, Access(owners=["u-owner"], grants={"u-grantee": "read"}), "t2") is True
    assert may_use_tool(OWNER, access, "t2") is True


def test_the_row_with_public_tools_carries_the_catalogue_token_only() -> None:
    access = Access(owners=["u-owner"], public_tools=["t1"])
    assert "tools:public" in access.members and "public" not in access.members
    assert "tools:public" not in Access(owners=["u-owner"]).members


def test_catalogue_predicate_is_none_for_admins() -> None:
    assert catalogue_predicate(ADMIN) is None
    assert catalogue_predicate(STRANGER) is not None


async def test_catalogue_predicate_lists_the_row_with_public_tools(sp) -> None:
    page = await sp.get_storage(Toolset).find(catalogue_predicate(STRANGER), OffsetPage(offset=0, length=50))
    assert {r.id for r in page.items} == {"public-row", "one-tool"}
