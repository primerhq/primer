"""The per-user main operator session (pivot plan P2 task 6, lead ruling 3).

The default workspace's main operator session is NOT seeded at boot: each user has their own, made on demand by the
ensure route (``POST /v1/workspaces/{id}/main_session``) and created AS the caller, so it runs at exactly the caller's
rank (A-20). This file pins the function the route calls: it returns the caller's own open (status not ``ended``)
session bound to the ``operator`` agent on the workspace, or creates one as the caller when they have none. The id is
``main_session_id(workspace_id, n)`` with the smallest free ``n``: two concurrent calls pick the same id, so at most
one of them wins the create and its ``ConflictError`` re-reads the row and returns the winner's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from primer.bootstrap.defaults import RESERVED_OPERATOR_AGENT
from primer.model.agent import Agent, AgentModel
from primer.model.except_ import NotFoundError
from primer.model.principal import PrincipalRef
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.workspace.session_factory import (
    SessionFactoryDeps,
    create_session,
)


def _principal(name: str, role: str = "user") -> PrincipalRef:
    return PrincipalRef(type="user", id=f"user-{name}", display=name, role=role, source="local")


def _deps(sp):
    return SessionFactoryDeps(
        storage_provider=sp, claim_engine=None, scheduler=None, workspace_registry=None,
    )


async def _operator_agent(sp) -> None:
    if await sp.get_storage(Agent).get(RESERVED_OPERATOR_AGENT) is None:
        await sp.get_storage(Agent).create(
            Agent(
                id=RESERVED_OPERATOR_AGENT, description=RESERVED_OPERATOR_AGENT,
                model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[],
            )
        )


async def _session(
    sp,
    *,
    name: str | None = None,
    agent: str = RESERVED_OPERATOR_AGENT,
    by: PrincipalRef | None = None,
    status: SessionStatus = SessionStatus.CREATED,
    last_turn_at: datetime | None = None,
) -> WorkspaceSession:
    """An ordinary session row on prime, made exactly the way the routes make them."""
    row = await create_session(
        workspace_id="prime",
        binding=AgentSessionBinding(agent_id=agent),
        initial_instructions=None,
        graph_input=None,
        auto_start=False,
        metadata=None,
        name=name,
        session_id=f"sess-prime-t{uuid4().hex[:8]}",
        initiated_by=by,
        deps=_deps(sp),
    )
    if status is not SessionStatus.CREATED or last_turn_at is not None:
        row = row.model_copy(update={"status": status, "last_turn_at": last_turn_at})
        await sp.get_storage(WorkspaceSession).update(row)
    return row


async def _sessions(sp) -> list[WorkspaceSession]:
    page = await sp.get_storage(WorkspaceSession).find(
        Predicate(
            left=FieldRef(name="workspace_id"), op=Op.EQ, right=Value(value="prime"),
        ),
        OffsetPage(offset=0, length=100),
    )
    return list(page.items)


async def _prime_workspace(sp) -> None:
    """The default workspace row, recorded first, exactly as the ensure pass leaves it."""
    await sp.get_storage(Workspace).create(
        Workspace(
            id="prime", name="prime", template_id="local-default", provider_id="local",
            created_at=datetime(2026, 1, 1, tzinfo=UTC), phase="running",
            runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="stub-token"),
        )
    )
    await sp.set_default_workspace_id("prime")


@pytest_asyncio.fixture
async def prime_sp(fake_storage_provider):
    await _prime_workspace(fake_storage_provider)
    await _operator_agent(fake_storage_provider)
    return fake_storage_provider


@pytest_asyncio.fixture
async def sqlite_sp(tmp_path: Path) -> AsyncIterator:
    """A real SQLite provider: the concurrency test wants the backend's own ConflictError."""
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "main-session.sqlite")))
    await sp.initialize()
    try:
        yield sp
    finally:
        await sp.aclose()


# ---- the id scheme ----------------------------------------------------------------------------------------------------------------


def test_the_id_scheme() -> None:
    from primer.bootstrap.prime import MAIN_SESSION_NAME, main_session_id

    assert MAIN_SESSION_NAME == "main"
    assert main_session_id("prime", 1) == "sess-prime-main-1"
    assert main_session_id("primer", 2) == "sess-primer-main-2"


