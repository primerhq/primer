"""Seglog Task 5: the routes that read a session's log answer on a segmented log (``messages.jsonl``, ``messages.0001.jsonl`` ...) exactly as on the
single file: the transcript page, the turn timeline, the usage totals, the manual compaction and the rewind (design sections 5.2, 6 and 8.3).

Each test serves the same log twice from one fake workspace, once as one file and once cut into segments, and compares what the route answers.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from primer.model.except_ import NotFoundError
from primer.model.workspace_session import WorkspaceSession
from primer.workspace.segments import LOG_NAME, ordered, segment_name
from tests._support.ranged_log import log as usage_log
from tests._support.segmented_io import ListsFiles
from tests._support.segmented_log import PLANS, full_run, split_log

_NOW = datetime(2026, 11, 2, 10, 0, 0, tzinfo=UTC)


class _SegmentedWorkspace(ListsFiles):
    """Session logs as segments: the listing, sizes and reads the log facade makes, the rewind route's event-row append (to the active segment),
    and a read count per path."""

    state_path = ".state"
    id = "ws-1"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self.reads: Counter[str] = Counter()
        self.mtime = _NOW

    @staticmethod
    def slot(sid: str) -> str:
        return f".state/sessions/{sid}"

    def install(self, sid: str, parts: list[bytes]) -> None:
        for n, part in enumerate(parts):
            self._files[f"{self.slot(sid)}/{segment_name(n)}"] = part

    def rotate(self, sid: str, part: bytes) -> None:
        """What a rotating commit does: the batch starts the next segment, the closed ones keep their bytes."""
        self._files[f"{self.slot(sid)}/{segment_name(len(self.names(sid)))}"] = part

    def names(self, sid: str) -> list[str]:
        return [name for _, name in ordered(p.rsplit("/", 1)[-1] for p in self._files if p.startswith(self.slot(sid) + "/"))]

    def log(self, sid: str) -> bytes:
        return b"".join(self._files[f"{self.slot(sid)}/{name}"] for name in self.names(sid))

    async def read_file(self, path: str) -> bytes:
        self.reads[path] += 1
        if path not in self._files:
            raise NotFoundError(f"{path!r} not found")
        return self._files[path]

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        names = self.names(session_id) or [LOG_NAME]
        path = f"{self.slot(session_id)}/{names[-1]}"
        self._files[path] = self._files.get(path, b"") + line


def _rec(seq: int, kind: str, **payload) -> str:
    return json.dumps({"seq": seq, "kind": kind, "payload": payload, "created_at": "2026-11-02T00:00:00+00:00"})


def _msg(role: str, text: str) -> str:
    return json.dumps({"role": role, "parts": [{"type": "text", "text": text}]})


def _blob(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def _max_seq(raw: bytes) -> int:
    return max((json.loads(line).get("seq", 0) for line in raw.decode().splitlines() if line.strip()), default=0)


async def _seed(fake_storage_provider, sid: str, raw: bytes, **over) -> None:
    from primer.model.workspace_session import AgentSessionBinding, SessionStatus

    fields = {
        "id": sid, "workspace_id": "ws-1", "binding": AgentSessionBinding(agent_id="ag1"), "status": SessionStatus.WAITING,
        "created_at": _NOW, "turn_status": "idle", "last_seq": _max_seq(raw),
    }
    fields.update(over)
    await fake_storage_provider.get_storage(WorkspaceSession).create(WorkspaceSession(**fields))


def _wire(app, ws) -> None:
    async def _get(wid):
        return ws if wid == "ws-1" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]


async def _one_and_split(app, fake_storage_provider, raw: bytes, plan: str, **over) -> _SegmentedWorkspace:
    """Session ``s-one`` holds ``raw`` as one file, ``s-seg`` the same bytes cut by ``plan``."""
    ws = _SegmentedWorkspace()
    parts = split_log(raw, PLANS[plan](raw))
    assert len(parts) > 1, "premise: the plan cuts this log"
    ws.install("s-one", [raw])
    ws.install("s-seg", parts)
    await _seed(fake_storage_provider, "s-one", raw, **over)
    await _seed(fake_storage_provider, "s-seg", raw, **over)
    _wire(app, ws)
    return ws


# --- the transcript page ------------------------------------------------------------------------------------------------------------------------

_QUERIES = [
    "", "?limit=3", "?limit=3&offset=4", "?after_seq=9", "?after_seq=5&limit=2&offset=1", "?tail=true&limit=4", "?tail=true&limit=4&offset=4",
    "?visible=true", "?visible=true&tail=true&limit=3", "?visible=true&after_seq=10",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["every_record", "around_marker", "tiny_tail"])
async def test_messages_route_pages_a_segmented_log_like_a_single_file(client: httpx.AsyncClient, app, fake_storage_provider, plan) -> None:
    await _one_and_split(app, fake_storage_provider, full_run(), plan)
    for query in _QUERIES:
        one = await client.get(f"/v1/sessions/s-one/messages{query}")
        seg = await client.get(f"/v1/sessions/s-seg/messages{query}")
        assert one.status_code == seg.status_code == 200, (query, seg.text)
        assert seg.json() == one.json(), query
    assert (await client.get("/v1/sessions/s-seg/messages")).json()["total"] > 20, "premise: the whole log, not segment 0"


@pytest.mark.asyncio
async def test_segmented_unreadable_workspace_still_503s_not_empty(client: httpx.AsyncClient, app, fake_storage_provider) -> None:
    """The ``_read_log_bytes`` contract over every segment: a listing or a segment read that fails in transport is a 503, never an empty or a
    partial page."""

    class _ListingDown(_SegmentedWorkspace):
        async def list_files(self, path: str = ".", **_kw):
            raise ConnectionError("the runtime did not answer")

    class _SegmentDown(_SegmentedWorkspace):
        async def read_file(self, path: str) -> bytes:
            if path.endswith(segment_name(1)):
                raise OSError("segment 1 cannot be read")
            return await super().read_file(path)

    raw = full_run()
    await _seed(fake_storage_provider, "s-down", raw)
    for ws in (_ListingDown(), _SegmentDown()):
        ws.install("s-down", split_log(raw, PLANS["half"](raw)))
        _wire(app, ws)
        for url in ("/v1/sessions/s-down/messages", "/v1/sessions/s-down/turns/0/timeline"):
            r = await client.get(url)
            assert r.status_code == 503, (type(ws).__name__, url, r.status_code, r.text)
            assert r.json()["type"] == "/errors/workspace-unreachable"


# --- the turn timeline --------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["every_record", "after_every_terminal"])
async def test_segmented_timeline_equals_single_file(client: httpx.AsyncClient, app, fake_storage_provider, plan) -> None:
    from primer.api.routers import sessions as sessions_router

    sessions_router.TIMELINE_CACHE._entries.clear()
    await _one_and_split(app, fake_storage_provider, full_run(), plan)
    found = 0
    for turn in range(7):
        one = await client.get(f"/v1/sessions/s-one/turns/{turn}/timeline")
        seg = await client.get(f"/v1/sessions/s-seg/turns/{turn}/timeline")
        assert one.status_code == seg.status_code, (turn, seg.text)
        if one.status_code == 200:
            found += 1
            assert {**seg.json(), "session_id": "-"} == {**one.json(), "session_id": "-"}, turn
    assert found >= 5, "premise: the turns of every segment are addressable"


@pytest.mark.asyncio
async def test_segmented_timeline_cache_key_changes_on_rotation(client: httpx.AsyncClient, app, fake_storage_provider) -> None:
    """A rotation keeps segment 0's size and time: the key must see the new segment, or the next turn is a stale 404 (Review Focus 2)."""
    from primer.api.routers import sessions as sessions_router

    sessions_router.TIMELINE_CACHE._entries.clear()
    first = _blob(_rec(1, "user_input", text="one"), _rec(2, "done", stop_reason="stop"))
    ws = _SegmentedWorkspace()
    ws.install("s-rot", [first])
    ws._files[".state/sessions/s-rot/turns.jsonl"] = b""
    await _seed(fake_storage_provider, "s-rot", first)
    _wire(app, ws)
    assert (await client.get("/v1/sessions/s-rot/turns/0/timeline")).status_code == 200
    assert (await client.get("/v1/sessions/s-rot/turns/1/timeline")).status_code == 404

    ws.rotate("s-rot", _blob(_rec(3, "user_input", text="two"), _rec(4, "done", stop_reason="stop")))    # the row has not moved yet
    r = await client.get("/v1/sessions/s-rot/turns/1/timeline")
    assert r.status_code == 200, r.text
    assert r.json()["terminal_seq"] == 4


