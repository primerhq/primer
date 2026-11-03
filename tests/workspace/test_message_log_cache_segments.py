"""The message log cache over a segmented log (seglog Task 3): continues into new segments, starts a cold read at the newest marker's segment."""

from __future__ import annotations

import json

from primer.workspace.message_log import MessageLogCache
from primer.workspace.segments import SessionLog, StateRepoSource, segment_name
from primer.workspace.session import reconstruct_compacted_history

SLOT = "sessions/s1"


class Repo:
    """A state repo over a dict of files; it can stat and list (the local repo's shape), and counts the bytes it reads."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.read_bytes = 0

    async def read_state_file(self, path):
        data = self.files.get(path)
        self.read_bytes += len(data or b"")
        return data

    async def stat_state_file(self, path):
        data = self.files.get(path)
        return None if data is None else (len(data), 0)

    async def list_state_dir(self, path):
        return [p.rsplit("/", 1)[1] for p in self.files if p.rsplit("/", 1)[0] == path]


def msg(role, text):
    return json.dumps({"role": role, "parts": [{"type": "text", "text": text}]}) + "\n"


def rec(seq, kind, payload=None):
    return json.dumps({"seq": seq, "kind": kind, "payload": payload or {}, "created_at": "2026-11-01T12:00:00+00:00"}) + "\n"


def put(repo, n, text, append=False):
    key = f"{SLOT}/{segment_name(n)}"
    repo.files[key] = (repo.files.get(key, b"") if append else b"") + text.encode()


def log_of(repo):
    return SessionLog(StateRepoSource(repo), SLOT)


def texts(view):
    return [m.parts[0].text for m in view.history]


async def test_the_cache_continues_into_a_new_segment_without_rereading_closed_ones():
    repo, cache = Repo(), MessageLogCache(verify=False)
    put(repo, 0, msg("user", "a") + msg("assistant", "b"))
    first = await cache.view_log(log_of(repo))
    assert texts(first) == ["a", "b"]
    put(repo, 1, msg("user", "c"))                       # a rotation: the next segment appears
    repo.read_bytes = 0
    second = await cache.view_log(log_of(repo))
    assert texts(second) == ["a", "b", "c"] and second.size == sum(len(v) for v in repo.files.values())
    put(repo, 1, msg("assistant", "d"), append=True)
    repo.read_bytes = 0
    third = await cache.view_log(log_of(repo))
    assert texts(third) == ["a", "b", "c", "d"]
    assert repo.read_bytes <= len(repo.files[f"{SLOT}/{segment_name(1)}"]) + 512, "only the active segment (and a guard window) is read"


async def test_an_unchanged_log_is_not_read_again():
    repo, cache = Repo(), MessageLogCache(verify=False)
    put(repo, 0, msg("user", "a"))
    put(repo, 1, msg("assistant", "b"))
    await cache.view_log(log_of(repo))
    repo.read_bytes = 0
    await cache.view_log(log_of(repo))
    assert repo.read_bytes == 0


async def test_a_cold_read_starts_at_the_segment_of_the_newest_marker():
    repo = Repo()
    put(repo, 0, msg("user", "old") + msg("assistant", "older"))
    put(repo, 1, msg("user", "mid") + rec(7, "compaction_marker", {"summary": "SUM", "kept_tail_messages": [], "tokens_after": 50}))
    put(repo, 2, msg("user", "new") + msg("assistant", "newer"))
    cache = MessageLogCache(verify=False)    # verify mode reads the whole log to compare, which is its job
    repo.read_bytes = 0
    view = await cache.view_log(log_of(repo))
    assert texts(view) == ["SUM", "new", "newer"]
    assert view.start_offset == len(repo.files[f"{SLOT}/{segment_name(0)}"]) and view.compaction.tokens_after == 50
    assert repo.read_bytes == sum(len(repo.files[f"{SLOT}/{segment_name(n)}"]) for n in (1, 2)), "segment 0 was not read for the history"


async def test_the_history_equals_the_single_file_with_a_rewind_in_a_later_segment():
    parts = [
        msg("user", "one") + rec(1, "user_input", {"text": "one"}) + msg("assistant", "two"),
        rec(2, "compaction_marker", {"summary": "SUM", "kept_tail_messages": []}) + rec(3, "user_input") + msg("user", "three"),
        rec(4, "rewind_marker", {"to_seq": 3}) + msg("assistant", "four"),
    ]
    repo = Repo()
    for n, part in enumerate(parts):
        put(repo, n, part)
    view = await MessageLogCache().view_log(log_of(repo))
    want = reconstruct_compacted_history("".join(parts).splitlines())
    assert [m.model_dump(mode="json") for m in view.history] == [m.model_dump(mode="json") for m in want]


async def test_the_highest_seq_is_found_when_the_active_segment_has_only_messages():
    repo = Repo()
    put(repo, 0, rec(41, "user_input") + msg("user", "a"))
    put(repo, 1, msg("assistant", "b"))
    view = await MessageLogCache().view_log(log_of(repo))
    assert view.max_seq == 41


async def test_a_partial_last_line_is_answered_for_but_not_consumed():
    repo, cache = Repo(), MessageLogCache(verify=True)
    put(repo, 0, msg("user", "a"))
    put(repo, 1, msg("assistant", "b")[:-1])             # no newline yet: the line is still growing
    assert texts(await cache.view_log(log_of(repo))) == ["a", "b"]
    put(repo, 1, "\n", append=True)
    assert texts(await cache.view_log(log_of(repo))) == ["a", "b"]
    put(repo, 1, msg("user", "c"), append=True)
    assert texts(await cache.view_log(log_of(repo))) == ["a", "b", "c"]


async def test_a_log_that_changed_before_the_offset_is_parsed_afresh():
    repo, cache = Repo(), MessageLogCache(verify=True)
    put(repo, 0, msg("user", "a") + msg("assistant", "b"))
    await cache.view_log(log_of(repo))
    put(repo, 0, msg("user", "X") + msg("assistant", "Y") + msg("user", "z"))   # rewritten, longer
    assert texts(await cache.view_log(log_of(repo))) == ["X", "Y", "z"]
