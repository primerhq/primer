"""Only a workspace OUTAGE is a 503, and an outage is logged once (follow-up asks on #489 and #521, lead 2026-10-08).

``_read_log_bytes`` (the one read of a session's log files) turned EVERY exception into ``WorkspaceUnreachableError``, so a request the
workspace understood and refused (a path that is a directory, a conflict) read as "the workspace is down", and a plain bug in a backend
did too. It also logged a full traceback on every read, which for a console polling the messages route every 2 s is a traceback every
2 s for as long as the outage lasts. Now:

* a missing file stays an empty log; a domain error the workspace raised (``BadRequestError``, ``ConflictError``, any ``PrimerError``) keeps
  its own status; only transport and OS failures (``OSError`` including ``ConnectionError`` and ``PermissionError``, ``TimeoutError``, the
  runtime client's own errors, aiohttp's) are an outage; anything else is a bug and propagates as one;
* the runtime client's own error is read by its CODE, never passed on raw (it is a plain ``Exception``: unmapped it is a 500 with a
  traceback on every poll): ``ENOENT`` is a log that was never written (an empty 200), ``EISDIR`` and ``ENOTDIR`` are a path that is not
  a file (a 400), and every other code, including one this code has never heard of, is an outage, as a local ``PermissionError`` is;
* an outage is one (workspace, file): it logs one full traceback until a read of that file is answered again (or five minutes pass), and one
  short line naming the file for each read after that.
"""

from __future__ import annotations

import logging

import httpx
import pytest

import primer.api.routers.sessions as sessions_router
from primer.model.except_ import BadRequestError, ConflictError, NotFoundError, WorkspaceUnreachableError
from tests.api.test_session_messages_route import _FakeWorkspace, _seed_session


class _RaisingWorkspace(_FakeWorkspace):
    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._exc = exc

    async def read_file(self, path: str) -> bytes:
        raise self._exc


@pytest.fixture(autouse=True)
def _fresh_outage_log_state():
    state = getattr(sessions_router, "_outage_logged_at", None)
    if state is not None:
        state.clear()
    yield
    if state is not None:
        state.clear()


async def _serve(app, fake_storage_provider, sid: str, ws) -> None:
    from primer.model.workspace_session import SessionStatus

    await _seed_session(fake_storage_provider, sid, SessionStatus.ENDED)

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]


# --- what is and is not an outage ----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_the_workspace_refused_keeps_its_own_status(client: httpx.AsyncClient, app, fake_storage_provider) -> None:
    await _serve(app, fake_storage_provider, "s-dir", _RaisingWorkspace(BadRequestError("'.state/sessions/s-dir/messages.jsonl' is not a file")))
    r = await client.get("/v1/sessions/s-dir/messages")
    assert r.status_code == 400, (r.status_code, r.text)
    assert r.json()["type"] == "/errors/bad-request"


@pytest.mark.asyncio
async def test_any_other_domain_error_keeps_its_own_status(client: httpx.AsyncClient, app, fake_storage_provider) -> None:
    await _serve(app, fake_storage_provider, "s-conflict", _RaisingWorkspace(ConflictError("the workspace is being torn down")))
    r = await client.get("/v1/sessions/s-conflict/turn_log")
    assert r.status_code == 409, (r.status_code, r.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    ConnectionRefusedError("refused"), PermissionError("log is mode 000"), OSError("disk gone"), TimeoutError("runtime did not answer"),
])
async def test_transport_and_os_failures_are_an_outage(client: httpx.AsyncClient, app, fake_storage_provider, exc) -> None:
    await _serve(app, fake_storage_provider, "s-out", _RaisingWorkspace(exc))
    r = await client.get("/v1/sessions/s-out/messages")
    assert r.status_code == 503 and r.json()["type"] == "/errors/workspace-unreachable"


