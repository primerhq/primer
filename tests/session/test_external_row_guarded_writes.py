"""Every ``ExternalToolCall`` status write is ONE guarded write (``resolve_external_row``).

The four writers of a row's status (``apply_tool_results``, ``cancel_pending_external``, the read surface's
``sweep_expired`` and ``flip_external_row``) used to write the whole row from a snapshot they had read, so a writer
that lost a race overwrote the winner's terminal status. Each now moves the row out of ``pending`` with one
``patch_if`` guarded on ``status == "pending"``: the first terminal status stands and a later writer's write is
rejected.

These run on a REAL SQLite storage, because the race needs a snapshot that is a copy (the in-memory fakes hand back
the stored object itself, so a writer's mutation would be visible to the other side before any write). The race
tests hold the LOSER at its write (``tests/_support/held_write.py``): it has done everything up to the write when
the winner runs, which is the interleaving a whole-row update loses. The route-level races (the yields-cancel route
and the read surface's sweep) are in ``tests/api/test_external_tools_guarded_writes.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from primer.model.except_ import NotFoundError, ServerError
from primer.model.external_tool import ExternalToolCall, ExternalToolResultIn
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.external_calls import flip_external_row
from primer.session.external_tools import apply_tool_results, cancel_pending_external
from primer.storage import PatchValueError
from primer.storage.sqlite import SqliteStorageProvider
from tests._support.held_write import hold_write
from tests.api.test_external_tools_steer import _parked_over

RESULT = {"customer": "c1"}


@pytest_asyncio.fixture
async def provider(tmp_path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "external-rows.sqlite"))
    await sp.initialize()
    yield sp
    await sp.aclose()


def _call(row_id: str, tool_call_id: str, *, session_id: str = "sess-1", **over) -> ExternalToolCall:
    fields = {
        "id": row_id,
        "session_id": session_id,
        "tool_call_id": tool_call_id,
        "tool_name": "lookup_customer",
        "arguments": {"q": tool_call_id},
        "created_at": datetime.now(UTC),
    }
    fields.update(over)
    return ExternalToolCall(**fields)


async def _parked_session(provider, sid: str = "sess-1") -> WorkspaceSession:
    """A single-event external park on ``tc-1`` whose row id is ``etool-fixed-1`` (the steer suite's shape)."""
    now = datetime.now(UTC)
    row = WorkspaceSession(
        id=sid,
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="agent-ext"),
        status=SessionStatus.RUNNING,
        created_at=now,
        started_at=now,
        **_parked_over(sid),
    )
    return await provider.get_storage(WorkspaceSession).create(row)


# ---------------------------------------------------------------------------
# resolve_external_row
# ---------------------------------------------------------------------------


async def test_resolve_external_row_lands_on_a_pending_row_and_returns_it(provider) -> None:
    from primer.session.external_calls import resolve_external_row

    calls = provider.get_storage(ExternalToolCall)
    await calls.create(_call("etool-1", "tc-1"))
    before = datetime.now(UTC)

    out = await resolve_external_row(calls, "etool-1", status="completed", result=RESULT, is_error=False)

    assert out is not None
    assert (out.status, out.result, out.is_error) == ("completed", RESULT, False)
    assert out.resolved_at is not None and out.resolved_at >= before
    stored = await calls.get("etool-1")
    assert stored == out
    # the fields the writer does not own are left as they were
    assert (stored.session_id, stored.tool_call_id, stored.arguments) == ("sess-1", "tc-1", {"q": "tc-1"})


@pytest.mark.parametrize("current", ["completed", "cancelled", "timed_out"])
async def test_resolve_external_row_returns_none_for_a_row_that_left_pending_and_writes_nothing(
    provider, current: str,
) -> None:
    from primer.session.external_calls import resolve_external_row

    calls = provider.get_storage(ExternalToolCall)
    resolved_at = datetime.now(UTC) - timedelta(minutes=5)
    await calls.create(_call(
        "etool-1", "tc-1", status=current, result={"first": True}, is_error=False, resolved_at=resolved_at,
    ))
    before = await calls.get("etool-1")

    out = await resolve_external_row(
        calls, "etool-1", status="cancelled", result={"cancelled": True, "reason": "late"}, is_error=True,
    )

    assert out is None
    assert await calls.get("etool-1") == before


async def test_resolve_external_row_raises_for_a_row_that_does_not_exist(provider) -> None:
    from primer.session.external_calls import resolve_external_row

    calls = provider.get_storage(ExternalToolCall)
    with pytest.raises(NotFoundError):
        await resolve_external_row(calls, "etool-missing", status="completed", result=RESULT, is_error=False)


async def test_resolve_external_row_raises_on_a_storage_error(provider, monkeypatch) -> None:
    from primer.session.external_calls import resolve_external_row

    calls = provider.get_storage(ExternalToolCall)
    await calls.create(_call("etool-1", "tc-1"))

    async def _down(*_a, **_kw):
        raise ServerError("database is down")

    monkeypatch.setattr(calls, "patch_if", _down)
    with pytest.raises(ServerError):
        await resolve_external_row(calls, "etool-1", status="completed", result=RESULT, is_error=False)


