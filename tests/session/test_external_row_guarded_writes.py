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
from pydantic_core import PydanticSerializationError

from primer.model.except_ import ConflictError, NotFoundError, ServerError
from primer.model.external_tool import ExternalToolCall, ExternalToolResultIn
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import AgentSessionBinding, GraphSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import WAKE_ENTRY_KEY, WAKE_PARK_KEY
from primer.session.external_calls import flip_external_row
from primer.session.external_tools import apply_tool_results, cancel_pending_external
from primer.storage import PatchValueError
from primer.storage.sqlite import SqliteStorageProvider
from tests._support.held_write import hold_write
from tests.api.test_external_tools_graph import _graph_parked_over
from tests.api.test_external_tools_steer import _parked_over
from tests.session.test_machine_wake_fences import _graph_park

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


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (object(), PydanticSerializationError),
        (b"\xff\xfe", UnicodeDecodeError),
        ("\ud800", PatchValueError),
    ],
    ids=["an-arbitrary-object", "bytes-that-are-not-utf8", "a-lone-surrogate"],
)
async def test_resolve_external_row_raises_on_a_result_that_cannot_be_stored_and_writes_nothing(
    provider, result, error,
) -> None:
    """A value no JSON document can hold raises before the row is written: the encoding refuses the first two, the
    storage's value check refuses the third (a string that is not valid Unicode)."""
    from primer.session.external_calls import resolve_external_row

    calls = provider.get_storage(ExternalToolCall)
    await calls.create(_call("etool-1", "tc-1"))
    before = await calls.get("etool-1")

    with pytest.raises(error):
        await resolve_external_row(calls, "etool-1", status="completed", result={"v": result}, is_error=False)

    assert await calls.get("etool-1") == before


@pytest.mark.parametrize(
    ("result", "stored"),
    [
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        ({"score": float("nan"), "xs": [1.5, float("inf"), -float("inf")]}, {"score": None, "xs": [1.5, None, None]}),
    ],
    ids=["nan", "infinity", "minus-infinity", "nested"],
)
async def test_resolve_external_row_stores_a_non_finite_number_as_null(provider, result, stored) -> None:
    """NaN and the infinities are not JSON, and a whole-row write stores them as null (``dump_for_storage``), which
    is also what the park receives for the same result. The guarded write stores the same null instead of refusing
    the row, so a result carrying one still completes its call."""
    from primer.session.external_calls import resolve_external_row

    calls = provider.get_storage(ExternalToolCall)
    await calls.create(_call("etool-1", "tc-1"))

    out = await resolve_external_row(calls, "etool-1", status="completed", result=result, is_error=False)

    assert out is not None and out.result == stored
    row = await calls.get("etool-1")
    assert (row.status, row.result, row.is_error) == ("completed", stored, False)


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


async def test_cancel_pending_external_refuses_a_naive_created_before_before_any_write(provider) -> None:
    calls = provider.get_storage(ExternalToolCall)
    await calls.create(_call("etool-a", "tc-a", created_at=datetime.now(UTC) - timedelta(seconds=30)))

    with pytest.raises(ValueError, match="timezone"):
        await cancel_pending_external(
            call_storage=calls, session_id="sess-1", created_before=datetime.now(UTC).replace(tzinfo=None),
        )

    assert (await calls.get("etool-a")).status == "pending"


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
    session = await sessions.get("sess-1")
    assert session.parked_state["resume_event_payload"] == {"result": RESULT, "is_error": False, WAKE_PARK_KEY: session.parked_at.isoformat(), WAKE_ENTRY_KEY: "etool-fixed-1"}


async def test_apply_tool_results_records_an_error_result_as_completed_with_is_error(provider) -> None:
    """An invoker's error result (``is_error: true``) completes the call like any result: the row is ``completed``
    and carries ``is_error`` True, and the park receives the same flag."""
    sessions = provider.get_storage(WorkspaceSession)
    calls = provider.get_storage(ExternalToolCall)
    await _parked_session(provider)
    await calls.create(_call("etool-fixed-1", "tc-1"))
    error = {"error": "customer lookup failed"}

    applied = await apply_tool_results(
        await sessions.get("sess-1"),
        [ExternalToolResultIn(tool_call_id="tc-1", result=error, is_error=True)],
        call_storage=calls, session_storage=sessions, engine=None, event_bus=None,
    )

    assert applied == 1
    row = await calls.get("etool-fixed-1")
    assert (row.status, row.result, row.is_error) == ("completed", error, True)
    session = await sessions.get("sess-1")
    assert session.parked_state["resume_event_payload"] == {"result": error, "is_error": True, WAKE_PARK_KEY: session.parked_at.isoformat(), WAKE_ENTRY_KEY: "etool-fixed-1"}


