"""The ids a person may read, over many visible rows (perf plan Task 4, finding H1): the answers every list door builds on must not change.

``readable_ids`` / ``readable_workspace_ids`` / ``scope_to_readable_workspaces`` / ``readable_documents`` decide which workspaces, sessions, collections and documents a
non-admin lists. The expected answer here is an ORACLE: every row read in full and judged by ``level_of``, independent of how the implementation
finds them. The matrix runs 0, 1, 199, 200, 201 and 450 visible rows (200 is the page size, where a paging walk goes wrong), every level, every
kind of person, and a revoked grant, on the in-memory store and on real SQLite. These tests pass on the unchanged code and must pass unchanged
after the ids-only and semi-join changes.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.access.person import Person
from primer.access.policy import level_of, readable_ids
from primer.access.workspaces import readable_workspace_ids, scope_to_readable_workspaces
from primer.model.access import LEVEL_RANK, Access, AccessLevel
from primer.model.collection import Collection, Document
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider
from tests.conftest import _FakeStorageProvider

NOW = datetime(2026, 10, 31, tzinfo=UTC)
ME, OTHER = "u-me", "u-other"
HIDDEN = 20


def _ws(wid: str, access: Access) -> Workspace:
    return Workspace(
        id=wid, template_id="t", provider_id="p", created_at=NOW, runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"), access=access,
    )


def _access_for(i: int) -> Access:
    """The ``i``th row ME can see, in rotation: owned, read grant, full grant, public (someone else's), owned jointly."""
    kind = i % 5
    if kind == 0:
        return Access(owners=[ME])
    if kind == 1:
        return Access(owners=[OTHER], grants={ME: "read"})
    if kind == 2:
        return Access(owners=[OTHER], grants={ME: "full"})
    if kind == 3:
        return Access(owners=[OTHER], public=True)
    return Access(owners=[OTHER, ME])


async def _provider(kind: str, tmp_path):
    if kind == "memory":
        return _FakeStorageProvider()
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await sp.initialize()
    return sp


async def _seed(sp, visible: int) -> None:
    store = sp.get_storage(Workspace)
    for i in range(visible):
        await store.create(_ws(f"ws-v{i:04d}", _access_for(i)))
    for i in range(HIDDEN):  # rows ME cannot see
        await store.create(_ws(f"ws-h{i:03d}", Access(owners=[OTHER], grants={"u-third": "read"})))


async def _all(store) -> list:
    rows, offset = [], 0
    while True:
        page = await store.list(OffsetPage(offset=offset, length=200))
        rows.extend(page.items)
        if len(page.items) < 200:
            return rows
        offset += 200


def _oracle(rows, person: Person, need: AccessLevel) -> list[str] | None:
    if person.is_admin:
        return None
    return sorted(
        row.id for row in rows
        if (held := level_of(person, row.access)) is not None and LEVEL_RANK[held] >= LEVEL_RANK[need]
    )


PEOPLE = {
    "me": Person(ME, "user"),
    "other": Person(OTHER, "user"),
    "stranger": Person("u-stranger", "user"),
    "no-user-id": Person(None, "user"),
    "restricted": Person(ME, "restricted"),
    "admin": Person("u-admin", "admin"),
    "system": Person(None, "system"),
}
SIZES = [0, 1, 199, 200, 201, 450]
CASES = [(kind, n) for kind in ("memory",) for n in SIZES] + [("sqlite", 1), ("sqlite", 201)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "visible"), CASES)
async def test_readable_workspace_ids_match_the_oracle_for_every_person_and_level(kind, visible, tmp_path):
    sp = await _provider(kind, tmp_path)
    await _seed(sp, visible)
    rows = await _all(sp.get_storage(Workspace))
    for name, person in PEOPLE.items():
        for need in AccessLevel:
            got = await readable_workspace_ids(sp, person, need)
            want = _oracle(rows, person, need)
            assert (None if got is None else sorted(got)) == want, (name, need, visible)
    me_read = await readable_workspace_ids(sp, PEOPLE["me"])
    assert len(me_read) == visible  # every visible row, none of the hidden ones
    await sp.aclose()


@pytest.mark.asyncio
async def test_a_revoked_grant_and_a_made_private_row_disappear_from_the_next_answer(tmp_path):
    sp = await _provider("sqlite", tmp_path)
    await _seed(sp, 12)
    me = PEOPLE["me"]
    before = set(await readable_workspace_ids(sp, me))
    store = sp.get_storage(Workspace)
    granted = await store.get("ws-v0001")  # read grant
    public = await store.get("ws-v0003")  # public
    await store.update(granted.model_copy(update={"access": Access(owners=[OTHER])}))
    await store.update(public.model_copy(update={"access": Access(owners=[OTHER], public=False)}))
    after = set(await readable_workspace_ids(sp, me))
    assert before - after == {"ws-v0001", "ws-v0003"} and after < before
    await sp.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_the_session_scope_lists_exactly_the_sessions_of_readable_workspaces(kind, tmp_path):
    sp = await _provider(kind, tmp_path)
    await _seed(sp, 30)
    sessions = sp.get_storage(WorkspaceSession)
    workspaces = await _all(sp.get_storage(Workspace))
    for i, ws in enumerate(workspaces):
        await sessions.create(WorkspaceSession(
            id=f"s-{i:03d}", workspace_id=ws.id, binding=AgentSessionBinding(agent_id="a"), status=SessionStatus.RUNNING, created_at=NOW,
        ))
    for name, person in PEOPLE.items():
        nothing, scoped = await scope_to_readable_workspaces(sp, person)
        readable = _oracle(workspaces, person, AccessLevel.READ)
        if readable is None:
            assert (nothing, scoped) == (False, None), name
            continue
        if not readable:
            assert nothing is True, name
            continue
        assert nothing is False
        got = sorted(s.id for s in (await sessions.find(scoped, OffsetPage(offset=0, length=200))).items)
        want = sorted(s.id for s in await _all(sessions) if s.workspace_id in set(readable))
        assert got == want, name
    await sp.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_collection_ids_and_the_document_scope_match_the_oracle(kind, tmp_path):
    from primer.toolset._collection_access import readable_documents

    sp = await _provider(kind, tmp_path)
    collections, documents = sp.get_storage(Collection), sp.get_storage(Document)
    for i in range(40):
        await collections.create(Collection(id=f"kb-{i:03d}", description="d", access=_access_for(i)))
        await documents.create(Document(id=f"d-{i:03d}", collection_id=f"kb-{i:03d}", slug=f"s{i}", path=f"p{i}", title="t"))
    rows = await _all(collections)
    for name, person in PEOPLE.items():
        for need in AccessLevel:
            got = await readable_ids(collections, person, need)
            assert (None if got is None else sorted(got)) == _oracle(rows, person, need), (name, need)
        predicate = await readable_documents(collections, person)
        want_docs = _oracle(rows, person, AccessLevel.READ)
        if predicate is None:
            assert want_docs is None
            continue
        found = sorted(d.collection_id for d in (await documents.find(predicate, OffsetPage(offset=0, length=200))).items) if want_docs else []
        assert found == sorted(want_docs), name
    await sp.aclose()