# ---- create ------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creates_the_callers_main_session(prime_sp) -> None:
    from primer.bootstrap.prime import ensure_main_operator_session

    caller = _principal("ada")

    row, created = await ensure_main_operator_session(
        prime_sp, workspace_id="prime", initiated_by=caller,
    )

    assert created is True
    assert row.id == "sess-prime-main-1"
    assert row.name == "main" and row.workspace_id == "prime"
    assert row.binding.agent_id == RESERVED_OPERATOR_AGENT
    assert row.status is SessionStatus.CREATED
    assert row.initiated_by == caller


@pytest.mark.asyncio
async def test_the_create_fails_cleanly_without_the_operator_agent(prime_sp) -> None:
    from primer.bootstrap.prime import ensure_main_operator_session

    await prime_sp.get_storage(Agent).delete(RESERVED_OPERATOR_AGENT)
    caller = _principal("ada")

    with pytest.raises(NotFoundError, match="operator"):
        await ensure_main_operator_session(prime_sp, workspace_id="prime", initiated_by=caller)

    assert await _sessions(prime_sp) == []


# ---- return the caller's own open session instead of making another ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_the_callers_open_session_and_creates_nothing(prime_sp) -> None:
    from primer.bootstrap.prime import ensure_main_operator_session

    caller = _principal("ada")
    existing = await _session(prime_sp, name="Session 1", by=caller)

    row, created = await ensure_main_operator_session(
        prime_sp, workspace_id="prime", initiated_by=caller,
    )

    assert created is False and row.id == existing.id
    rows = await _sessions(prime_sp)
    assert [r.id for r in rows] == [existing.id]


@pytest.mark.asyncio
async def test_the_most_recently_active_open_session_wins(prime_sp) -> None:
    from primer.bootstrap.prime import ensure_main_operator_session

    caller = _principal("ada")
    newer = datetime(2026, 6, 1, tzinfo=UTC)
    older = datetime(2026, 5, 1, tzinfo=UTC)
    recent = await _session(prime_sp, name="landed here", by=caller, last_turn_at=newer)
    await _session(prime_sp, name="was here first", by=caller, last_turn_at=older)

    row, created = await ensure_main_operator_session(
        prime_sp, workspace_id="prime", initiated_by=caller,
    )

    assert created is False and row.id == recent.id


@pytest.mark.asyncio
async def test_another_users_open_session_is_not_returned(prime_sp) -> None:
    from primer.bootstrap.prime import ensure_main_operator_session

    carol = _principal("carol")
    ada = _principal("ada")
    await ensure_main_operator_session(prime_sp, workspace_id="prime", initiated_by=carol)

    row, created = await ensure_main_operator_session(
        prime_sp, workspace_id="prime", initiated_by=ada,
    )

    assert created is True and row.id == "sess-prime-main-2"
    assert row.initiated_by == ada
    carol_row = await prime_sp.get_storage(WorkspaceSession).get("sess-prime-main-1")
    assert carol_row is not None and carol_row.initiated_by == carol


@pytest.mark.asyncio
async def test_a_non_operator_open_session_is_not_returned(prime_sp) -> None:
    from primer.bootstrap.prime import ensure_main_operator_session

    caller = _principal("ada")
    await _session(prime_sp, name="builder work", by=caller, agent="builder")

    row, created = await ensure_main_operator_session(
        prime_sp, workspace_id="prime", initiated_by=caller,
    )

    assert created is True and row.id == "sess-prime-main-1"
    assert row.binding.agent_id == RESERVED_OPERATOR_AGENT


# ---- an ended or deleted main is replaced, never resurrected ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_ended_main_is_replaced_with_the_next_number(prime_sp) -> None:
    from primer.bootstrap.prime import ensure_main_operator_session

    caller = _principal("ada")
    first, _ = await ensure_main_operator_session(
        prime_sp, workspace_id="prime", initiated_by=caller,
    )
    assert first.id == "sess-prime-main-1"
    await prime_sp.get_storage(WorkspaceSession).update(
        first.model_copy(update={"status": SessionStatus.ENDED})
    )

    again, created = await ensure_main_operator_session(
        prime_sp, workspace_id="prime", initiated_by=caller,
    )

    assert created is True and again.id == "sess-prime-main-2"


@pytest.mark.asyncio
async def test_a_deleted_main_reuses_its_number(prime_sp) -> None:
    from primer.bootstrap.prime import ensure_main_operator_session

    caller = _principal("ada")
    first, _ = await ensure_main_operator_session(
        prime_sp, workspace_id="prime", initiated_by=caller,
    )
    await prime_sp.get_storage(WorkspaceSession).delete(first.id)

    again, created = await ensure_main_operator_session(
        prime_sp, workspace_id="prime", initiated_by=caller,
    )

    assert created is True and again.id == "sess-prime-main-1"