# ---------------------------------------------------------------------------
# apply_tool_results: the leaf and the row each result reaches (#707 review, round 2)
# ---------------------------------------------------------------------------


async def test_apply_tool_results_keeps_the_leaf_of_every_result_for_one_graph_park(provider) -> None:
    """N2 (probe C5). One call carries the results of BOTH external calls of a graph park. Each result is its own ``resume_event_payloads`` leaf, and
    the flip writes ``parked_state`` whole, so the second wake must start from the row the first one wrote: from one snapshot it dropped the first
    leaf while BOTH rows say ``completed`` (that call cannot be answered again, and its node waits for its deadline)."""
    sessions = provider.get_storage(WorkspaceSession)
    calls = provider.get_storage(ExternalToolCall)
    now = datetime.now(UTC)
    await sessions.create(WorkspaceSession(
        id="sess-1", workspace_id="ws-1", binding=GraphSessionBinding(graph_id="gr-ext"), status=SessionStatus.RUNNING, created_at=now,
        started_at=now, **_graph_parked_over("sess-1"),
    ))
    await calls.create(_call("etool-g1", "tc-g1", node_id="n1"))
    await calls.create(_call("etool-g2", "tc-g2", node_id="n2"))

    applied = await apply_tool_results(
        await sessions.get("sess-1"),
        [ExternalToolResultIn(tool_call_id="tc-g1", result="r1"), ExternalToolResultIn(tool_call_id="tc-g2", result="r2")],
        call_storage=calls, session_storage=sessions, engine=None, event_bus=None,
    )

    assert applied == 2
    park = (await sessions.get("sess-1")).parked_state
    leaves = {e["event_key"]: e["payload"]["result"] for e in park["resume_event_payloads"].values()}
    assert leaves == {"external_tool:sess-1:tc-g1": "r1", "external_tool:sess-1:tc-g2": "r2"}, "the first result's leaf was dropped by the second wake"
    assert [(await calls.get(row_id)).status for row_id in ("etool-g1", "etool-g2")] == ["completed", "completed"]


async def test_apply_tool_results_names_and_completes_the_call_row_the_pending_entry_names(provider) -> None:
    """N7. Two PENDING rows share the raw id ``tc-1``: the one the park waits on (its entry names ``etool-fixed-1``) and a stale one (a call whose park
    hit the yield cap with no timeout, so nothing resolved its row). The result answers the ENTRY: the wake names the entry's row, so the entry fence
    admits it, and that row is the one completed. Naming the pending row the lookup by raw id kept had the fence refuse the result, and completed the
    stale row with it."""
    from primer.session.external_tools import _rows_by_tcid

    sessions = provider.get_storage(WorkspaceSession)
    calls = provider.get_storage(ExternalToolCall)
    await _parked_session(provider)
    await calls.create(_call("etool-fixed-1", "tc-1"))
    await calls.create(_call("etool-stale", "tc-1", created_at=datetime.now(UTC) - timedelta(hours=2)))
    assert (await _rows_by_tcid(calls, session_id="sess-1"))["tc-1"].id == "etool-stale", "precondition: the lookup by raw id keeps the stale row"

    applied = await apply_tool_results(
        await sessions.get("sess-1"),
        [ExternalToolResultIn(tool_call_id="tc-1", result=RESULT)],
        call_storage=calls, session_storage=sessions, engine=None, event_bus=None,
    )

    assert applied == 1
    session = await sessions.get("sess-1")
    assert session.parked_status == "resumable", "the result was refused: it named a call row the park does not wait on"
    assert session.parked_state["resume_event_payload"][WAKE_ENTRY_KEY] == "etool-fixed-1"
    assert ((await calls.get("etool-fixed-1")).status, (await calls.get("etool-stale")).status) == ("completed", "pending")


# ---------------------------------------------------------------------------
# apply_tool_results: round 3 of the #707 review
# ---------------------------------------------------------------------------


def _external_wait(node: str, tcid: str, key: str, row_id: str) -> dict:
    """A graph agent node's pending external call, its entry naming its row."""
    return {"node_id": node, "tool_call_id": tcid, "event_key": key, "tool_name": "_external", "resume_metadata": {"external_call_row_id": row_id}}


def _leaves(row: WorkspaceSession) -> dict:
    return {e["event_key"]: e["payload"] for e in ((row.parked_state or {}).get("resume_event_payloads") or {}).values()}