@pytest.mark.asyncio
async def test_the_runtime_clients_own_errors_and_aiohttps_are_an_outage(client: httpx.AsyncClient, app, fake_storage_provider) -> None:
    aiohttp = pytest.importorskip("aiohttp")
    from primer.workspace.runtime.protocol import ErrorCode
    from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeClientError

    await _serve(app, fake_storage_provider, "s-rt", _RaisingWorkspace(RuntimeClientError(ErrorCode.EPROTOCOL, "Connection lost")))
    assert (await client.get("/v1/sessions/s-rt/messages")).status_code == 503
    await _serve(app, fake_storage_provider, "s-http", _RaisingWorkspace(aiohttp.ClientConnectionError("reset")))
    assert (await client.get("/v1/sessions/s-http/messages")).status_code == 503


@pytest.mark.asyncio
async def test_a_bug_in_a_backend_is_not_passed_off_as_an_outage(fake_storage_provider) -> None:
    with pytest.raises(KeyError):
        await sessions_router._read_log_bytes(_RaisingWorkspace(KeyError("a bug")), ".state/x.jsonl", workspace_id="ws-1")


# --- the graph turn-log routes read the same helper (survivor S2 of the #489 review) -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/v1/graphs/g-1/runs/{sid}/turn_log",
    "/v1/graphs/g-1/runs/{sid}/nodes/begin/turn_log",
])
async def test_a_graph_turn_log_of_an_unreachable_workspace_is_a_typed_503(
    client: httpx.AsyncClient, app, fake_storage_provider, path,
) -> None:
    from primer.model.workspace_session import GraphSessionBinding, SessionStatus, WorkspaceSession

    sid = "sess-graph-down"
    from tests.api.test_session_messages_route import _now
    await fake_storage_provider.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=sid, workspace_id="ws-1", binding=GraphSessionBinding(graph_id="g-1"),
        status=SessionStatus.ENDED, created_at=_now(), turn_status="idle",
    ))
    ws = _RaisingWorkspace(ConnectionError("runtime connection refused"))

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get(path.format(sid=sid))
    assert r.status_code == 503, (r.status_code, r.text)
    body = r.json()
    assert body["type"] == "/errors/workspace-unreachable"
    assert "runtime connection refused" not in body["detail"]


# --- one traceback per outage --------------------------------------------------------------------------------------------------


def _warnings_with_traceback(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "could not be read" in r.getMessage() and r.exc_info]


@pytest.mark.asyncio
async def test_a_poll_loop_does_not_log_a_traceback_per_poll(caplog) -> None:
    ws = _RaisingWorkspace(ConnectionError("down"))
    with caplog.at_level(logging.DEBUG):
        for _ in range(5):
            with pytest.raises(WorkspaceUnreachableError):
                await sessions_router._read_log_bytes(ws, ".state/x.jsonl", workspace_id="ws-1", session_id="s1")
    assert len(_warnings_with_traceback(caplog)) == 1, "one traceback for the outage"
    later = [r for r in caplog.records if "still unreachable" in r.getMessage()]
    assert len(later) == 4 and not any(r.exc_info for r in later), "and one short line, without a traceback, for each read after it"


@pytest.mark.asyncio
async def test_each_workspace_gets_its_own_traceback(caplog) -> None:
    ws = _RaisingWorkspace(ConnectionError("down"))
    with caplog.at_level(logging.WARNING):
        for wid in ("ws-1", "ws-2", "ws-1", "ws-2"):
            with pytest.raises(WorkspaceUnreachableError):
                await sessions_router._read_log_bytes(ws, ".state/x.jsonl", workspace_id=wid)
    assert len(_warnings_with_traceback(caplog)) == 2


@pytest.mark.asyncio
async def test_a_successful_read_ends_the_outage_so_the_next_one_logs_its_own_traceback(caplog) -> None:
    down = _RaisingWorkspace(ConnectionError("down"))
    up = _FakeWorkspace()
    up.write(".state/x.jsonl", "")
    with caplog.at_level(logging.WARNING):
        with pytest.raises(WorkspaceUnreachableError):
            await sessions_router._read_log_bytes(down, ".state/x.jsonl", workspace_id="ws-1")
        await sessions_router._read_log_bytes(up, ".state/x.jsonl", workspace_id="ws-1")
        with pytest.raises(WorkspaceUnreachableError):
            await sessions_router._read_log_bytes(down, ".state/x.jsonl", workspace_id="ws-1")
    assert len(_warnings_with_traceback(caplog)) == 2


