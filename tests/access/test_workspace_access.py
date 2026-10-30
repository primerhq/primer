"""Access on workspaces and their sessions (access-control spec 6.2, 6.7, 6.8; plan P2 Task 1).

``workspace_level`` resolves a person's level on one workspace row;
``require_workspace`` / ``require_session`` enforce a minimum level, with the
Phase-1 answers (a missing row, or a row the person holds no level on, answers
exactly like a missing one; an insufficient level answers ``AccessDeniedError``).
``readable_workspace_ids`` is the id set a person's list queries filter on
(``None`` for admins: no filter). The index pins: the ``workspace`` table
carries the derived-members GIN index like the other owned tables, and
``sessions`` and the ``events`` log get btree indexes on ``workspace_id`` so
the readable-set filter and the event scoping (Task 7) run off an index.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from primer.access.person import Person
from primer.model.access import Access, AccessLevel
from primer.model.except_ import AccessDeniedError, NotFoundError
from primer.model.provider import SqliteConfig
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.storage.postgres import _HOT_FIELD_INDEXES, _hot_field_index_ddl
from primer.storage.sqlite import SqliteStorageProvider

try:  # the production event store (its DDL declares the events indexes)
    from primer.storage.postgres import PostgresEventStore
except ImportError:  # asyncpg missing in a slim env: the pin still runs on the DDL strings
    PostgresEventStore = None  # type: ignore[assignment]

U1, U2, U3, U9 = "u1", "u2", "u3", "u9"
OWNER = Person(U1, "user")
READER = Person(U2, "user")  # full on ws-full, read on ws-read
STRANGER = Person(U3, "user")
ADMIN = Person(None, "admin")
SYSTEM = Person(None, "system")
NOBODY = Person(None, "restricted")

_WS_IDS = ("ws-owned", "ws-full", "ws-read", "ws-public")


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "ws-access.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


def _workspace(
    ws_id: str, owners: list[str] | None = None, **access: Any
) -> Workspace:
    return Workspace(
        id=ws_id,
        template_id="local-default",
        provider_id="local",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="stub-token"),
        access=Access(owners=owners or [U1], **access),
    )


async def _seed(sp: SqliteStorageProvider) -> None:
    workspaces = sp.get_storage(Workspace)
    await workspaces.create(_workspace("ws-owned"))
    await workspaces.create(_workspace("ws-full", grants={U2: "full"}))
    await workspaces.create(_workspace("ws-read", grants={U2: "read"}))
    await workspaces.create(_workspace("ws-public", public=True))
    await workspaces.create(_workspace("ws-private", owners=[U9]))
    sessions = sp.get_storage(WorkspaceSession)
    for sid, ws in (
        ("sess-owned", "ws-owned"),
        ("sess-read", "ws-read"),
        ("sess-private", "ws-private"),
    ):
        await sessions.create(
            WorkspaceSession(
                id=sid,
                workspace_id=ws,
                binding={"kind": "agent", "agent_id": "agt-1"},
                status=SessionStatus.RUNNING,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )


# ---------------------------------------------------------------------------
# workspace_level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_owner_holds_owner(sp: SqliteStorageProvider) -> None:
    from primer.access.workspaces import workspace_level

    await _seed(sp)
    assert await workspace_level(sp, OWNER, "ws-owned") is AccessLevel.OWNER


@pytest.mark.asyncio
async def test_a_grantee_holds_the_granted_level(sp: SqliteStorageProvider) -> None:
    from primer.access.workspaces import workspace_level

    await _seed(sp)
    assert await workspace_level(sp, READER, "ws-full") is AccessLevel.FULL
    assert await workspace_level(sp, READER, "ws-read") is AccessLevel.READ


@pytest.mark.asyncio
async def test_a_stranger_holds_nothing_on_a_private_row(sp: SqliteStorageProvider) -> None:
    from primer.access.workspaces import workspace_level

    await _seed(sp)
    assert await workspace_level(sp, STRANGER, "ws-owned") is None
    assert await workspace_level(sp, STRANGER, "ws-private") is None


@pytest.mark.asyncio
async def test_public_is_read_for_every_full_user_and_nothing_for_restricted(
    sp: SqliteStorageProvider,
) -> None:
    from primer.access.workspaces import workspace_level

    await _seed(sp)
    assert await workspace_level(sp, READER, "ws-public") is AccessLevel.READ
    assert await workspace_level(sp, Person(None, "user"), "ws-public") is AccessLevel.READ
    assert await workspace_level(sp, NOBODY, "ws-public") is None


@pytest.mark.asyncio
async def test_admin_and_system_are_owner_on_any_row_and_a_missing_row_is_none(
    sp: SqliteStorageProvider,
) -> None:
    from primer.access.workspaces import workspace_level

    await _seed(sp)
    assert await workspace_level(sp, ADMIN, "ws-private") is AccessLevel.OWNER
    assert await workspace_level(sp, SYSTEM, "ws-owned") is AccessLevel.OWNER
    assert await workspace_level(sp, OWNER, "no-such-workspace") is None


# ---------------------------------------------------------------------------
# require_workspace / require_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_workspace_answers_a_missing_id_with_the_missing_row_text(
    sp: SqliteStorageProvider,
) -> None:
    from primer.access.workspaces import require_workspace

    await _seed(sp)
    with pytest.raises(NotFoundError) as exc:
        await require_workspace(sp, OWNER, "no-such-workspace", AccessLevel.READ)
    assert str(exc.value) == "Workspace 'no-such-workspace' does not exist"


@pytest.mark.asyncio
async def test_require_workspace_refuses_an_unreadable_row_like_a_missing_one(
    sp: SqliteStorageProvider,
) -> None:
    from primer.access.workspaces import require_workspace

    await _seed(sp)
    with pytest.raises(NotFoundError) as stranger:
        await require_workspace(sp, STRANGER, "ws-owned", AccessLevel.READ)
    with pytest.raises(NotFoundError) as missing:
        await require_workspace(sp, STRANGER, "no-such-workspace", AccessLevel.READ)
    assert str(stranger.value) == str(missing.value) == "Workspace 'ws-owned' does not exist"


@pytest.mark.asyncio
async def test_require_workspace_refuses_a_reader_who_needs_full(sp: SqliteStorageProvider) -> None:
    from primer.access.workspaces import require_workspace

    await _seed(sp)
    with pytest.raises(AccessDeniedError) as exc:
        await require_workspace(sp, READER, "ws-read", AccessLevel.FULL)
    assert "needs full access" in str(exc.value)
    row = await require_workspace(sp, READER, "ws-full", AccessLevel.FULL)
    assert row.id == "ws-full"


@pytest.mark.asyncio
async def test_require_session_answers_a_missing_session_with_the_session_text(
    sp: SqliteStorageProvider,
) -> None:
    from primer.access.workspaces import require_session

    await _seed(sp)
    with pytest.raises(NotFoundError) as exc:
        await require_session(sp, OWNER, "no-such-session", AccessLevel.READ)
    assert str(exc.value) == "Session 'no-such-session' does not exist"


@pytest.mark.asyncio
async def test_require_session_in_an_unreadable_workspace_answers_like_a_missing_session(
    sp: SqliteStorageProvider,
) -> None:
    from primer.access.workspaces import require_session

    await _seed(sp)
    with pytest.raises(NotFoundError) as exc:
        await require_session(sp, STRANGER, "sess-owned", AccessLevel.READ)
    # The same text a missing session id answers: the workspace's access must
    # not be distinguishable from its absence.
    assert str(exc.value) == "Session 'sess-owned' does not exist"


@pytest.mark.asyncio
async def test_require_session_of_a_reader_clears_read_and_refuses_full(
    sp: SqliteStorageProvider,
) -> None:
    from primer.access.workspaces import require_session

    await _seed(sp)
    session = await require_session(sp, READER, "sess-read", AccessLevel.READ)
    assert session.id == "sess-read"
    with pytest.raises(AccessDeniedError):
        await require_session(sp, READER, "sess-read", AccessLevel.FULL)


# ---------------------------------------------------------------------------
# readable_workspace_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readable_workspace_ids_of_an_admin_is_none(sp: SqliteStorageProvider) -> None:
    from primer.access.workspaces import readable_workspace_ids

    await _seed(sp)
    assert await readable_workspace_ids(sp, ADMIN) is None
    assert await readable_workspace_ids(sp, SYSTEM, AccessLevel.FULL) is None


@pytest.mark.asyncio
async def test_readable_workspace_ids_per_person(sp: SqliteStorageProvider) -> None:
    from primer.access.workspaces import readable_workspace_ids

    await _seed(sp)
    assert sorted(await readable_workspace_ids(sp, OWNER)) == list(_WS_IDS)
    assert sorted(await readable_workspace_ids(sp, READER)) == ["ws-full", "ws-public", "ws-read"]
    assert await readable_workspace_ids(sp, STRANGER) == ["ws-public"]
    assert await readable_workspace_ids(sp, NOBODY) == []
    # full drops the read grant and the public rows (public is read-only).
    assert await readable_workspace_ids(sp, READER, AccessLevel.FULL) == ["ws-full"]
    assert await readable_workspace_ids(sp, STRANGER, AccessLevel.FULL) == []


# ---------------------------------------------------------------------------
# Index pins (pure DDL shape; the live Postgres layer follows test_access_index)
# ---------------------------------------------------------------------------


def test_the_workspace_table_carries_the_members_gin_index() -> None:
    entries = [e for e in _HOT_FIELD_INDEXES["workspace"] if e[0] == "access_members"]
    (suffix, unique, expr), = entries
    assert unique is False
    assert expr == "USING GIN ((data->'access'->'members'))"
    (stmt,) = [s for s in _hot_field_index_ddl("workspace", '"public"."workspace"') if "access_members" in s]
    assert f'"workspace_access_members"' in stmt
    assert "USING GIN" in stmt and "UNIQUE" not in stmt


def test_the_sessions_table_carries_a_workspace_id_index() -> None:
    entries = [e for e in _HOT_FIELD_INDEXES["sessions"] if e[0] == "workspace_id"]
    (suffix, unique, expr), = entries
    assert unique is False
    assert expr == "((data->>'workspace_id'))"
    (stmt,) = [s for s in _hot_field_index_ddl("sessions", '"public"."sessions"') if "data->>'workspace_id'" in s]
    assert f'"sessions_workspace_id"' in stmt
    assert "CREATE INDEX IF NOT EXISTS" in stmt and "UNIQUE" not in stmt


@pytest.mark.asyncio
async def test_the_events_table_declares_a_workspace_id_index() -> None:
    assert PostgresEventStore is not None, "asyncpg is required for the events DDL pin"

    class _RecordingConn:
        """Records every executed statement; doubles as its own async context managers."""

        def __init__(self) -> None:
            self.statements: list[str] = []

        async def execute(self, sql: str, *params: Any) -> None:
            self.statements.append(sql)

        def transaction(self) -> "_RecordingConn":
            return self

        async def __aenter__(self) -> "_RecordingConn":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _AcquireConn:
        """Pool-shaped: ``acquire()`` hands back the recording conn."""

        def __init__(self, conn: _RecordingConn) -> None:
            self._conn = conn

        def acquire(self) -> _RecordingConn:
            return self._conn

    conn = _RecordingConn()
    store = PostgresEventStore(pool=_AcquireConn(conn), schema="public")  # type: ignore[arg-type]
    await store.ensure_schema()
    (stmt,) = [s for s in conn.statements if "workspace_id" in s and "CREATE INDEX" in s]
    assert "events_workspace" in stmt
    assert "ON \"public\".events (workspace_id)" in stmt
