"""A retryable model error leaves an interactive session RESTING instead of ending it (C-024 slice 2, ticket 01a11d23-705d).

After the llm layer's own retries are exhausted, a transport failure of the model call (5xx, a 429, a dropped connection, a stall, a generation that
ran out its total budget) used to END the session ``ended / failed``; a Retry or a new message reopened it as a fresh invocation. The lead's ruling
(2026-10-09, Option B): an INTERACTIVE session whose turn failed with one of those codes rests (``WAITING``, no ``ended_reason``), the next send
continues the same invocation, and the row still says the turn failed through ``last_turn_error`` (slice 1). Resting never retries by itself: the pool
re-arms only a ``claimable`` row, and the failed turn left it ``idle``. Everything else keeps ending: a rejection the operator has to fix
(``auth_error``, ``bad_request``, ``model_not_found``, ``unsupported_content``, ``context_overflow_unrecoverable``), a code nobody classified, a crash
that is not a model error, and every autonomous, graph or trigger session (a resting one-shot would wedge a ``parallelism="skip"`` gate).

``session.turn_failed`` is announced on EVERY failed turn, ended or resting (``session.ended`` is only for an ended one).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import primer.session.dispatch as dispatch
from primer.model.chat import Done, Error, TextDelta, TurnStreamFailure
from primer.model.workspace_session import SessionMessageKind, SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.enqueue import SessionWakeDeps, wake_session
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeExecutor,
    FakeWorkspaceIO,
    _make_lease,
    _seed_session,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
)

REST_CODES = ["server_error", "rate_limit", "network_error", "connect_timeout", "stream_timeout", "generation_timeout"]
#: A stream that died WITHOUT a code is most often a broken connection (lead ruling 2026-10-09): it rests like the transport set. A code that IS
#: set but that nobody classified ends the session, and so does everything the operator has to fix.
NO_CODE = "llm_stream_error"
END_CODES = [
    "auth_error", "bad_request", "model_not_found", "unsupported_content", "context_overflow_unrecoverable", "a_code_nobody_classified",
]


def _failure(code: str | None) -> TurnStreamFailure:
    return TurnStreamFailure(Error(code=code, message="boom", fatal=True), partial_messages=[], rounds_completed=0)


class _Emitted:
    """Stands in for the event recorder: keeps (type, payload) of everything the turn announces."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, name: str, **kwargs: Any) -> None:
        self.events.append((name, kwargs.get("payload") or {}))

    def types(self) -> list[str]:
        return [name for name, _ in self.events]


async def _run(storage_provider, io, bus, monkeypatch, session, events, *, last_done_reason: str | None = None) -> tuple[Any, _Emitted]:
    emitted = _Emitted()
    monkeypatch.setattr(dispatch, "_event_recorder", lambda deps: emitted)

    async def build(_session: WorkspaceSession):
        executor = FakeExecutor(events)
        if last_done_reason is not None:
            executor.last_done_reason = last_done_reason     # what a real executor reports; a clean "stop" rests an interactive session
        return executor

    deps = SessionDispatchDeps(storage_provider=storage_provider, workspace_io=io, event_bus=bus, build_executor=build)
    outcome = await run_one_session_turn(_make_lease(session.id), deps)
    return outcome, emitted


async def _row(storage_provider, sid: str) -> WorkspaceSession:
    row = await storage_provider.get_storage(WorkspaceSession).get(sid)
    assert row is not None
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("code", REST_CODES)
async def test_a_transport_failure_of_an_interactive_session_leaves_it_resting(
    code, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    session = await _seed_session(fake_storage_provider, "s-rest")

    outcome, emitted = await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [_failure(code)])

    row = await _row(fake_storage_provider, "s-rest")
    assert (row.status, row.ended_reason, row.ended_detail, row.ended_at) == (SessionStatus.WAITING, None, None, None)
    assert row.last_turn_error is not None and row.last_turn_error.code == code
    assert row.turn_status == "idle", "nothing re-arms it: the pool re-arms only a claimable row"
    assert outcome.success is False and outcome.drop_lease is True, "the release is a failed one, as before"
    assert emitted.types().count("session.turn_failed") == 1 and "session.ended" not in emitted.types()