@pytest.mark.asyncio
async def test_a_long_outage_logs_again_after_the_window(caplog, monkeypatch) -> None:
    ws = _RaisingWorkspace(ConnectionError("down"))
    clock = {"t": 1000.0}
    monkeypatch.setattr(sessions_router, "_now", lambda: clock["t"])
    with caplog.at_level(logging.WARNING):
        for step in (0.0, 10.0, 301.0):
            clock["t"] = 1000.0 + step
            with pytest.raises(WorkspaceUnreachableError):
                await sessions_router._read_log_bytes(ws, ".state/x.jsonl", workspace_id="ws-1")
    assert len(_warnings_with_traceback(caplog)) == 2, "a reminder with the traceback once the window has passed"


# --- one outage is one (workspace, file), review of PR 521 -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_file_that_is_not_there_ends_the_outage_so_the_next_failure_logs_its_own_traceback(caplog) -> None:
    """down -> NotFound -> down is two outages: the workspace answered in between."""
    down = _RaisingWorkspace(ConnectionError("down"))
    missing = _RaisingWorkspace(NotFoundError("no such file"))
    with caplog.at_level(logging.WARNING):
        with pytest.raises(WorkspaceUnreachableError):
            await sessions_router._read_log_bytes(down, ".state/x.jsonl", workspace_id="ws-1")
        assert await sessions_router._read_log_bytes(missing, ".state/x.jsonl", workspace_id="ws-1") == b""
        with pytest.raises(WorkspaceUnreachableError):
            await sessions_router._read_log_bytes(down, ".state/x.jsonl", workspace_id="ws-1")
    assert len(_warnings_with_traceback(caplog)) == 2


@pytest.mark.asyncio
async def test_a_different_file_that_answers_does_not_end_the_outage_of_this_one(caplog) -> None:
    """A poll cycle reads several files of one workspace (messages, turn log, state). One that is simply absent used to reset the
    dedupe key of the whole workspace, so the file that WAS failing logged a traceback again on the next poll."""
    down = _RaisingWorkspace(ConnectionError("down"))
    missing = _RaisingWorkspace(NotFoundError("no such file"))
    up = _FakeWorkspace()
    up.write(".state/other.jsonl", "")
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            with pytest.raises(WorkspaceUnreachableError):
                await sessions_router._read_log_bytes(down, ".state/messages.jsonl", workspace_id="ws-1")
            assert await sessions_router._read_log_bytes(missing, ".state/turn_log.jsonl", workspace_id="ws-1") == b""
            await sessions_router._read_log_bytes(up, ".state/other.jsonl", workspace_id="ws-1")
    assert len(_warnings_with_traceback(caplog)) == 1, "the failing file logged one traceback for the whole outage"


@pytest.mark.asyncio
async def test_two_files_failing_in_one_workspace_each_get_their_own_traceback(caplog) -> None:
    ws = _RaisingWorkspace(ConnectionError("down"))
    with caplog.at_level(logging.WARNING):
        for path in (".state/a.jsonl", ".state/b.jsonl", ".state/a.jsonl", ".state/b.jsonl"):
            with pytest.raises(WorkspaceUnreachableError):
                await sessions_router._read_log_bytes(ws, path, workspace_id="ws-1")
    assert len(_warnings_with_traceback(caplog)) == 2


@pytest.mark.asyncio
async def test_the_short_line_names_the_file(caplog) -> None:
    ws = _RaisingWorkspace(ConnectionError("down"))
    with caplog.at_level(logging.DEBUG):
        for _ in range(2):
            with pytest.raises(WorkspaceUnreachableError):
                await sessions_router._read_log_bytes(ws, ".state/messages.jsonl", workspace_id="ws-1")
    short = [r.getMessage() for r in caplog.records if "still unreachable" in r.getMessage()]
    assert len(short) == 1 and ".state/messages.jsonl" in short[0]


def test_the_clock_is_one_function_a_test_can_move() -> None:
    assert callable(getattr(sessions_router, "_now", None))
    assert isinstance(sessions_router._now(), float)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["EPROTOCOL", "ETIMEDOUT", "EINTERNAL"])