class _RecordingBus:
    """Records every publish and delivers none."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict | None = None) -> None:
        self.published.append((key, dict(payload or {})))


def _after_the_first_row_write(monkeypatch, action) -> None:
    """Run ``action`` once, right after ``apply_tool_results`` wrote its FIRST call row: after the first wake, before the second re-read."""
    import primer.session.external_tools as external_tools

    real = external_tools.resolve_external_row
    writes = []

    async def resolve_then_act(storage, row_id, **kwargs):
        out = await real(storage, row_id, **kwargs)
        writes.append(row_id)
        if len(writes) == 1:
            await action()
        return out

    monkeypatch.setattr(external_tools, "resolve_external_row", resolve_then_act)


async def test_apply_tool_results_keeps_three_leaves_and_each_wake_names_the_park_it_read_and_its_row(provider) -> None:
    """N3 (probe N2a). THREE results for one graph park: each further wake re-reads the row, so the third keeps the second's leaf as the second kept
    the first's; every leaf names the park the producer read and its own call row."""
    sessions = provider.get_storage(WorkspaceSession)
    calls = provider.get_storage(ExternalToolCall)
    parked_at = datetime.now(UTC) - timedelta(minutes=1)
    waits = tuple(_external_wait(f"n{i}", f"tc-{i}", f"external_tool:gs:tc-{i}", f"etool-{i}") for i in (1, 2, 3))
    await sessions.create(_graph_park("gs", parked_at=parked_at, agent_yields=waits))
    for i in (1, 2, 3):
        await calls.create(_call(f"etool-{i}", f"tc-{i}", session_id="gs", node_id=f"n{i}"))

    applied = await apply_tool_results(
        await sessions.get("gs"), [ExternalToolResultIn(tool_call_id=f"tc-{i}", result=f"r{i}") for i in (1, 2, 3)],
        call_storage=calls, session_storage=sessions, engine=None, event_bus=None,
    )

    assert applied == 3
    leaves = _leaves(await sessions.get("gs"))
    assert {key: payload["result"] for key, payload in leaves.items()} == {f"external_tool:gs:tc-{i}": f"r{i}" for i in (1, 2, 3)}
    assert {key: (payload[WAKE_PARK_KEY], payload[WAKE_ENTRY_KEY]) for key, payload in leaves.items()} == {
        f"external_tool:gs:tc-{i}": (parked_at.isoformat(), f"etool-{i}") for i in (1, 2, 3)
    }
    assert [(await calls.get(f"etool-{i}")).status for i in (1, 2, 3)] == ["completed"] * 3


async def test_apply_tool_results_does_not_wake_a_park_that_no_longer_waits_on_the_call_after_the_re_read(provider, monkeypatch) -> None:
    """N1 (probe N2g). Between the first wake and the second, the second call was resolved elsewhere (a concurrent steer cancelled it) and the graph
    re-parked on ANOTHER entry that shares its raw id under another key (n3's ask_user under ``call_0``). The re-read park does not wait on the second
    call any more, so its wake (and publish) is skipped: landing on that park would have the resume answer n3's question with the external result (a
    key that names no pending entry selects by the raw id)."""
    sessions = provider.get_storage(WorkspaceSession)
    calls = provider.get_storage(ExternalToolCall)
    first, second = _external_wait("n1", "tc-1", "external_tool:gs:tc-1", "etool-1"), _external_wait("n2", "call_0", "external_tool:gs:call_0", "etool-2")
    await sessions.create(_graph_park("gs", parked_at=datetime.now(UTC) - timedelta(minutes=1), agent_yields=(first, second)))
    await calls.create(_call("etool-1", "tc-1", session_id="gs", node_id="n1"))
    await calls.create(_call("etool-2", "call_0", session_id="gs", node_id="n2"))
    question = {"node_id": "n3", "tool_call_id": "call_0", "event_key": "ask_user:gs:n3:call_0", "tool_name": "ask_user",
                "resume_metadata": {"prompt": "approve the wire transfer?", "gate_id": "c" * 32}}
    new_park = _graph_park("gs", parked_at=datetime.now(UTC), agent_yields=(question,))

    async def resolved_elsewhere_then_reparked() -> None:
        assert await calls.patch_if("etool-2", {"status": "cancelled"}, where={"status": ["pending"]}) is not None
        await sessions.update(new_park)

    _after_the_first_row_write(monkeypatch, resolved_elsewhere_then_reparked)
    bus = _RecordingBus()

    await apply_tool_results(
        await sessions.get("gs"),
        [ExternalToolResultIn(tool_call_id="tc-1", result="r1"), ExternalToolResultIn(tool_call_id="call_0", result={"wire": "denied"})],
        call_storage=calls, session_storage=sessions, engine=None, event_bus=bus,
    )

    after = await sessions.get("gs")
    assert after.parked_status == "parked", "a wake for a call the re-read park does not wait on flipped it"
    assert after.parked_state == new_park.parked_state, "the new park is untouched"
    assert [key for key, _payload in bus.published] == ["external_tool:gs:tc-1"], "and nothing was published for the skipped wake"
    assert (await calls.get("etool-2")).status == "cancelled"


