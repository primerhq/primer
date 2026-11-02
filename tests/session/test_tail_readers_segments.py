"""The tail and window readers over a segmented log (seglog Task 4): the relay's final text and the open-turn check cross segment boundaries, and read only the end."""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from primer.channel.session_relay import read_session_final_text
from primer.model.except_ import NotFoundError
from primer.session.dispatch import _read_message_lines
from primer.session.turns import count_turn_state
from primer.workspace.segments import segment_name
from tests._support.ranged_log import blob, log, rec
from tests._support.segmented_log import boundaries, split_log

SID = "s1"
SLOT = f".state/sessions/{SID}"


class SegWs:
    """A workspace double that serves a session's segments and counts the bytes handed out per file."""

    state_path = ".state"

    def __init__(self, parts: list[bytes]) -> None:
        self.files = {f"{SLOT}/{segment_name(n)}": p for n, p in enumerate(parts)}
        self.read_by_file: dict[str, int] = {}

    @property
    def bytes_read(self) -> int:
        return sum(self.read_by_file.values())

    def _get(self, path: str) -> bytes:
        if path not in self.files:
            raise NotFoundError(path)
        return self.files[path]

    def _count(self, path: str, n: int) -> None:
        self.read_by_file[path] = self.read_by_file.get(path, 0) + n

    async def list_files(self, path: str):
        return [SimpleNamespace(path=p) for p in self.files if p.rsplit("/", 1)[0] == path]

    async def file_info(self, path: str):
        return SimpleNamespace(size_bytes=len(self._get(path)), modified_at=1)

    async def read_file(self, path: str) -> bytes:
        data = self._get(path)
        self._count(path, len(data))
        return data

    async def read_file_from(self, path: str, offset: int) -> bytes:
        data = self._get(path)[offset:]
        self._count(path, len(data))
        return data

    async def read_file_tail(self, path: str, max_bytes: int):
        data = self._get(path)
        tail = data[-max_bytes:]
        self._count(path, len(tail))
        return tail, len(tail) == len(data)


class SegShim:
    """The pool's IO shim read surface over one SegWs (state-relative paths)."""

    def __init__(self, ws: SegWs) -> None:
        self.ws = ws

    def _full(self, rel: str) -> str:
        return f".state/{rel}"

    async def list_state_dir(self, workspace_id, rel):
        return [p.rsplit("/", 1)[1] for p in self.ws.files if p.rsplit("/", 1)[0] == self._full(rel)]

    async def state_size(self, workspace_id, rel):
        try:
            return (await self.ws.file_info(self._full(rel))).size_bytes
        except NotFoundError:
            return None

    async def read_state_file(self, workspace_id, rel):
        return await self.ws.read_file(self._full(rel))

    async def read_state_from(self, workspace_id, rel, offset):
        return await self.ws.read_file_from(self._full(rel), offset)

    async def read_state_tail(self, workspace_id, rel, max_bytes):
        return await self.ws.read_file_tail(self._full(rel), max_bytes)


def _row(cursor: int):
    return SimpleNamespace(id=SID, workspace_id="w", next_unprocessed_seq=cursor)


def _cut(data: bytes, rng: random.Random, pieces: int) -> list[bytes]:
    points = sorted(rng.sample(boundaries(data), min(pieces - 1, len(boundaries(data)))))
    return split_log(data, points)


@pytest.mark.parametrize("seed", range(12))
async def test_final_text_when_the_last_turn_straddles_a_rotation(seed):
    rng = random.Random(seed)
    data = blob(log(rng.randint(1, 6), open_last=bool(seed % 3 == 0))[0])
    whole = await read_session_final_text(SegWs([data]), SID)
    parts = _cut(data, rng, rng.randint(2, 8))
    assert await read_session_final_text(SegWs(parts), SID) == whole


async def test_the_final_text_of_a_long_segmented_log_reads_only_the_end():
    closed = blob(log(3000, pad=100)[0])
    active = blob(log(3, start_seq=100_000)[0])
    ws = SegWs([closed, active])
    assert await read_session_final_text(ws, SID) is not None
    assert len(closed) > 500_000 and ws.bytes_read <= 64 * 1024, "one first tail window, across the boundary, and no more"


async def test_the_final_text_window_widens_into_the_previous_segment():
    lines, seq = log(4)
    data = blob(lines)
    parts = split_log(data, [boundaries(data)[-2]])    # the last answer's closing records alone in the active segment
    assert await read_session_final_text(SegWs(parts), SID) == await read_session_final_text(SegWs([data]), SID)


@pytest.mark.parametrize("open_last", [False, True])
async def test_open_turn_detection_across_a_boundary(open_last):
    lines, end = log(60, open_last=open_last)
    data = blob(lines)
    rng = random.Random(7)
    for cursor in (0, 1, 30, end - 8, end - 2, end - 1, end):
        want = count_turn_state(await _read_message_lines(SegShim(SegWs([data])), _row(cursor)), cursor=cursor)
        for pieces in (2, 5, 20):
            got = await _read_message_lines(SegShim(SegWs(_cut(data, rng, pieces))), _row(cursor))
            assert count_turn_state(got, cursor=cursor) == want, (open_last, cursor, pieces)


async def test_a_cursor_below_the_active_segments_first_seq_widens_into_the_previous_one():
    lines, end = log(500, pad=200, open_last=True)
    data = blob(lines)
    half = [b for b in boundaries(data) if b > len(data) // 2][0]
    ws = SegWs(split_log(data, [half]))
    got = await _read_message_lines(SegShim(ws), _row(10))           # the cursor is in the closed segment
    assert count_turn_state(got, cursor=10) == count_turn_state(await _read_message_lines(SegShim(SegWs([data])), _row(10)), cursor=10)


async def test_a_workspace_that_cannot_list_is_read_as_one_file():
    class Plain:
        state_path = ".state"

        async def read_file(self, path):
            assert path == f"{SLOT}/messages.jsonl"
            return blob(log(2)[0])

    assert await read_session_final_text(Plain(), SID) is not None
