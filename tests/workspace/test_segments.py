"""The segment module and the ``SessionLog`` reader facade (segmented session log, design D1 to D7, sections 3 and 5)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from primer.model.except_ import NotFoundError
from primer.workspace.segments import (
    LOG_NAME,
    LinesSource,
    SegmentGap,
    SessionLog,
    StateRepoSource,
    WorkspaceSource,
    ordered,
    parse_segment,
    segment_name,
)


def _line(**obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _msg(text: str) -> bytes:
    return _line(role="user", parts=[{"type": "text", "text": text}])


def _record(seq: int, kind: str = "assistant_token") -> bytes:
    return _line(seq=seq, kind=kind, payload={})


class _MemSource:
    """The five primitives over a dict of ``name -> bytes``; counts the full reads so the cache can be asserted."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.reads: list[str] = []
        self.sizes: list[str] = []

    async def list_names(self, dir_rel: str) -> list[str]:
        return list(self.files)

    async def size(self, rel: str) -> int | None:
        self.sizes.append(rel)
        data = self.files.get(rel.rsplit("/", 1)[-1])
        return None if data is None else len(data)

    async def read(self, rel: str) -> bytes | None:
        self.reads.append(rel)
        return self.files.get(rel.rsplit("/", 1)[-1])

    async def read_from(self, rel: str, offset: int) -> bytes:
        return (self.files.get(rel.rsplit("/", 1)[-1]) or b"")[offset:]

    async def read_tail(self, rel: str, max_bytes: int) -> tuple[bytes, bool]:
        data = self.files.get(rel.rsplit("/", 1)[-1]) or b""
        return data[-max_bytes:], len(data) <= max_bytes


def _log(files: dict[str, bytes]) -> tuple[SessionLog, _MemSource]:
    source = _MemSource(files)
    return SessionLog(source, "sessions/s1"), source


def _three() -> dict[str, bytes]:
    return {
        LOG_NAME: _msg("zero") + _record(1),
        "messages.0001.jsonl": _msg("one") + _record(2),
        "messages.0002.jsonl": _msg("two"),
    }


def test_names_round_trip_and_order():
    assert [segment_name(n) for n in (0, 7, 12345)] == ["messages.jsonl", "messages.0007.jsonl", "messages.12345.jsonl"]
    assert [parse_segment(segment_name(n)) for n in (0, 7, 12345)] == [0, 7, 12345]
    names = ["messages.10000.jsonl", "other.jsonl", "messages.jsonl.bak", "messages.9999.jsonl", "messages.jsonl", "messages.12.jsonl"]
    assert ordered(names) == [(0, "messages.jsonl"), (9999, "messages.9999.jsonl"), (10000, "messages.10000.jsonl")]


async def test_gap_raises_segment_gap():
    log, _ = _log({LOG_NAME: b"a\n", "messages.0002.jsonl": b"c\n"})
    with pytest.raises(SegmentGap):
        await log.read_all()
    log, _ = _log({"messages.0001.jsonl": b"b\n"})
    with pytest.raises(SegmentGap):
        await log.segments()


async def test_read_all_is_the_concatenation_and_a_single_file_is_one_segment():
    files = _three()
    log, _ = _log(files)
    assert await log.read_all() == b"".join(files.values())
    assert await log.size() == sum(map(len, files.values()))
    single, _ = _log({LOG_NAME: b"x\ny\n"})
    assert [(s.index, s.name, s.size) for s in await single.segments()] == [(0, LOG_NAME, 4)]
    assert (await single.active()).name == LOG_NAME and await single.read_all() == b"x\ny\n"
    none, _ = _log({})
    assert await none.segments() == [] and await none.active() is None and await none.read_all() == b"" and await none.size() == 0


async def test_read_from_uses_logical_offsets_across_segments():
    files = _three()
    whole = b"".join(files.values())
    log, _ = _log(files)
    first, second = len(files[LOG_NAME]), len(files["messages.0001.jsonl"])
    for offset in (0, 5, first - 1, first, first + 3, first + second, first + second + 4, len(whole) - 1):
        assert await log.read_from(offset) == whole[offset:], offset
    assert await log.read_from(len(whole)) == b""
    assert await log.read_from(len(whole) + 100) == b""


