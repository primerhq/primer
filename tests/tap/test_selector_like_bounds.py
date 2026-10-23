"""G6-4: a tap selector's LIKE pattern is bounded in size and time, and matched off the event loop.

``GET /v1/workspaces/{id}/tap?selector=`` (user tier) and the ``workspace_tap`` MCP tool evaluate the ``events`` predicate in
memory. ``LIKE`` turned ``%`` into ``.*`` under stdlib ``re``, so ``%a%a%a%a%!`` grew polynomially with the value (0.75 s at
160 characters, seconds at 220) inside the coroutine. Now: a 1000-character cap checked when the selector is decoded, the
``regex`` engine in a worker thread with the GIL released, and one time budget per read whose end is a clear error.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

import primer.tap.selector as selector_module
from primer.model.storage import FieldRef, Op, Predicate, Value
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.tap.cursor import TapCursor
from primer.tap.reader import read_batch, read_session_since
from primer.tap.router import WorkspaceTick
from primer.tap.selector import TapSelector
from primer.toolset.workspaces import build_workspaces_toolset

_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
_WID = "ws-1"

# Polynomial under stdlib re: about 3 s on this value with nothing bounding it.
POLY_LIKE = "%a%a%a%a%!"
POLY_VALUE = "a" * 220
# Quadratic in the regex engine too: about 20 s on this value, so only the budget ends it.
SLOW_LIKE = "%a" * 8 + "b"
SLOW_VALUE = "a" * 20_000 + "b!"


def _like(pattern: str, op: Op = Op.LIKE, field: str = "payload.text") -> TapSelector:
    return TapSelector(events=Predicate(left=FieldRef(name=field), op=op, right=Value(value=pattern)))


class _IO:
    state_path = ".state"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def add(self, sid: str, seq: int, kind: str, **payload) -> None:
        path = f".state/sessions/{sid}/messages.jsonl"
        rec = {"seq": seq, "kind": kind, "payload": payload, "created_at": _NOW.isoformat()}
        self.files[path] = self.files.get(path, b"") + (json.dumps(rec) + "\n").encode()

    async def read_file(self, path: str) -> bytes:
        from primer.model.except_ import NotFoundError

        if path not in self.files:
            raise NotFoundError(f"{path!r} not found")
        return self.files[path]


class _Provider:
    def __init__(self) -> None:
        from tests.conftest import _InMemoryStorage

        self.store = _InMemoryStorage(WorkspaceSession)

    def get_storage(self, model_cls):  # noqa: ANN001
        return self.store


def _session(sid: str = "s1") -> WorkspaceSession:
    return WorkspaceSession(
        id=sid, workspace_id=_WID, binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=_NOW, turn_status="idle",
    )


async def _watch_loop(body):
    """(longest pause of a 5 ms ticker while ``body`` ran, its result or exception, seconds it took)."""
    gaps = [0.0]
    state = {"stop": False}

    async def ticker() -> None:
        last = time.perf_counter()
        while not state["stop"]:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)
    started = time.perf_counter()
    try:
        outcome = await body()
    except Exception as exc:  # noqa: BLE001 - returned for the caller to assert on
        outcome = exc
    finally:
        elapsed = time.perf_counter() - started
        state["stop"] = True
        await task
    return max(gaps), outcome, elapsed


@pytest.fixture
def short_budget(monkeypatch):
    monkeypatch.setattr(selector_module, "TAP_MATCH_SECONDS", 0.3, raising=False)


# ---- the cap, checked when a selector is decoded -----------------------------------------------------------------------


@pytest.mark.parametrize("op", [Op.LIKE, Op.ILIKE], ids=["like", "ilike"])
def test_check_selector_refuses_a_like_pattern_over_1000_characters(op):
    from primer.tap.selector import TapSelectorError, check_selector

    check_selector(_like("%" * 1000, op))
    nested = TapSelector(events=Predicate(
        left=Predicate(left=FieldRef(name="class"), op=Op.EQ, right=Value(value="done")),
        op=Op.AND,
        right=_like("x" * 1001, op).events,
    ))
    with pytest.raises(TapSelectorError, match="1001 characters long; the limit is 1000"):
        check_selector(nested)


def test_the_sse_route_answers_400_for_a_like_pattern_over_the_cap():
    from primer.api.routers.tap import _decode_selector

    with pytest.raises(HTTPException) as refused:
        _decode_selector(_like("x" * 1001).model_dump_json())
    assert refused.value.status_code == 400
    assert "the limit is 1000" in refused.value.detail["detail"]
    assert _decode_selector(_like("%needle%").model_dump_json()).events is not None


# ---- matching: off the loop, and bounded --------------------------------------------------------------------------------


async def test_a_polynomial_like_pattern_does_not_stall_the_event_loop():
    io = _IO()
    io.add("s1", 1, "user_input", text=POLY_VALUE)
    io.add("s1", 2, "user_input", text="xaaaa!")
    gap, outcome, elapsed = await _watch_loop(lambda: read_session_since(
        io, workspace_id=_WID, session=_session(), after_seq=0, selector=_like(POLY_LIKE),
    ))
    events, _offset = outcome
    assert [e.seq for e in events] == [2]
    assert elapsed < 1.0
    assert gap < 0.2, gap


async def test_a_read_that_runs_out_of_its_budget_is_a_clear_error(short_budget):
    from primer.tap.selector import TapSelectorTimeout

    io = _IO()
    io.add("s1", 1, "user_input", text=SLOW_VALUE)
    gap, outcome, elapsed = await _watch_loop(lambda: read_session_since(
        io, workspace_id=_WID, session=_session(), after_seq=0, selector=_like(SLOW_LIKE),
    ))
    assert isinstance(outcome, TapSelectorTimeout), outcome
    assert "simplify the selector" in str(outcome)
    assert elapsed < 2.0
    assert gap < 0.2, gap


async def test_a_drain_that_runs_out_of_its_budget_raises_and_keeps_the_cursor(short_budget):
    from primer.tap.selector import TapSelectorTimeout

    provider = _Provider()
    await provider.store.create(_session())
    io = _IO()
    io.add("s1", 1, "user_input", text="fine")
    io.add("s1", 2, "user_input", text=SLOW_VALUE)
    cursor = TapCursor.decode(None)
    with pytest.raises(TapSelectorTimeout):
        await read_batch(provider, io, workspace_id=_WID, selector=_like(SLOW_LIKE), cursor=cursor, limit=10)
    assert cursor.resume_seq("s1") == 0


async def test_a_drain_still_honours_the_limit_and_advances_past_filtered_records():
    provider = _Provider()
    await provider.store.create(_session())
    io = _IO()
    for seq, text in enumerate(["needle 1", "hay", "needle 2", "needle 3"], start=1):
        io.add("s1", seq, "user_input", text=text)
    cursor = TapCursor.decode(None)
    events, cursor = await read_batch(
        provider, io, workspace_id=_WID, selector=_like("needle%"), cursor=cursor, limit=2,
    )
    assert [e.seq for e in events] == [1, 3]
    assert cursor.resume_seq("s1") == 3
    events, cursor = await read_batch(
        provider, io, workspace_id=_WID, selector=_like("NEEDLE%", Op.ILIKE), cursor=cursor, limit=2,
    )
    assert [e.seq for e in events] == [4]


# ---- the two surfaces answer with the cause -----------------------------------------------------------------------------


async def test_the_mcp_tap_tool_refuses_a_long_pattern_and_reports_a_spent_budget(short_budget):
    provider = _Provider()
    await provider.store.create(_session())
    io = _IO()
    io.add("s1", 1, "user_input", text=SLOW_VALUE)

    class _Registry:
        async def get_workspace(self, workspace_id):  # noqa: ANN001
            return io

    ts = build_workspaces_toolset(storage_provider=provider, workspace_registry=_Registry(), tap_router=None)
    too_long = await ts.call(tool_name="workspace_tap", arguments={
        "workspace_id": _WID, "selector": json.loads(_like("x" * 1001).model_dump_json()),
    })
    assert too_long.is_error
    assert json.loads(too_long.output)["type"] == "bad-request"
    assert "the limit is 1000" in json.loads(too_long.output)["message"]

    started = time.perf_counter()
    spent = await ts.call(tool_name="workspace_tap", arguments={
        "workspace_id": _WID, "selector": json.loads(_like(SLOW_LIKE).model_dump_json()),
    })
    assert time.perf_counter() - started < 2.0
    assert spent.is_error
    body = json.loads(spent.output)
    assert body["type"] == "bad-request"
    assert "simplify the selector" in body["message"]


class _Sub:
    def __init__(self, items) -> None:
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._items:
            return self._items.pop(0)
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        pass


class _Router:
    def subscribe(self, workspace_id):  # noqa: ANN001
        return _Sub([WorkspaceTick(session_id="s1", seq=1)])

    def subscribe_delta(self, workspace_id):  # noqa: ANN001
        return _Sub([])


async def test_the_sse_stream_ends_with_a_named_error_frame_when_the_budget_runs_out(short_budget):
    from primer.api.routers.tap import _stream_tap

    provider = _Provider()
    await provider.store.create(_session())
    io = _IO()
    io.add("s1", 1, "user_input", text=SLOW_VALUE)
    frames = []
    stream = _stream_tap(
        router=_Router(), sessions_storage=provider.store, workspace_io=io, workspace_id=_WID,
        selector=_like(SLOW_LIKE), cursor=TapCursor.decode(None), had_cursor=True,
    )
    async with asyncio.timeout(5):
        async for frame in stream:
            frames.append(frame)
    assert len(frames) == 1
    # A long retry: an EventSource reconnects on its own, and the same selector would only fail again (round 2, L4).
    assert frames[0].startswith("retry: 3600000\nevent: error\n")
    data = json.loads(frames[0].split("data: ", 1)[1])
    assert data["type"] == "/errors/selector-timeout"
    assert "simplify the selector" in data["detail"]



# ---- round 2: M3 the drain matches only what the limit asks for; L9 a LIKE compiles once per selector; L1 the executor ----


def _counting_eval(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = selector_module._eval_predicate

    def _counting(pred, event, ctx):
        calls.append(event.seq)
        return real(pred, event, ctx)

    monkeypatch.setattr(selector_module, "_eval_predicate", _counting)
    return calls


@pytest.mark.parametrize("op", [Op.EQ, Op.LIKE], ids=["plain-predicate", "like-off-loop"])
async def test_a_drain_matches_no_more_events_than_its_limit_asks_for(monkeypatch, op):
    provider = _Provider()
    await provider.store.create(_session())
    io = _IO()
    for seq in range(1, 51):  # a backlog of 10x the limit, every event matching
        io.add("s1", seq, "user_input", text="needle")
    calls = _counting_eval(monkeypatch)
    pattern = "needle" if op is Op.EQ else "need%"
    events, cursor = await read_batch(
        provider, io, workspace_id=_WID, selector=_like(pattern, op), cursor=TapCursor.decode(None), limit=5,
    )
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]
    assert len(calls) == 5, calls
    assert cursor.resume_seq("s1") == 5
    # The next drain resumes exactly there.
    events, _cursor = await read_batch(
        provider, io, workspace_id=_WID, selector=_like(pattern, op), cursor=cursor, limit=5,
    )
    assert [e.seq for e in events] == [6, 7, 8, 9, 10]


async def test_a_selector_compiles_each_like_pattern_once(monkeypatch):
    compiles: list[str] = []
    real = selector_module._like_to_regex

    def _counting(pattern, *, flags=0):
        compiles.append(pattern)
        return real(pattern, flags=flags)

    monkeypatch.setattr(selector_module, "_like_to_regex", _counting)
    io = _IO()
    for seq in range(1, 101):
        io.add("s1", seq, "user_input", text=f"needle {seq}")
    selector = _like("needle%")
    events, _offset = await read_session_since(io, workspace_id=_WID, session=_session(), after_seq=0, selector=selector)
    assert len(events) == 100
    assert compiles == ["needle%"]


async def test_like_matching_runs_on_the_user_regex_executor(monkeypatch):
    import threading

    names: list[str] = []
    real = selector_module._like_matches

    def _recording(*args, **kwargs):
        names.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(selector_module, "_like_matches", _recording)
    io = _IO()
    io.add("s1", 1, "user_input", text="needle")
    await read_session_since(io, workspace_id=_WID, session=_session(), after_seq=0, selector=_like("need%"))
    assert names and all(name.startswith("user-regex") for name in names), names
