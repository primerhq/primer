"""Only a workspace OUTAGE is a 503, and an outage is logged once (follow-up asks on #489, lead 2026-10-08).

``_read_log_bytes`` (the one read of a session's log files) turned EVERY exception into ``WorkspaceUnreachableError``, so a request the
workspace understood and refused (a path that is a directory, a conflict) read as "the workspace is down", and a plain bug in a backend
did too. It also logged a full traceback on every read, which for a console polling the messages route every 2 s is a traceback every
2 s for as long as the outage lasts. Now:

* a missing file stays an empty log; a domain error the workspace raised (``BadRequestError``, ``ConflictError``, any ``PrimerError``) keeps
  its own status; only transport and OS failures (``OSError`` including ``ConnectionError`` and ``PermissionError``, ``TimeoutError``, the
  runtime client's own errors, aiohttp's) are an outage; anything else is a bug and propagates as one;
* an outage logs one full traceback per workspace until a read succeeds again (or five minutes pass), and one short line for each read
  after that.
"""

from __future__ import annotations

import logging

import httpx
import pytest

import primer.api.routers.sessions as sessions_router
from primer.model.except_ import BadRequestError, ConflictError, WorkspaceUnreachableError
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
    monkeypatch.setattr(sessions_router.time, "monotonic", lambda: clock["t"])
    with caplog.at_level(logging.WARNING):
        for step in (0.0, 10.0, 301.0):
            clock["t"] = 1000.0 + step
            with pytest.raises(WorkspaceUnreachableError):
                await sessions_router._read_log_bytes(ws, ".state/x.jsonl", workspace_id="ws-1")
    assert len(_warnings_with_traceback(caplog)) == 2, "a reminder with the traceback once the window has passed"