def _raised(code: str, cls: str = "ServerError") -> Exception:
    """What an adapter raises BEFORE a stream opens (an upstream 500 after the retries, a 429, a refused connection): the classified error itself."""
    from primer.model import except_

    return getattr(except_, cls)("the provider said no", code=code)


@pytest.mark.asyncio
@pytest.mark.parametrize("cls, code", [
    ("ServerError", "server_error"), ("RateLimitError", "rate_limit"), ("NetworkError", "network_error"),
    ("ProviderTimeoutError", "stream_timeout"), ("ProviderTimeoutError", "generation_timeout"), ("ProviderTimeoutError", "connect_timeout"),
])
async def test_an_error_the_adapter_raised_before_the_stream_opened_rests_the_session_too(
    cls, code, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    """The common shape of an upstream 5xx: the OpenAI-compatible client raises, the retries are spent, and the classified error reaches dispatch as
    the exception itself (not wrapped in a TurnStreamFailure)."""
    session = await _seed_session(fake_storage_provider, "s-raised")

    _, emitted = await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [_raised(code, cls)])

    row = await _row(fake_storage_provider, "s-raised")
    assert (row.status, row.ended_reason, row.ended_detail) == (SessionStatus.WAITING, None, None)
    assert row.last_turn_error is not None and row.last_turn_error.code == code
    assert [p for n, p in emitted.events if n == "session.turn_failed"] == [{"code": code, "ended": False}]