async def test_apply_tool_results_refuses_a_result_whose_entry_names_a_row_that_is_no_longer_pending(provider) -> None:
    """N2 (probe N7b). The entry's own row was already CANCELLED (the yields cancel route wrote it; its bus copy has not flipped the park yet) and a
    stale PENDING row shares the raw id. The validation is by the row the entry names: the call is already resolved, so the request is refused
    with nothing applied, as the docstring says (409), instead of delivering a result for a cancelled call."""
    sessions = provider.get_storage(WorkspaceSession)
    calls = provider.get_storage(ExternalToolCall)
    await _parked_session(provider)
    await calls.create(_call("etool-fixed-1", "tc-1", status="cancelled", result={"cancelled": True}, is_error=True, resolved_at=datetime.now(UTC)))
    await calls.create(_call("etool-stale", "tc-1", created_at=datetime.now(UTC) - timedelta(hours=2)))

    with pytest.raises(ConflictError):
        await apply_tool_results(
            await sessions.get("sess-1"), [ExternalToolResultIn(tool_call_id="tc-1", result="late")],
            call_storage=calls, session_storage=sessions, engine=None, event_bus=None,
        )

    session = await sessions.get("sess-1")
    assert session.parked_status == "parked" and not session.parked_state.get("resume_event_payload")
    assert ((await calls.get("etool-fixed-1")).status, (await calls.get("etool-stale")).status) == ("cancelled", "pending")


async def test_apply_tool_results_completes_the_rows_the_entries_of_both_graph_lists_name(provider) -> None:
    """N3 (probe N7c). A graph park waits on a ToolCall node's external call (``pending_toolcalls``) and an agent node's (``pending_agent_yields``); two
    pending rows share each raw id and the lookup by raw id keeps the STALE one of each. Each result names and completes the row its entry names."""
    from primer.session.external_tools import _rows_by_tcid

    sessions = provider.get_storage(WorkspaceSession)
    calls = provider.get_storage(ExternalToolCall)
    agent_key, tool_key = "external_tool:gs:call_0", "external_tool:gs:uuid-9"
    tool_node = {"node_id": "nt", "tool_call_id": "uuid-9", "parked_event_key": tool_key, "arguments": {}, "tool_name": "_external",
                 "resume_metadata": {"external_call_row_id": "etool-t"}}
    await sessions.create(_graph_park(
        "gs", parked_at=datetime.now(UTC) - timedelta(minutes=1), toolcalls=(tool_node,),
        agent_yields=(_external_wait("na", "call_0", agent_key, "etool-a"),),
    ))
    for row_id, tcid, age in (("etool-a", "call_0", 0), ("etool-a-stale", "call_0", 2), ("etool-t", "uuid-9", 0), ("etool-t-stale", "uuid-9", 2)):
        await calls.create(_call(row_id, tcid, session_id="gs", created_at=datetime.now(UTC) - timedelta(hours=age)))
    kept = await _rows_by_tcid(calls, session_id="gs")
    assert {tcid: row.id for tcid, row in kept.items()} == {"call_0": "etool-a-stale", "uuid-9": "etool-t-stale"}, "precondition"

    await apply_tool_results(
        await sessions.get("gs"), [ExternalToolResultIn(tool_call_id="call_0", result="ra"), ExternalToolResultIn(tool_call_id="uuid-9", result="rt")],
        call_storage=calls, session_storage=sessions, engine=None, event_bus=None,
    )

    leaves = _leaves(await sessions.get("gs"))
    assert {key: (payload["result"], payload[WAKE_ENTRY_KEY]) for key, payload in leaves.items()} == {
        agent_key: ("ra", "etool-a"), tool_key: ("rt", "etool-t"),
    }
    statuses = {row_id: (await calls.get(row_id)).status for row_id in ("etool-a", "etool-a-stale", "etool-t", "etool-t-stale")}
    assert statuses == {"etool-a": "completed", "etool-a-stale": "pending", "etool-t": "completed", "etool-t-stale": "pending"}


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