# ---- concurrency -------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_creations_yield_one_main_session(sqlite_sp) -> None:
    """The S2 check: however many processes race the first ensure, there is one main session."""
    from primer.bootstrap.prime import ensure_main_operator_session

    await _prime_workspace(sqlite_sp)
    await _operator_agent(sqlite_sp)
    caller = _principal("ada")

    results = await asyncio.gather(
        *(
            ensure_main_operator_session(sqlite_sp, workspace_id="prime", initiated_by=caller)
            for _ in range(4)
        )
    )

    assert {row.id for row, _ in results} == {"sess-prime-main-1"}
    assert sum(1 for _, created in results if created) == 1
    rows = await _sessions(sqlite_sp)
    assert [r.id for r in rows] == ["sess-prime-main-1"]
    assert rows[0].initiated_by == caller


# ---- the caller is its principal type and id (lead ruling 4 of the G7 fixes, audit G7-12) ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"display": "ada lovelace"}, id="a new display name"),
        pytest.param({"source": "okta"}, id="an SSO login instead of a password"),
        pytest.param({"role": "admin"}, id="a promotion"),
    ],
)
async def test_the_same_person_keeps_their_main_session_whatever_else_changed(prime_sp, change) -> None:
    """The lookup used to compare the whole PrincipalRef, so any of these handed the same person a second main session."""
    from primer.bootstrap.prime import ensure_main_operator_session

    first, _ = await ensure_main_operator_session(prime_sp, workspace_id="prime", initiated_by=_principal("ada"))

    again, created = await ensure_main_operator_session(
        prime_sp, workspace_id="prime", initiated_by=_principal("ada").model_copy(update=change),
    )

    assert created is False and again.id == first.id
    assert [r.id for r in await _sessions(prime_sp)] == [first.id]


@pytest.mark.asyncio
async def test_an_api_token_of_the_same_person_is_its_own_principal(prime_sp) -> None:
    from primer.bootstrap.prime import ensure_main_operator_session

    first, _ = await ensure_main_operator_session(prime_sp, workspace_id="prime", initiated_by=_principal("ada"))
    token = PrincipalRef(type="api_token", id="user-ada", display="ci", role="user", source="internal")

    row, created = await ensure_main_operator_session(prime_sp, workspace_id="prime", initiated_by=token)

    assert created is True and row.id != first.id and row.initiated_by == token


@pytest.mark.asyncio
async def test_a_demoted_admin_is_not_handed_the_admin_session_they_can_no_longer_drive(prime_sp) -> None:
    """Its recorded rank is admin, so the person's own steer would be refused (lead ruling 2): they get a new main session at their rank."""
    from primer.bootstrap.prime import ensure_main_operator_session

    admin_main, _ = await ensure_main_operator_session(prime_sp, workspace_id="prime", initiated_by=_principal("ada", "admin"))

    row, created = await ensure_main_operator_session(prime_sp, workspace_id="prime", initiated_by=_principal("ada", "user"))

    assert created is True and row.id != admin_main.id and row.initiated_by.role == "user"


@pytest.mark.asyncio
async def test_the_lookup_is_one_query_filtered_in_storage(prime_sp, monkeypatch) -> None:
    """Never a scan of the workspace's sessions: the caller's session is found by one ``find`` whose predicate names the workspace, the
    principal's id, the operator binding and the status, however many other sessions the workspace holds."""
    from primer.bootstrap.prime import ensure_main_operator_session

    caller = _principal("ada")
    for i in range(250):
        await _session(prime_sp, name=f"other {i}", by=_principal(f"u{i}"))
    mine = await _session(prime_sp, name="mine", by=caller)
    finds: list = []
    sessions = prime_sp.get_storage(WorkspaceSession)
    real_find = sessions.find

    async def _find(predicate, page, **kw):
        finds.append((predicate, page))
        return await real_find(predicate, page, **kw)

    monkeypatch.setattr(sessions, "find", _find)

    row, created = await ensure_main_operator_session(prime_sp, workspace_id="prime", initiated_by=caller)

    assert created is False and row.id == mine.id
    (predicate, page), = finds
    named = predicate.model_dump_json()
    for field in ("workspace_id", "initiated_by.id", "binding.agent_id", "status"):
        assert f'"{field}"' in named, field
    assert page.offset == 0