# --- the usage totals ---------------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def _fresh_usage_cache():
    from primer.api.routers import sessions as sessions_router

    sessions_router.USAGE_CACHE.clear()
    yield
    sessions_router.USAGE_CACHE.clear()


async def _usage(client: httpx.AsyncClient, sid: str) -> dict:
    r = await client.get(f"/v1/sessions/{sid}")
    assert r.status_code == 200, r.text
    assert r.json()["usage"] is not None, "the usage fold failed"
    return r.json()["usage"]


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["every_record", "half", "tiny_tail"])
async def test_segmented_usage_totals_equal_the_single_file(client: httpx.AsyncClient, app, fake_storage_provider, _fresh_usage_cache, plan) -> None:
    lines, _ = usage_log(5)
    raw = _blob(*lines)
    await _one_and_split(app, fake_storage_provider, raw, plan)
    one = await _usage(client, "s-one")
    assert one["turns"] == 5 and one["total_input_tokens"] == 50
    assert await _usage(client, "s-seg") == one


@pytest.mark.asyncio
async def test_segmented_usage_totals_see_a_rotation(client: httpx.AsyncClient, app, fake_storage_provider, _fresh_usage_cache) -> None:
    """The detail read after a rotation equals a fresh fold: the key sees the new segment although segment 0 kept its size and time."""
    lines, seq = usage_log(3)
    raw = _blob(*lines)
    ws = _SegmentedWorkspace()
    ws.install("s-rot", [raw])
    await _seed(fake_storage_provider, "s-rot", raw)
    _wire(app, ws)
    assert (await _usage(client, "s-rot"))["turns"] == 3

    more, _ = usage_log(2, start_seq=seq)
    ws.rotate("s-rot", _blob(*more))                                  # the row has not moved yet
    after = await _usage(client, "s-rot")
    assert after["turns"] == 5 and after["total_input_tokens"] == 50