@pytest.mark.asyncio
@pytest.mark.parametrize("cls, code", [
    ("AuthenticationError", "auth_error"), ("BadRequestError", "bad_request"), ("ProviderError", "weird"),
])
async def test_a_raised_rejection_or_an_error_with_no_usable_code_still_ends_the_session(
    cls, code, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    from primer.model import except_

    session = await _seed_session(fake_storage_provider, "s-raised-end")
    error = getattr(except_, cls)("the provider said no", code=code)

    await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [error])

    row = await _row(fake_storage_provider, "s-raised-end")
    assert (row.status, row.ended_reason, row.ended_detail) == (SessionStatus.ENDED, "failed", None), (
        "a raised error never wrote ended_detail, and it still does not"
    )
    assert row.last_turn_error is not None and row.last_turn_error.code == (code or "turn_failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("code", END_CODES)
async def test_a_rejection_or_an_unknown_code_still_ends_the_session(
    code, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    session = await _seed_session(fake_storage_provider, "s-end")

    _, emitted = await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [_failure(code)])

    row = await _row(fake_storage_provider, "s-end")
    assert (row.status, row.ended_reason, row.ended_detail) == (SessionStatus.ENDED, "failed", code)
    assert row.last_turn_error is not None and row.last_turn_error.code == code
    assert "session.ended" in emitted.types() and emitted.types().count("session.turn_failed") == 1


@pytest.mark.asyncio
async def test_a_stream_that_died_without_a_code_rests_like_the_transport_set(
    fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    session = await _seed_session(fake_storage_provider, "s-nocode")

    await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [_failure(None)])

    row = await _row(fake_storage_provider, "s-nocode")
    assert (row.status, row.ended_reason, row.ended_detail) == (SessionStatus.WAITING, None, None)
    assert row.last_turn_error is not None and row.last_turn_error.code == NO_CODE


@pytest.mark.asyncio
async def test_a_crash_that_is_not_a_model_error_still_ends_the_session(
    fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    session = await _seed_session(fake_storage_provider, "s-crash")

    await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [RuntimeError("kaboom")])

    row = await _row(fake_storage_provider, "s-crash")
    assert (row.status, row.ended_reason, row.ended_detail) == (SessionStatus.ENDED, "failed", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["server_error", "generation_timeout"])
async def test_an_autonomous_session_still_ends_on_a_transport_failure(
    code, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    """A trigger or webhook one-shot has nobody to resume it: resting would wedge a ``parallelism="skip"`` subscription gate forever."""
    session = await _seed_session(fake_storage_provider, "s-auto", autonomous=True)

    await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [_failure(code)])

    row = await _row(fake_storage_provider, "s-auto")
    assert (row.status, row.ended_reason, row.ended_detail) == (SessionStatus.ENDED, "failed", code)


@pytest.mark.asyncio
async def test_a_graph_bound_session_still_ends_on_a_transport_failure(
    fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    from primer.model.workspace_session import GraphSessionBinding

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    session = await _seed_session(fake_storage_provider, "s-graph")
    # autonomous=False is an explicit override that a graph binding must not take: a graph run has nobody to resume it
    await storage.update(session.model_copy(update={"binding": GraphSessionBinding(graph_id="g-1"), "autonomous": False}))
    session = await _row(fake_storage_provider, "s-graph")

    await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [_failure("server_error")])

    row = await _row(fake_storage_provider, "s-graph")
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_no, state", [(0, "waiting"), (3, "parked")])
async def test_the_session_state_a_resting_failure_serves(
    turn_no, state, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    """A failed turn does not bump ``turn_no``: a first-turn failure serves ``waiting`` (the value a never-started session serves), a later one
    ``parked`` (resting after a completed turn). ``last_turn_error`` is what tells either from a healthy rest."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    session = await _seed_session(fake_storage_provider, "s-state")
    await storage.update(session.model_copy(update={"turn_no": turn_no}))
    session = await _row(fake_storage_provider, "s-state")

    await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [_failure("server_error")])

    row = await _row(fake_storage_provider, "s-state")
    assert row.status == SessionStatus.WAITING and row.session_state == state
    assert row.last_turn_error is not None


@pytest.mark.asyncio
async def test_a_crash_is_announced_as_a_failed_turn_too(fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch):
    session = await _seed_session(fake_storage_provider, "s-crash-ev")

    _, emitted = await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [RuntimeError("kaboom")])

    turn_failed = [payload for name, payload in emitted.events if name == "session.turn_failed"]
    assert turn_failed == [{"code": "turn_failed", "ended": True}], emitted.events


@pytest.mark.asyncio
async def test_the_event_says_whether_the_session_ended(fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch):
    rested = await _seed_session(fake_storage_provider, "s-ev-rest")
    ended = await _seed_session(fake_storage_provider, "s-ev-end")

    _, rest_events = await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, rested, [_failure("rate_limit")])
    _, end_events = await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, ended, [_failure("auth_error")])

    assert [p for n, p in rest_events.events if n == "session.turn_failed"] == [{"code": "rate_limit", "ended": False}]
    assert [p for n, p in end_events.events if n == "session.turn_failed"] == [{"code": "auth_error", "ended": True}]


@pytest.mark.asyncio
async def test_the_log_of_a_rested_failure_has_the_shape_of_an_ended_one(
    fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    """The turn windows (and the fold-on-read of ticket 01a11ca5, which makes a failed turn ONE window) depend on the records a failed turn
    writes, not on whether the session then rests: the stream's ERROR, dispatch's ERROR, and the release marker the claim adapter writes when the
    pool releases the failed turn. Each session is released as the pool releases it."""
    from primer.claim.adapters.sessions import SessionClaimAdapter

    class _Registry:
        async def get_workspace(self, workspace_id):
            return fake_workspace_io

    rested = await _seed_session(fake_storage_provider, "s-shape-rest")
    ended = await _seed_session(fake_storage_provider, "s-shape-end", autonomous=True)
    events = [TextDelta(text="par", index=0), Error(code="server_error", message="boom", fatal=True), _failure("server_error")]
    adapter = SessionClaimAdapter(
        session_storage=fake_storage_provider.get_storage(WorkspaceSession), workspace_registry=_Registry(), event_bus=fake_event_bus,
    )

    for session in (rested, ended):
        outcome, _ = await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, list(events))
        await adapter.on_release(None, session.id, outcome=outcome)

    def kinds(sid: str) -> list[str]:
        return [json.loads(line)["kind"] for line in fake_workspace_io.read_lines(sid)]

    assert kinds("s-shape-rest") == kinds("s-shape-end")
    assert kinds("s-shape-rest").count(SessionMessageKind.ERROR.value) == 3, "stream ERROR, dispatch ERROR, release marker"
    assert (await _row(fake_storage_provider, "s-shape-rest")).status == SessionStatus.WAITING


class _Slot:
    async def reopen(self) -> None: ...

    async def append_instruction(self, content, *, extra_parts=None) -> None: ...


class _Workspace(FakeWorkspaceIO):
    async def get_session(self, session_id):
        return _Slot()


class _Registry:
    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id):
        return self._workspace

    async def get_workspace_row(self, workspace_id):
        return None


class _Scheduler:
    async def enqueue(self, session_id) -> None: ...


class _Engine:
    async def upsert(self, *args, **kwargs) -> None: ...


@pytest.mark.asyncio
async def test_the_next_send_continues_the_same_invocation_and_clears_the_failure(
    fake_event_bus, fake_storage_provider, monkeypatch,
):
    """The console's Retry (and any new message) on a rested failure: no reopen, so no invocation divider and no invocation bump; the failure is
    cleared when the next turn starts, and the turn answers."""
    workspace = _Workspace()
    session = await _seed_session(fake_storage_provider, "s-retry")
    wake = SessionWakeDeps(
        storage_provider=fake_storage_provider, scheduler=_Scheduler(), claim_engine=_Engine(),
        workspace_registry=_Registry(workspace), event_bus=fake_event_bus,
    )
    await _run(fake_storage_provider, workspace, fake_event_bus, monkeypatch, session, [_failure("server_error")])
    assert (await _row(fake_storage_provider, "s-retry")).status == SessionStatus.WAITING

    await wake_session(workspace_id=session.workspace_id, session_id="s-retry", instruction="try again", human_intent=True, deps=wake)

    woken = await _row(fake_storage_provider, "s-retry")
    assert woken.status != SessionStatus.ENDED and "invocation" not in (woken.metadata or {})
    outcome, _ = await _run(
        fake_storage_provider, workspace, fake_event_bus, monkeypatch, woken,
        [TextDelta(text="done", index=0), Done(stop_reason="stop", raw_reason="stop")], last_done_reason="stop",
    )
    assert outcome.success is True
    row = await _row(fake_storage_provider, "s-retry")
    assert row.last_turn_error is None and row.status == SessionStatus.WAITING and row.ended_reason is None
    kinds = [json.loads(line)["kind"] for line in workspace.read_lines("s-retry")]
    assert SessionMessageKind.INVOCATION_DIVIDER.value not in kinds, kinds
    assert kinds.count(SessionMessageKind.USER_INPUT.value) >= 1


def test_the_resting_codes_are_pinned():
    assert dispatch._RESTING_FAILURE_CODES == frozenset(REST_CODES) | {NO_CODE}


@pytest.mark.asyncio
@pytest.mark.parametrize("cls, code", [
    ("ServerError", "server_error"), ("RateLimitError", "rate_limit"), ("NetworkError", "network_error"), ("ProviderTimeoutError", "stream_timeout"),
])
async def test_a_raised_transport_error_with_no_code_is_classified_by_its_class(
    cls, code, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    """The adapters' own exhausted-pool error (``AggregatedLLM``'s ``RateLimitError``, aggregated.py) carries no code: the class says what it is."""
    from primer.model import except_

    session = await _seed_session(fake_storage_provider, "s-bycls")

    _, emitted = await _run(
        fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [getattr(except_, cls)("the pool is exhausted")],
    )

    row = await _row(fake_storage_provider, "s-bycls")
    assert (row.status, row.ended_reason) == (SessionStatus.WAITING, None)
    assert row.last_turn_error is not None and row.last_turn_error.code == code
    assert [p for n, p in emitted.events if n == "session.turn_failed"] == [{"code": code, "ended": False}]


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ["PrimerError", "ProviderError", "BadRequestError", "AuthenticationError"])
async def test_a_raised_error_with_no_code_that_is_not_a_transport_class_still_ends_the_session(
    cls, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    from primer.model import except_

    session = await _seed_session(fake_storage_provider, "s-generic")

    await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [getattr(except_, cls)("something")])

    row = await _row(fake_storage_provider, "s-generic")
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    assert row.last_turn_error is not None and row.last_turn_error.code == "turn_failed"


@pytest.mark.asyncio
async def test_a_cancel_that_lands_while_the_turn_fails_ends_the_session(
    fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    """Whether the session rests is decided under the lifecycle lock, from a fresh read: a Cancel (the flag the route sets on a RUNNING row) that
    landed since the turn started is not lost. Resting would have left ``cancel_requested`` on a WAITING row, and the NEXT send would have ended
    the session as cancelled without ever calling the model."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    session = await _seed_session(fake_storage_provider, "s-cancel")

    class _CancelThenFail(FakeExecutor):
        async def invoke(self, messages, **kwargs):
            row = await storage.get("s-cancel")
            await storage.update(row.model_copy(update={"cancel_requested": True}))
            raise _failure("server_error")
            yield  # pragma: no cover

    async def build(_session: WorkspaceSession):
        return _CancelThenFail([])

    monkeypatch.setattr(dispatch, "_event_recorder", lambda deps: _Emitted())
    deps = SessionDispatchDeps(storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus, build_executor=build)
    await run_one_session_turn(_make_lease(session.id), deps)

    row = await _row(fake_storage_provider, "s-cancel")
    assert row.status == SessionStatus.ENDED, "the Cancel was swallowed by a rest"


@pytest.mark.asyncio
async def test_a_failure_that_could_not_be_stamped_ends_the_session_instead_of_resting_it(
    fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    """A rested first-turn failure WITHOUT its stamp has every mark of a session that never started, and the stuck-session sweeper would end it
    `never_started` after the grace. When the stamp does not land the session ends, with the real reason."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    session = await _seed_session(fake_storage_provider, "s-unstamped")
    real_patch = storage.patch_if

    async def patch_if(id, patch=None, **kwargs):
        if patch and patch.get("last_turn_error") is not None:
            raise RuntimeError("storage hiccup")
        return await real_patch(id, patch, **kwargs)

    storage.patch_if = patch_if

    await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [_failure("server_error")])

    row = await _row(fake_storage_provider, "s-unstamped")
    assert (row.status, row.ended_reason, row.ended_detail) == (SessionStatus.ENDED, "failed", "server_error")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["error", "graph_failed"])
async def test_a_failure_the_clean_arm_ends_is_announced_too(
    reason, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
):
    """``Done(stop_reason="error")`` and a failed graph run end the turn through the clean-completion arm, not through the exception exit."""
    session = await _seed_session(fake_storage_provider, f"s-{reason}")

    _, emitted = await _run(
        fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session,
        [Done(stop_reason="error", raw_reason="error")], last_done_reason=reason,
    )

    row = await _row(fake_storage_provider, f"s-{reason}")
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    assert [p for n, p in emitted.events if n == "session.turn_failed"] == [{"code": reason, "ended": True}]


@pytest.mark.asyncio
async def test_turn_failed_is_announced_before_session_ended(fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch):
    """A consumer that reacts to ``session.ended`` must already have the failure that ended it."""
    session = await _seed_session(fake_storage_provider, "s-order")

    _, emitted = await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, monkeypatch, session, [_failure("auth_error")])

    types = emitted.types()
    assert types.index("session.turn_failed") < types.index("session.ended"), types

