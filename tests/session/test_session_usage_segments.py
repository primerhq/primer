"""Seglog Task 5: the usage fold and its cache over a segmented log (design section 5.2, Review Focus 2).

``fold_usage`` keeps a fold of the log up to a LOGICAL offset (into ``concat(segments)``) and continues it with what was appended since; the
session detail's totals are cached per log identity. A rotation adds a segment and leaves segment 0's size and time as they were, so both
must see it: the continued fold reads across the boundary, and the identity changes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.model.except_ import NotFoundError
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.usage import session_usage
from primer.session.usage_cache import UsageCache, fold_usage
from primer.workspace.segments import segment_name, session_log_for
from tests._support.ranged_log import blob, log, rec, turn
from tests._support.segmented_io import ListsFiles

SID = "s-use"
SLOT = f".state/sessions/{SID}"
_NOW = datetime(2026, 11, 2, tzinfo=UTC)


class _Ws(ListsFiles):
    """A session log as segments; counts the bytes it hands out and the reads."""

    state_path = ".state"

    def __init__(self, *parts: bytes) -> None:
        self._files = {f"{SLOT}/{segment_name(n)}": part for n, part in enumerate(parts)}
        self.mtime = _NOW
        self.bytes_read = self.reads = 0

    def rotate(self, part: bytes) -> None:
        self._files[f"{SLOT}/{segment_name(len(self._files))}"] = part

    def grow(self, part: bytes) -> None:
        active = f"{SLOT}/{segment_name(len(self._files) - 1)}"
        self._files[active] += part

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            raise NotFoundError(path)
        self.reads += 1
        self.bytes_read += len(self._files[path])
        return self._files[path]

    async def read_file_from(self, path: str, offset: int) -> bytes:
        if path not in self._files:
            raise NotFoundError(path)
        self.reads += 1
        self.bytes_read += len(self._files[path]) - offset
        return self._files[path][offset:]


@pytest.mark.asyncio
async def test_usage_totals_after_a_rotation_equal_a_fresh_fold():
    lines, seq = log(40, pad=100)
    ws = _Ws(blob(lines))
    cache = UsageCache()
    assert await fold_usage(session_log_for(ws, SID), cache, "k") == session_usage(lines)

    for n in range(3):
        got, seq = turn(seq, 100 + n)
        more, seq = turn(seq, 200 + n)
        ws.grow(blob(got))                                     # the active segment grows, then the next batch rotates
        ws.rotate(blob(more))
        lines += got + more
        ws.bytes_read = 0
        continued = await fold_usage(session_log_for(ws, SID), cache, "k")
        assert continued == session_usage(lines) == await fold_usage(session_log_for(ws, SID), UsageCache(), "fresh")
        assert continued.turns == 40 + 2 * (n + 1)
        ws.bytes_read = 0
        await fold_usage(session_log_for(ws, SID), cache, "k")
        assert ws.bytes_read < 2048, f"the continued fold read {ws.bytes_read} bytes: it must start at its logical offset, not at byte 0"


@pytest.mark.asyncio
async def test_usage_fold_of_a_segmented_log_with_a_rewind_is_the_whole_fold():
    """A rewind marker cannot be continued: the fold reads every segment again, never segment 0 alone."""
    lines, seq = log(6)
    rewind = [*turn(seq, 50)[0][:2]]
    ws = _Ws(blob(lines), blob(rewind))
    cache = UsageCache()
    await fold_usage(session_log_for(ws, SID), cache, "k")
    marker = rec(seq + 5, "rewind_marker", {"to_seq": 4})
    ws.rotate(blob([marker]))
    want = session_usage([*lines, *rewind, marker])
    assert await fold_usage(session_log_for(ws, SID), cache, "k") == want
    ws.rotate(blob(turn(seq + 6, 51)[0]))
    assert await fold_usage(session_log_for(ws, SID), cache, "k") == session_usage([*lines, *rewind, marker, *turn(seq + 6, 51)[0]])


@pytest.mark.asyncio
async def test_usage_fold_of_a_log_with_no_segment_is_not_found():
    with pytest.raises(NotFoundError):
        await fold_usage(session_log_for(_Ws(), SID), UsageCache(), "k")


class _Registry:
    def __init__(self, ws) -> None:
        self.ws = ws

    async def get_workspace(self, _wid):
        return self.ws


@pytest.fixture
def _fresh_cache():
    from primer.api.routers import sessions as sessions_router

    sessions_router.USAGE_CACHE.clear()
    yield
    sessions_router.USAGE_CACHE.clear()


@pytest.mark.asyncio
async def test_usage_cache_key_changes_on_rotation(_fresh_cache):
    """Segment 0 keeps its size and time and the row has not moved: only the new segment's name tells the cache that the log changed."""
    from primer.api.routers.sessions import _session_usage_totals

    lines, seq = log(3)
    ws = _Ws(blob(lines))
    row = WorkspaceSession(id=SID, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="a"), status=SessionStatus.RUNNING, created_at=_NOW)
    first = await _session_usage_totals(row, _Registry(ws))
    reads = ws.reads
    assert first["turns"] == 3
    assert await _session_usage_totals(row, _Registry(ws)) == first and ws.reads == reads, "an unchanged log is served from the cache"

    more, _ = log(2, start_seq=seq)
    ws.rotate(blob(more))
    after = await _session_usage_totals(row, _Registry(ws))
    assert after["turns"] == 5 and after["total_input_tokens"] == 50
