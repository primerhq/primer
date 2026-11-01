"""Task 14 stage 1: the session's cached view of messages.jsonl equals a full parse, and reads/parses only what is new."""

from __future__ import annotations

import json

import pytest

from primer.model.chat import Message, TextPart
from primer.workspace.message_log import MessageLogCache, MessageLogMismatch
from primer.workspace.session import last_compaction_state, reconstruct_compacted_history


def msg(role: str, text: str) -> str:
    return Message(role=role, parts=[TextPart(text=text)]).model_dump_json() + "\n"


def rec(seq: int, kind: str, payload: dict | None = None) -> str:
    return json.dumps({"seq": seq, "kind": kind, "payload": payload or {}, "created_at": "2026-11-01T00:00:00+00:00"}) + "\n"


def marker(seq: int, summary: str, tokens_after: int = 10) -> str:
    return rec(seq, "compaction_marker", {"summary": summary, "tokens_after": tokens_after})


class Disk:
    """A file the cache reads through, counting reads and stats."""

    def __init__(self, data: bytes = b"", stat: bool = True):
        self.data, self.reads, self.stats, self.has_stat, self.mtime = data, 0, 0, stat, 0

    def append(self, text: str) -> None:
        self.data += text.encode()
        self.mtime += 1

    def set(self, data: bytes) -> None:
        self.data, self.mtime = data, self.mtime + 1

    async def read(self):
        self.reads += 1
        return self.data or None

    async def stat(self):
        self.stats += 1
        return (len(self.data), self.mtime) if self.data else None


async def view(cache, disk):
    return await cache.view(disk.read, disk.stat if disk.has_stat else None)


def full(disk):
    lines = disk.data.decode().splitlines()
    return reconstruct_compacted_history(lines), last_compaction_state(lines)


async def test_continued_views_equal_a_full_parse_through_markers_and_rewinds():
    disk, cache = Disk(), MessageLogCache(verify=True)
    steps = [
        msg("user", "a") + msg("assistant", "b") + rec(1, "assistant_token"),
        msg("user", "c"),
        marker(2, "SUM1") + msg("user", "after"),
        rec(3, "compaction_note", {"outcome": "skipped", "reason": "small"}),
        marker(4, "SUM2", 20) + msg("assistant", "x"),
        rec(5, "rewind_marker", {"to_seq": 3}),
        msg("user", "tail"),
    ]
    for step in steps:
        disk.append(step)
        got = await view(cache, disk)
        history, compaction = full(disk)
        assert [m.model_dump() for m in got.history] == [m.model_dump() for m in history] and got.compaction == compaction


async def test_an_unchanged_file_costs_a_stat_and_no_read_and_no_parse():
    disk, cache = Disk(), MessageLogCache(verify=False)
    disk.append(msg("user", "a") + rec(7, "assistant_token"))
    first = await view(cache, disk)
    parsed = cache.parsed_bytes
    for _ in range(5):
        again = await view(cache, disk)
    assert disk.reads == 1 and cache.parsed_bytes == parsed and again.max_seq == first.max_seq == 7


async def test_only_the_appended_bytes_are_parsed():
    disk, cache = Disk(), MessageLogCache(verify=False)
    disk.append("".join(msg("user", f"line {i}") for i in range(200)))
    await view(cache, disk)
    base = cache.parsed_bytes
    new = msg("assistant", "new") + rec(9, "done")
    disk.append(new)
    got = await view(cache, disk)
    assert cache.parsed_bytes - base == len(new.encode()) and len(got.history) == 201 and got.max_seq == 9


async def test_a_backend_without_stat_still_skips_the_parse_of_old_lines():
    disk, cache = Disk(stat=False), MessageLogCache(verify=False)
    disk.append("".join(msg("user", f"line {i}") for i in range(50)))
    await view(cache, disk)
    base = cache.parsed_bytes
    await view(cache, disk)
    assert disk.reads == 2 and cache.parsed_bytes == base


async def test_a_file_that_shrank_or_changed_before_the_offset_is_parsed_afresh():
    disk, cache = Disk(), MessageLogCache(verify=True)
    disk.append(msg("user", "one") + msg("assistant", "two") + msg("user", "three"))
    await view(cache, disk)
    disk.set((msg("user", "ONE") + msg("assistant", "two") + msg("user", "three") + msg("user", "four")).encode())   # same length prefix, different bytes
    got = await view(cache, disk)
    assert [m.parts[0].text for m in got.history] == ["ONE", "two", "three", "four"]
    disk.set(msg("user", "only").encode())
    assert [m.parts[0].text for m in (await view(cache, disk)).history] == ["only"]


async def test_a_partial_last_line_is_answered_but_not_kept():
    disk, cache = Disk(), MessageLogCache(verify=True)
    disk.append(msg("user", "a"))
    await view(cache, disk)
    disk.append(msg("assistant", "b").rstrip("\n"))          # a line still being written
    got = await view(cache, disk)
    assert [m.role for m in got.history] == ["user", "assistant"]
    disk.append("\n" + msg("user", "c"))
    assert [m.role for m in (await view(cache, disk)).history] == ["user", "assistant", "user"]


async def test_verify_mode_raises_when_the_cache_was_corrupted():
    disk, cache = Disk(), MessageLogCache(verify=True)
    disk.append(msg("user", "a") + msg("assistant", "b"))
    await view(cache, disk)
    cache._history.carried.pop()                               # a bug in the cache
    disk.append(msg("user", "c"))
    with pytest.raises(MessageLogMismatch):
        await view(cache, disk)


async def test_an_absent_file_is_an_empty_view():
    disk, cache = Disk(), MessageLogCache(verify=True)
    got = await view(cache, disk)
    assert got.history == [] and got.size == 0 and got.max_seq == 0