async def test_resolve_external_row_raises_on_a_result_that_cannot_be_stored_and_writes_nothing(provider) -> None:
    from primer.session.external_calls import resolve_external_row

    calls = provider.get_storage(ExternalToolCall)
    await calls.create(_call("etool-1", "tc-1"))

    with pytest.raises(PatchValueError):
        await resolve_external_row(
            calls, "etool-1", status="completed", result={"score": float("nan")}, is_error=False,
        )

    assert (await calls.get("etool-1")).status == "pending"


# ---------------------------------------------------------------------------
# cancel_pending_external: the filters and the landed ids
# ---------------------------------------------------------------------------


async def test_cancel_pending_external_returns_the_tool_call_ids_it_cancelled(provider) -> None:
    calls = provider.get_storage(ExternalToolCall)
    await calls.create(_call("etool-a", "tc-a"))
    await calls.create(_call("etool-b", "tc-b"))
    await calls.create(_call("etool-c", "tc-c", status="completed", result=RESULT))
    await calls.create(_call("etool-x", "tc-x", session_id="sess-2"))

    landed = await cancel_pending_external(call_storage=calls, session_id="sess-1", reason="session cancelled")

    assert sorted(landed) == ["tc-a", "tc-b"]
    for row_id in ("etool-a", "etool-b"):
        row = await calls.get(row_id)
        assert row.status == "cancelled"
        assert row.result == {"cancelled": True, "reason": "session cancelled"}
        assert row.is_error is True and row.resolved_at is not None
    # a terminal row and another session's row are left alone
    assert (await calls.get("etool-c")).status == "completed"
    assert (await calls.get("etool-c")).result == RESULT
    assert (await calls.get("etool-x")).status == "pending"


async def test_cancel_pending_external_with_nothing_pending_returns_no_ids(provider) -> None:
    calls = provider.get_storage(ExternalToolCall)
    await calls.create(_call("etool-c", "tc-c", status="completed", result=RESULT))

    assert await cancel_pending_external(call_storage=calls, session_id="sess-1") == []


async def test_cancel_pending_external_cancels_only_the_named_tool_call_ids(provider) -> None:
    calls = provider.get_storage(ExternalToolCall)
    for tcid in ("tc-a", "tc-b", "tc-c"):
        await calls.create(_call(f"etool-{tcid}", tcid))

    landed = await cancel_pending_external(
        call_storage=calls, session_id="sess-1", tool_call_ids=["tc-a", "tc-c", "tc-unknown"],
    )

    assert sorted(landed) == ["tc-a", "tc-c"]
    assert (await calls.get("etool-tc-a")).status == "cancelled"
    assert (await calls.get("etool-tc-b")).status == "pending"
    assert (await calls.get("etool-tc-c")).status == "cancelled"


async def test_cancel_pending_external_with_an_empty_id_list_cancels_nothing(provider) -> None:
    calls = provider.get_storage(ExternalToolCall)
    await calls.create(_call("etool-a", "tc-a"))

    assert await cancel_pending_external(call_storage=calls, session_id="sess-1", tool_call_ids=[]) == []
    assert (await calls.get("etool-a")).status == "pending"


async def test_cancel_pending_external_created_before_spares_later_rows_and_rows_without_created_at(
    provider,
) -> None:
    calls = provider.get_storage(ExternalToolCall)
    t1 = datetime.now(UTC)
    await calls.create(_call("etool-old", "tc-old", created_at=t1 - timedelta(seconds=30)))
    await calls.create(_call("etool-same", "tc-same", created_at=t1))
    await calls.create(_call("etool-new", "tc-new", created_at=t1 + timedelta(seconds=30)))
    await calls.create(_call("etool-none", "tc-none", created_at=None))

    landed = await cancel_pending_external(call_storage=calls, session_id="sess-1", created_before=t1)

    assert landed == ["tc-old"]
    assert (await calls.get("etool-old")).status == "cancelled"
    # not earlier than t1, or no created_at at all: spared
    for row_id in ("etool-same", "etool-new", "etool-none"):
        assert (await calls.get(row_id)).status == "pending"


async def test_cancel_pending_external_applies_both_filters_together(provider) -> None:
    calls = provider.get_storage(ExternalToolCall)
    t1 = datetime.now(UTC)
    await calls.create(_call("etool-a", "tc-a", created_at=t1 - timedelta(seconds=30)))
    await calls.create(_call("etool-b", "tc-b", created_at=t1 - timedelta(seconds=30)))
    await calls.create(_call("etool-c", "tc-c", created_at=t1 + timedelta(seconds=30)))

    landed = await cancel_pending_external(
        call_storage=calls, session_id="sess-1", tool_call_ids=["tc-a", "tc-c"], created_before=t1,
    )

    assert landed == ["tc-a"]
    assert [(await calls.get(r)).status for r in ("etool-a", "etool-b", "etool-c")] == [
        "cancelled", "pending", "pending",
    ]


# ---------------------------------------------------------------------------
# The races, each loser held AT its write
# ---------------------------------------------------------------------------