async def test_a_runtime_error_of_a_transport_kind_is_an_outage(code: str) -> None:
    from primer.workspace.runtime.protocol import ErrorCode
    from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeClientError

    ws = _RaisingWorkspace(RuntimeClientError(ErrorCode(code), "the runtime did not answer"))
    with pytest.raises(WorkspaceUnreachableError):
        await sessions_router._read_log_bytes(ws, ".state/x.jsonl", workspace_id="ws-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "status", "problem"), [
    ("ENOENT", 200, None),
    ("EISDIR", 400, "/errors/bad-request"),
    ("ENOTDIR", 400, "/errors/bad-request"),
    ("EACCES", 503, "/errors/workspace-unreachable"),          # the same as a local PermissionError
    ("EEXIST", 503, "/errors/workspace-unreachable"),
    ("EUNSUPPORTED", 503, "/errors/workspace-unreachable"),
    ("EPROTOCOL", 503, "/errors/workspace-unreachable"),
    ("ETIMEDOUT", 503, "/errors/workspace-unreachable"),
    ("EINTERNAL", 503, "/errors/workspace-unreachable"),
    ("EWHATEVER", 503, "/errors/workspace-unreachable"),       # a code nobody has heard of defaults to an outage, not to a 500
])
async def test_a_runtime_errors_code_decides_the_status_of_the_route(
    client: httpx.AsyncClient, app, fake_storage_provider, caplog, code: str, status: int, problem: str | None,
) -> None:
    """The runtime client's error is a plain Exception, not a PrimerError: passed on raw it is a 500 and a logged traceback on EVERY 2 s poll."""
    from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeClientError

    await _serve(app, fake_storage_provider, "s-code", _RaisingWorkspace(RuntimeClientError(code, "the runtime says " + code)))
    with caplog.at_level(logging.ERROR):
        r = await client.get("/v1/sessions/s-code/messages")
    assert r.status_code == status, (code, r.status_code, r.text)
    if problem is None:
        assert r.json()["items"] == [], "a log that was never written is an empty list"
    else:
        assert r.json()["type"] == problem
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR], "a classified answer must not log an error on every poll"


@pytest.mark.asyncio
async def test_a_local_permission_error_and_the_runtimes_eacces_are_classified_the_same(client: httpx.AsyncClient, app, fake_storage_provider) -> None:
    from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeClientError

    await _serve(app, fake_storage_provider, "s-local", _RaisingWorkspace(PermissionError("log is mode 000")))
    local = await client.get("/v1/sessions/s-local/messages")
    await _serve(app, fake_storage_provider, "s-remote", _RaisingWorkspace(RuntimeClientError("EACCES", "denied")))
    remote = await client.get("/v1/sessions/s-remote/messages")
    assert local.status_code == remote.status_code == 503


@pytest.mark.asyncio
async def test_a_runtimes_missing_file_ends_the_outage_like_a_not_found_does(caplog) -> None:
    from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeClientError

    down = _RaisingWorkspace(ConnectionError("down"))
    missing = _RaisingWorkspace(RuntimeClientError("ENOENT", "no such file"))
    with caplog.at_level(logging.WARNING):
        with pytest.raises(WorkspaceUnreachableError):
            await sessions_router._read_log_bytes(down, ".state/x.jsonl", workspace_id="ws-1")
        assert await sessions_router._read_log_bytes(missing, ".state/x.jsonl", workspace_id="ws-1") == b""
        with pytest.raises(WorkspaceUnreachableError):
            await sessions_router._read_log_bytes(down, ".state/x.jsonl", workspace_id="ws-1")
    assert len(_warnings_with_traceback(caplog)) == 2


@pytest.mark.asyncio
async def test_a_runtime_error_for_a_path_that_is_not_a_file_names_the_path() -> None:
    from primer.model.except_ import BadRequestError
    from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeClientError

    ws = _RaisingWorkspace(RuntimeClientError("EISDIR", "is a directory"))
    with pytest.raises(BadRequestError, match=r"\.state/x\.jsonl"):
        await sessions_router._read_log_bytes(ws, ".state/x.jsonl", workspace_id="ws-1")
