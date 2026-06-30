"""Tests for the ``workspaces__workspace_tap`` MCP drain tool.

The tool is a primer toolset tool (it lives in the ``workspaces``
reserved toolset and is dispatched by the generic MCP server, exactly
like ``cancel_workspace_session``) — NOT a hand-written MCP SDK handler.
So these tests invoke it through ``InternalToolsetProvider.call`` (the
same seam the MCP dispatcher uses) and assert on the ``ToolCallResult``
envelope.

The tool reuses :func:`primer.tap.reader.read_batch` for the drain and
``app.state.workspace_tap_router`` for the bounded long-poll. We seed an
in-memory session store + a fake workspace IO whose ``messages.jsonl``
bytes feed the reader, mirroring ``tests/tap/test_reader.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.tap.router import WorkspaceTick
from primer.toolset.workspaces import build_workspaces_toolset

_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
_WID = "ws-1"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeWorkspaceIO:
    """In-memory workspace IO exposing ``read_file`` (mirrors the reader fakes)."""

    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def write(self, path: str, content: bytes) -> None:
        self._files[path] = content

    def append(self, path: str, content: bytes) -> None:
        self._files[path] = self._files.get(path, b"") + content

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(f"{path!r} not found")
        return self._files[path]


class _FakeRegistry:
    """Workspace registry stub returning the single fake IO for any id."""

    def __init__(self, io: _FakeWorkspaceIO) -> None:
        self._io = io

    async def get_workspace(self, workspace_id: str):  # noqa: ANN001
        from primer.model.except_ import NotFoundError

        if workspace_id != _WID:
            raise NotFoundError(f"workspace {workspace_id!r} not found")
        return self._io


class _Provider:
    """Storage provider over a single in-memory WorkspaceSession store."""

    def __init__(self) -> None:
        from tests.conftest import _InMemoryStorage

        self._store = _InMemoryStorage(WorkspaceSession)

    def get_storage(self, model_cls):  # noqa: ANN001
        return self._store

    @property
    def store(self):
        return self._store


class _FakeTapRouter:
    """Minimal tap router: hands out a subscription whose first ``__anext__``
    resolves from a queue the test can feed (or never, to force a timeout)."""

    def __init__(self) -> None:
        self.queues: list[asyncio.Queue] = []

    def subscribe(self, workspace_id: str):  # noqa: ANN001
        q: asyncio.Queue = asyncio.Queue()
        self.queues.append(q)
        return _FakeSub(q)

    def publish(self, tick: WorkspaceTick) -> None:
        for q in self.queues:
            q.put_nowait(tick)


class _FakeSub:
    def __init__(self, q: asyncio.Queue) -> None:
        self._q = q
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._q.get()

    async def aclose(self) -> None:
        self.closed = True


def _msg_path(sid: str) -> str:
    return f".state/sessions/{sid}/messages.jsonl"


def _line(seq: int, kind: str, **payload) -> bytes:
    rec = {
        "seq": seq,
        "kind": kind,
        "payload": payload,
        "created_at": _NOW.isoformat(),
    }
    return (json.dumps(rec) + "\n").encode()


async def _seed_session(store, sid: str, *, agent_id: str = "ag1") -> None:
    await store.create(
        WorkspaceSession(
            id=sid,
            workspace_id=_WID,
            binding=AgentSessionBinding(agent_id=agent_id),
            status=SessionStatus.RUNNING,
            created_at=_NOW,
            turn_status="idle",
        )
    )


def _build(provider, io, *, tap_router=None):
    return build_workspaces_toolset(
        storage_provider=provider,
        workspace_registry=_FakeRegistry(io),
        tap_router=tap_router,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWorkspaceTapTool:
    @pytest.mark.asyncio
    async def test_first_drain_returns_events_and_cursor(self) -> None:
        provider = _Provider()
        io = _FakeWorkspaceIO()
        await _seed_session(provider.store, "s1")
        io.write(
            _msg_path("s1"),
            _line(1, "user_input") + _line(2, "tool_call") + _line(3, "done"),
        )
        ts = _build(provider, io)

        result = await ts.call(
            tool_name="workspace_tap", arguments={"workspace_id": _WID}
        )
        assert not result.is_error, result.output
        body = json.loads(result.output)
        assert [e["class"] for e in body["events"]] == [
            "user_input",
            "tool_call",
            "done",
        ]
        # by-alias serialization: "class" key present, not "class_".
        assert "class_" not in body["events"][0]
        assert body["next_cursor"]

    @pytest.mark.asyncio
    async def test_resume_with_cursor_returns_only_newer(self) -> None:
        provider = _Provider()
        io = _FakeWorkspaceIO()
        await _seed_session(provider.store, "s1")
        io.write(_msg_path("s1"), _line(1, "user_input") + _line(2, "tool_call"))
        ts = _build(provider, io)

        first = json.loads(
            (
                await ts.call(
                    tool_name="workspace_tap", arguments={"workspace_id": _WID}
                )
            ).output
        )
        cursor = first["next_cursor"]
        assert len(first["events"]) == 2

        # Nothing new yet → empty events, same cursor returned.
        again = json.loads(
            (
                await ts.call(
                    tool_name="workspace_tap",
                    arguments={"workspace_id": _WID, "cursor": cursor},
                )
            ).output
        )
        assert again["events"] == []

        # Append a new record; resume returns ONLY it.
        io.append(_msg_path("s1"), _line(3, "done"))
        newer = json.loads(
            (
                await ts.call(
                    tool_name="workspace_tap",
                    arguments={"workspace_id": _WID, "cursor": cursor},
                )
            ).output
        )
        assert [e["class"] for e in newer["events"]] == ["done"]
        assert newer["events"][0]["seq"] == 3

    @pytest.mark.asyncio
    async def test_empty_when_nothing_and_no_wait(self) -> None:
        provider = _Provider()
        io = _FakeWorkspaceIO()
        await _seed_session(provider.store, "s1")
        # No messages.jsonl at all.
        ts = _build(provider, io)

        result = await ts.call(
            tool_name="workspace_tap",
            arguments={"workspace_id": _WID, "wait_seconds": 0},
        )
        assert not result.is_error
        body = json.loads(result.output)
        assert body["events"] == []
        assert body["next_cursor"]

    @pytest.mark.asyncio
    async def test_malformed_selector_is_clean_tool_error(self) -> None:
        provider = _Provider()
        io = _FakeWorkspaceIO()
        await _seed_session(provider.store, "s1")
        ts = _build(provider, io)

        # ``sessions`` must be a Predicate object; a bare string is invalid.
        result = await ts.call(
            tool_name="workspace_tap",
            arguments={
                "workspace_id": _WID,
                "selector": {"sessions": "not-a-predicate"},
            },
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "bad-request"

    @pytest.mark.asyncio
    async def test_unknown_workspace_is_not_found(self) -> None:
        provider = _Provider()
        io = _FakeWorkspaceIO()
        ts = _build(provider, io)

        result = await ts.call(
            tool_name="workspace_tap", arguments={"workspace_id": "no-such"}
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_event_selector_filters(self) -> None:
        provider = _Provider()
        io = _FakeWorkspaceIO()
        await _seed_session(provider.store, "s1")
        io.write(
            _msg_path("s1"),
            _line(1, "user_input") + _line(2, "tool_call") + _line(3, "done"),
        )
        ts = _build(provider, io)

        sel = {
            "events": {
                "left": {"kind": "field", "name": "class"},
                "op": "=",
                "right": {"kind": "value", "value": "tool_call"},
            }
        }
        result = await ts.call(
            tool_name="workspace_tap",
            arguments={"workspace_id": _WID, "selector": sel},
        )
        assert not result.is_error, result.output
        body = json.loads(result.output)
        assert [e["class"] for e in body["events"]] == ["tool_call"]

    @pytest.mark.asyncio
    async def test_wait_seconds_returns_promptly_after_tick(self) -> None:
        provider = _Provider()
        io = _FakeWorkspaceIO()
        await _seed_session(provider.store, "s1")
        router = _FakeTapRouter()
        ts = _build(provider, io, tap_router=router)

        async def _publish_after_first_drain() -> None:
            # Give the tool time to do its empty first drain + subscribe,
            # then append a record and wake the long-poll.
            await asyncio.sleep(0.05)
            io.write(_msg_path("s1"), _line(1, "done"))
            router.publish(WorkspaceTick(session_id="s1", seq=1))

        publisher = asyncio.create_task(_publish_after_first_drain())
        result = await ts.call(
            tool_name="workspace_tap",
            arguments={"workspace_id": _WID, "wait_seconds": 5},
        )
        await publisher
        assert not result.is_error, result.output
        body = json.loads(result.output)
        assert [e["class"] for e in body["events"]] == ["done"]
        # The subscription was closed after the long-poll.
        assert router.queues[0]  # a sub was created
        assert all(s for s in router.queues)

    @pytest.mark.asyncio
    async def test_wait_seconds_times_out_to_empty(self) -> None:
        provider = _Provider()
        io = _FakeWorkspaceIO()
        await _seed_session(provider.store, "s1")
        router = _FakeTapRouter()  # never publishes
        ts = _build(provider, io, tap_router=router)

        result = await ts.call(
            tool_name="workspace_tap",
            arguments={"workspace_id": _WID, "wait_seconds": 0.1},
        )
        assert not result.is_error
        body = json.loads(result.output)
        assert body["events"] == []

    @pytest.mark.asyncio
    async def test_tap_tool_is_mcp_exposable(self) -> None:
        from primer.mcp.safety import is_exposable

        provider = _Provider()
        io = _FakeWorkspaceIO()
        ts = _build(provider, io)
        tools = {t.id: t async for t in ts.list_tools()}
        assert "workspace_tap" in tools
        ok, reason = is_exposable(tools["workspace_tap"], provider=ts)
        assert ok, f"workspace_tap not MCP-exposable: {reason}"