async def test_read_tail_walks_segments_and_reports_whole_only_at_byte_zero():
    files = _three()
    whole = b"".join(files.values())
    log, _ = _log(files)
    assert await log.read_tail(10) == (whole[-10:], False)
    reaches_second = len(files["messages.0002.jsonl"]) + 5
    assert await log.read_tail(reaches_second) == (whole[-reaches_second:], False)
    assert await log.read_tail(len(whole) - 1) == (whole[1:], False)
    assert await log.read_tail(len(whole)) == (whole, True)
    assert await log.read_tail(len(whole) * 3) == (whole, True)
    empty, _ = _log({})
    assert await empty.read_tail(10) == (b"", True)


async def test_since_last_marker_starts_at_the_marker_segment_and_reports_its_offset():
    files = {
        LOG_NAME: _msg("old") + _record(1),
        "messages.0001.jsonl": _msg("older") + _record(2, "compaction_marker") + _msg("kept"),
        "messages.0002.jsonl": _msg("new") + _record(3),
        "messages.0003.jsonl": _msg("newest"),
    }
    log, source = _log(files)
    offset, data = await log.since_last_marker()
    read = [r.rsplit("/", 1)[-1] for r in source.reads]
    assert offset == len(files[LOG_NAME])
    assert data == b"".join(files[n] for n in list(files)[1:])
    assert LOG_NAME not in read, "the segments before the marker are never read"
    assert (await log.read_all())[offset:] == data


async def test_since_last_marker_takes_the_newest_marker_and_no_marker_returns_everything():
    files = {LOG_NAME: _record(1, "compaction_marker"), "messages.0001.jsonl": _record(2, "compaction_marker") + _msg("x")}
    log, _ = _log(files)
    assert (await log.since_last_marker())[0] == len(files[LOG_NAME])
    plain, _ = _log(_three())
    offset, data = await plain.since_last_marker()
    assert offset == 0 and data == await plain.read_all()
    empty, _ = _log({})
    assert await empty.since_last_marker() == (0, b"")


async def test_a_message_that_mentions_a_marker_in_its_text_only_costs_a_wider_read():
    files = {LOG_NAME: _msg("old"), "messages.0001.jsonl": _msg("the word compaction_marker in a user message")}
    log, _ = _log(files)
    offset, data = await log.since_last_marker()
    assert (offset, data) == (len(files[LOG_NAME]), files["messages.0001.jsonl"]), "the substring test is a hint: a wider read is harmless"


async def test_max_event_seq_walks_back_past_a_segment_of_bare_messages():
    files = {LOG_NAME: _msg("a") + _record(4) + _record(9), "messages.0001.jsonl": _msg("b") + _msg("c"), "messages.0002.jsonl": _msg("d")}
    log, _ = _log(files)
    assert await log.max_event_seq() == 9
    newest, _ = _log({LOG_NAME: _record(2), "messages.0001.jsonl": _record(7) + _msg("x")})
    assert await newest.max_event_seq() == 7
    bare, _ = _log({LOG_NAME: _msg("only messages")})
    assert await bare.max_event_seq() == 0
    assert await _log({})[0].max_event_seq() == 0


async def test_an_empty_segment_anywhere_is_accepted():
    for files in (
        {LOG_NAME: b"", "messages.0001.jsonl": _msg("a")},
        {LOG_NAME: _msg("a"), "messages.0001.jsonl": b"", "messages.0002.jsonl": _msg("b")},
        {LOG_NAME: _msg("a"), "messages.0001.jsonl": b""},
    ):
        log, _ = _log(files)
        whole = b"".join(files.values())
        assert await log.read_all() == whole and await log.size() == len(whole)
        assert await log.read_from(0) == whole and await log.read_tail(len(whole)) == (whole, True)
        assert (await log.since_last_marker()) == (0, whole)