# --- the manual compaction ----------------------------------------------------------------------------------------------------------------------


def _compacted_log() -> list[bytes]:
    """Two turns, then (segment 1) a compaction marker and a third turn, then (segment 2, the active one) a fourth."""
    def turn(seq: int, n: str) -> list[str]:
        return [_rec(seq, "user_input", text=f"question {n}"), _msg("user", f"question {n}"), _rec(seq + 1, "assistant_token", text=f"answer {n}"),
                _msg("assistant", f"answer {n}"), _rec(seq + 2, "done", stop_reason="stop")]

    kept = [{"role": "user", "parts": [{"type": "text", "text": "question two"}]}, {"role": "assistant", "parts": [{"type": "text", "text": "answer two"}]}]
    return [
        _blob(*turn(1, "one"), *turn(4, "two")),
        _blob(_rec(7, "compaction_marker", summary="SUMMARY OF ONE", kept_tail_messages=kept), *turn(8, "three")),
        _blob(*turn(11, "four")),
    ]


async def _compact(app, client, fake_storage_provider, monkeypatch, layout: list[bytes]) -> tuple[_SegmentedWorkspace, list]:
    """The manual compaction of session ``j-1`` holding ``layout``: the workspace after it and what the summariser was given."""
    from tests.api.test_session_compact import _journey, _StubLLM

    llm = _StubLLM("the story so far")
    await _journey(app, fake_storage_provider, monkeypatch, llm=llm)            # the agent, the model and the session row
    ws = _SegmentedWorkspace()
    ws.install("j-1", layout)
    _wire(app, ws)
    r = await client.post("/v1/workspaces/ws-1/sessions/j-1/compact")
    assert r.status_code == 200, r.text
    await fake_storage_provider.get_storage(WorkspaceSession).delete("j-1")
    return ws, [m.model_dump(mode="json") for m in llm.calls[0]["messages"]]