async def test_cancel_pending_external_held_at_its_write_while_a_result_completes_leaves_completed(
    provider, monkeypatch,
) -> None:
    """N53b: a cancel that read the row ``pending`` and is held at its ``cancelled`` write while a result completes
    the call. The result's ``completed`` stands, and the cancel does not report that id as cancelled."""
    sessions = provider.get_storage(WorkspaceSession)
    calls = provider.get_storage(ExternalToolCall)
    await _parked_session(provider)
    await calls.create(_call("etool-fixed-1", "tc-1"))
    await calls.create(_call("etool-2", "tc-2"))
    held = hold_write(monkeypatch, calls, row_id="etool-fixed-1", status="cancelled")

    canceller = asyncio.create_task(cancel_pending_external(call_storage=calls, session_id="sess-1"))
    await held.wait_arrived()
    applied = await apply_tool_results(
        await sessions.get("sess-1"),
        [ExternalToolResultIn(tool_call_id="tc-1", result=RESULT)],
        call_storage=calls, session_storage=sessions, engine=None, event_bus=None,
    )
    assert applied == 1
    await held.release()
    landed = await canceller

    row = await calls.get("etool-fixed-1")
    assert (row.status, row.result, row.is_error) == ("completed", RESULT, False)
    assert (await calls.get("etool-2")).status == "cancelled"
    assert landed == ["tc-2"]


async def test_apply_tool_results_held_at_its_row_write_does_not_overwrite_a_row_that_left_pending(
    provider, monkeypatch,
) -> None:
    """``apply_tool_results``' row write is the guarded helper: held at its ``completed`` write while a cancel of
    the same call lands, it leaves the cancel's status and result on the row. The park took the result before the
    row write (the wake comes first), so the call still counts as applied; reconciling that row with what the park
    carries is the result route's d6 work, not this write's."""
    sessions = provider.get_storage(WorkspaceSession)
    calls = provider.get_storage(ExternalToolCall)
    await _parked_session(provider)
    await calls.create(_call("etool-fixed-1", "tc-1"))
    held = hold_write(monkeypatch, calls, row_id="etool-fixed-1", status="completed")

    applier = asyncio.create_task(apply_tool_results(
        await sessions.get("sess-1"),
        [ExternalToolResultIn(tool_call_id="tc-1", result=RESULT)],
        call_storage=calls, session_storage=sessions, engine=None, event_bus=None,
    ))
    await held.wait_arrived()
    cancelled = await cancel_pending_external(call_storage=calls, session_id="sess-1", reason="operator")
    await held.release()
    applied = await applier

    row = await calls.get("etool-fixed-1")
    assert row.status == "cancelled"
    assert row.result == {"cancelled": True, "reason": "operator"}
    assert row.is_error is True
    assert applied == 1
    assert cancelled == ["tc-1"]
    park = (await sessions.get("sess-1")).parked_state
    assert park["resume_event_payload"] == {"result": RESULT, "is_error": False}


# ---------------------------------------------------------------------------
# flip_external_row: best effort around the helper
# ---------------------------------------------------------------------------


async def test_flip_external_row_resolves_a_pending_row(provider) -> None:
    calls = provider.get_storage(ExternalToolCall)
    await calls.create(_call("etool-1", "tc-1"))

    await flip_external_row(
        calls, row_id="etool-1", status="cancelled", result={"cancelled": True, "reason": "operator"},
    )

    row = await calls.get("etool-1")
    assert (row.status, row.result, row.is_error) == ("cancelled", {"cancelled": True, "reason": "operator"}, True)
    assert row.resolved_at is not None


async def test_flip_external_row_leaves_a_terminal_row_alone(provider) -> None:
    calls = provider.get_storage(ExternalToolCall)
    await calls.create(_call("etool-1", "tc-1", status="completed", result=RESULT))
    before = await calls.get("etool-1")

    await flip_external_row(calls, row_id="etool-1", status="cancelled", result={"cancelled": True})

    assert await calls.get("etool-1") == before


async def test_flip_external_row_swallows_a_missing_row_and_a_storage_error(provider, monkeypatch, caplog) -> None:
    calls = provider.get_storage(ExternalToolCall)
    # no row id, and a row that does not exist: nothing to resolve, nothing raised, and nothing logged as a failure
    with caplog.at_level("ERROR", logger="primer.session.external_calls"):
        await flip_external_row(calls, row_id=None, status="cancelled", result={"cancelled": True})
        await flip_external_row(calls, row_id="etool-missing", status="cancelled", result={"cancelled": True})
    assert [r for r in caplog.records if r.name == "primer.session.external_calls"] == []

    await calls.create(_call("etool-1", "tc-1"))

    async def _down(*_a, **_kw):
        raise ServerError("database is down")

    monkeypatch.setattr(calls, "patch_if", _down)
    monkeypatch.setattr(calls, "update", _down)
    with caplog.at_level("ERROR", logger="primer.session.external_calls"):
        await flip_external_row(calls, row_id="etool-1", status="cancelled", result={"cancelled": True})
    assert "external tool call row flip failed" in caplog.text