async def test_closed_segments_are_cached_by_name_and_size_and_dropped_when_a_read_disagrees():
    files = _three()
    log, source = _log(files)
    await log.size()
    first_stats = len(source.sizes)
    await log.size()
    again = source.sizes[first_stats:]
    assert [r.rsplit("/", 1)[-1] for r in again] == ["messages.0002.jsonl"], "only the active segment is stat'd again"
    other = SessionLog(source, "sessions/s1")
    await other.size()
    assert len(source.sizes) == first_stats + 1 + 1, "the cache is per process, not per facade"
    # a closed segment that changes under the cache is noticed when its bytes are read
    files[LOG_NAME] = files[LOG_NAME] + _msg("grew")
    assert await log.read_all() == b"".join(files.values())
    assert await log.size() == sum(map(len, files.values()))


# ---- the same log through the three adapters ------------------------------------------------------------------------------------------------


def _workspace(files: dict[str, bytes]):
    def full(path: str) -> str:
        return path.rsplit("/", 1)[-1]

    async def list_files(path=".", **_kw):
        return [SimpleNamespace(path=f"{path}/{name}", kind="file", size_bytes=len(data)) for name, data in files.items()] + [
            SimpleNamespace(path=f"{path}/session.json", kind="file", size_bytes=2)
        ]

    async def file_info(path):
        if full(path) not in files:
            raise NotFoundError(path)
        return SimpleNamespace(size_bytes=len(files[full(path)]))

    async def read_file(path):
        if full(path) not in files:
            raise NotFoundError(path)
        return files[full(path)]

    async def read_file_from(path, offset):
        return (await read_file(path))[offset:]

    return SimpleNamespace(list_files=list_files, file_info=file_info, read_file=read_file, read_file_from=read_file_from)


def _repo(files: dict[str, bytes]):
    async def read_state_file(path):
        return files.get(path.rsplit("/", 1)[-1])

    async def stat_state_file(path):
        data = files.get(path.rsplit("/", 1)[-1])
        return None if data is None else (len(data), 0)

    async def list_state_dir(path):
        return [*files, "session.json"]

    return SimpleNamespace(read_state_file=read_state_file, stat_state_file=stat_state_file, list_state_dir=list_state_dir)


def _lines(files: dict[str, bytes]):
    def read_lines(session_id, filename="messages.jsonl"):
        return [ln for ln in files.get(filename, b"").decode().splitlines() if ln.strip()]

    return SimpleNamespace(read_lines=read_lines)


async def test_workspace_state_and_lines_sources_agree():
    files = {
        LOG_NAME: _msg("old") + _record(1),
        "messages.0001.jsonl": _msg("a") + _record(2, "compaction_marker") + _msg("kept"),
        "messages.0002.jsonl": _msg("new") + _record(5),
    }
    whole = b"".join(files.values())
    logs = {
        "workspace": SessionLog(WorkspaceSource(_workspace(files)), ".state/sessions/s1"),
        "state": SessionLog(StateRepoSource(_repo(files)), "sessions/s1"),
        "lines": SessionLog(LinesSource(_lines(files)), "x/s1"),
    }
    for name, log in logs.items():
        assert await log.read_all() == whole, name
        assert await log.size() == len(whole), name
        assert await log.read_from(7) == whole[7:], name
        assert await log.read_tail(20) == (whole[-20:], False), name
        assert await log.max_event_seq() == 5, name
        assert (await log.since_last_marker())[0] == len(files[LOG_NAME]), name
        assert [s.name for s in await log.segments()] == list(files), name


async def test_a_state_repo_without_a_directory_listing_is_probed_and_a_missing_log_is_empty():
    files = _three()
    repo = _repo(files)
    del repo.list_state_dir
    assert [s.name for s in await SessionLog(StateRepoSource(repo), "sessions/s1").segments()] == list(files)
    assert await SessionLog(StateRepoSource(_repo({})), "sessions/s1").read_all() == b""
    assert await SessionLog(WorkspaceSource(_workspace({})), ".state/sessions/s1").size() == 0