@pytest.mark.asyncio
async def test_compaction_route_reads_from_the_last_marker_segment(client: httpx.AsyncClient, app, fake_storage_provider, monkeypatch) -> None:
    """The route folds the history from the segment that holds the newest marker on (everything before it is in the marker): the summariser
    gets what it gets from the single file, segment 0 is never read, and the new marker lands in the active segment."""
    parts = _compacted_log()
    _, single = await _compact(app, client, fake_storage_provider, monkeypatch, [b"".join(parts)])
    ws, segmented = await _compact(app, client, fake_storage_provider, monkeypatch, parts)
    assert segmented == single
    assert "SUMMARY OF ONE" in json.dumps(segmented), "premise: the summariser was given the history from the marker on"
    assert ws.reads[f"{ws.slot('j-1')}/{segment_name(0)}"] == 0, "segment 0 is before the marker: never read"
    assert ws.log("j-1").startswith(b"".join(parts)) and b"compaction_marker" in ws._files[f"{ws.slot('j-1')}/{segment_name(2)}"]


# --- the rewind ---------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rewind_route_accepts_a_target_in_an_earlier_segment_after_the_marker(client: httpx.AsyncClient, app, fake_storage_provider) -> None:
    """The target (seq 4) is in segment 1, after the marker in segment 0; the active segment holds neither. Only the whole log knows it is legal:
    segment 0 alone does not hold seq 4, and the active segment alone does not either."""
    parts = [
        _blob(_rec(1, "user_input", text="first"), _rec(2, "done"), _rec(3, "compaction_marker", summary="so far")),
        _blob(_rec(4, "user_input", text="second"), _rec(5, "done")),
        _blob(_rec(6, "user_input", text="third"), _rec(7, "done")),
    ]
    ws = _SegmentedWorkspace()
    ws.install("s-ok", parts)
    await _seed(fake_storage_provider, "s-ok", b"".join(parts))
    _wire(app, ws)

    r = await client.post("/v1/workspaces/ws-1/sessions/s-ok/rewind", json={"to_seq": 4})
    assert r.status_code == 200, r.text
    assert r.json()["marker_seq"] == 8
    assert [ws._files[f"{ws.slot('s-ok')}/{segment_name(n)}"] for n in (0, 1)] == parts[:2], "closed segments are never written"
    marker = json.loads(ws._files[f"{ws.slot('s-ok')}/{segment_name(2)}"].decode().splitlines()[-1])
    assert marker["kind"] == "rewind_marker" and marker["payload"]["to_seq"] == 4, "the marker is appended to the active segment"


@pytest.mark.asyncio
async def test_rewind_route_refuses_a_target_behind_a_marker_in_an_earlier_segment(client: httpx.AsyncClient, app, fake_storage_provider) -> None:
    """The marker (seq 5) is in segment 1, the target (seq 3) in segment 0: 409, nothing written. Segment 0 alone would accept the target."""
    parts = [
        _blob(_rec(1, "user_input", text="first"), _rec(2, "done"), _rec(3, "user_input", text="second"), _rec(4, "done")),
        _blob(_rec(5, "compaction_marker", summary="so far"), _rec(6, "user_input", text="third"), _rec(7, "done")),
        _blob(_rec(8, "user_input", text="fourth"), _rec(9, "done")),
    ]
    ws = _SegmentedWorkspace()
    ws.install("s-no", parts)
    await _seed(fake_storage_provider, "s-no", b"".join(parts))
    _wire(app, ws)

    r = await client.post("/v1/workspaces/ws-1/sessions/s-no/rewind", json={"to_seq": 3})
    assert r.status_code == 409, r.text
    assert ws.log("s-no") == b"".join(parts), "nothing written"
