"""PERF-T14 stage 3: the readers that look at the end of messages.jsonl read the end, and answer exactly as the whole-file read does."""
from __future__ import annotations

import pytest

from primer.channel.session_relay import read_session_final_text
from primer.session.turns import count_turn_state
from tests._support.ranged_log import Ws, blob, log, message, rec, turn

SID = "s1"


def _cases() -> dict[str, list[str]]:
    big_last, seq = log(3)
    got, _ = turn(seq, 3, pad=300_000)                      # the last window alone is bigger than the first tail read
    big_last += got
    graph, seq = log(2)
    graph += [rec(seq, "done", {"graph_end": True, "stop_reason": "stop"})]
    return {
        "empty": [],
        "one_turn": log(1)[0],
        "few_turns": log(5)[0],
        "many_turns": log(1500, pad=200)[0],
        "open_last_turn": log(40, open_last=True)[0],
        "big_last_window": big_last,
        "graph_end_last": graph,
        "garbage_lines": [*log(30)[0], "{not json", "123", message("user", "x")],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(_cases()))
async def test_final_text_is_the_same_by_range_as_by_whole_file(name):
    data = blob(_cases()[name])
    ranged, whole = Ws(data), Ws(data, ranged=False)
    assert await read_session_final_text(ranged, SID) == await read_session_final_text(whole, SID)


@pytest.mark.asyncio
async def test_final_text_of_a_missing_log_is_none():
    assert await read_session_final_text(Ws(None), SID) is None
    assert await read_session_final_text(Ws(None, ranged=False), SID) is None


@pytest.mark.asyncio
async def test_final_text_reads_the_tail_of_a_2mb_log():
    data = blob(log(6000, pad=100)[0])
    assert len(data) > 2_000_000
    ws = Ws(data)
    assert await read_session_final_text(ws, SID) is not None
    assert ws.bytes_read <= 64 * 1024, ws.bytes_read


# --- the open-turn window (count_turn_state / has_open_turn from the drain cursor) -----------------------------------------------------------

class _Shim:
    """The worker IO shim's read surface over one log."""

    def __init__(self, data: bytes | None, *, ranged: bool = True):
        self.ws = Ws(data)
        if not ranged:
            self.read_state_tail = None

    async def read_state_file(self, workspace_id, rel):
        return await self.ws.read_file(rel) if self.ws.data is not None else b""

    async def read_state_tail(self, workspace_id, rel, max_bytes):
        return await self.ws.read_file_tail(rel, max_bytes) if self.ws.data is not None else (b"", True)


def _row(cursor: int):
    from types import SimpleNamespace
    return SimpleNamespace(id=SID, workspace_id="w", next_unprocessed_seq=cursor)


def _window_cases() -> dict[str, tuple[list[str], list[int]]]:
    closed, end = log(3000, pad=50)
    opened, end2 = log(3000, pad=50, open_last=True)
    return {
        "closed": (closed, [0, 1, 500, end - 40, end - 4, end]),
        "open_last": (opened, [0, 1, 500, end2 - 40, end2 - 2, end2 - 1, end2]),
        "short": (log(4)[0], [0, 1, 3, 100]),
        "empty": ([], [0, 5]),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(_window_cases()))
async def test_open_turn_state_from_the_window_equals_the_whole_log(name):
    from primer.session.dispatch import _read_message_lines

    lines, cursors = _window_cases()[name]
    data = blob(lines)
    for cursor in cursors:
        ranged = await _read_message_lines(_Shim(data), _row(cursor))
        whole = await _read_message_lines(_Shim(data, ranged=False), _row(cursor))
        assert count_turn_state(ranged, cursor=cursor) == count_turn_state(whole, cursor=cursor), (name, cursor)


@pytest.mark.asyncio
async def test_the_open_turn_check_reads_the_tail_of_a_2mb_log():
    from primer.session.dispatch import _read_message_lines

    lines, end = log(6000, pad=100, open_last=True)
    data = blob(lines)
    assert len(data) > 2_000_000
    shim = _Shim(data)
    got = await _read_message_lines(shim, _row(end - 1))
    assert count_turn_state(got, cursor=end - 1).open_turns == 1
    assert shim.ws.bytes_read <= 64 * 1024, shim.ws.bytes_read


# --- usage: the sidecar fold -----------------------------------------------------------------------------------------------------------------

def _usage_logs() -> dict[str, list[str]]:
    base, seq = log(6)
    with_compaction = [*base, rec(seq, "compaction_marker", {"summary": "s"}), *log(3, start_seq=seq + 1)[0]]
    with_rewind = [*base, rec(seq, "rewind_marker", {"to_seq": 7}), *log(2, start_seq=seq + 1)[0]]
    failed = [rec(1, "user_input", {"text": "q"}), rec(2, "error", {"message": "boom"}), rec(3, "error", {"message": "boom"}),
              rec(4, "done", {"graph_end": True, "stop_reason": "error"})]
    return {"plain": base, "compaction": with_compaction, "rewind": with_rewind, "failed": failed, "empty": [], "garbage": [*base, "{x", "7"]}


@pytest.mark.parametrize("name", list(_usage_logs()))
def test_usage_fold_in_pieces_equals_session_usage(name):
    from primer.session.usage import UsageFold, session_usage

    lines = _usage_logs()[name]
    for cut in range(0, len(lines) + 1, 3):
        fold = UsageFold()
        ok = fold.feed(lines[:cut]) and fold.feed(lines[cut:])
        if name == "rewind" and any('"rewind_marker"' in l for l in lines):
            assert not ok                                   # a rewind cannot be continued: the caller folds the whole log
            continue
        assert ok and fold.usage() == session_usage(lines), (name, cut)


@pytest.mark.asyncio
async def test_fold_usage_follows_a_growing_log_and_reads_only_what_was_added():
    from primer.session.usage import session_usage
    from primer.session.usage_cache import UsageCache, fold_usage

    cache, lines, seq = UsageCache(), log(6000, pad=100)[0], 6000 * 3 + 1
    ws = Ws(blob(lines))
    assert len(ws.data) > 2_000_000
    first = await fold_usage(ws, "p", cache, "k")
    assert first == session_usage(lines)
    for n in range(3):
        ws.bytes_read = 0
        got, seq = turn(seq, 9000 + n)
        lines += got
        ws.data = blob(lines)
        assert await fold_usage(ws, "p", cache, "k") == session_usage(lines)
        assert ws.bytes_read < 2048, ws.bytes_read


@pytest.mark.asyncio
async def test_fold_usage_counts_a_partial_last_line_without_consuming_it():
    from primer.session.usage import session_usage
    from primer.session.usage_cache import UsageCache, fold_usage

    lines = log(4)[0]
    done = rec(99, "done", {"stop_reason": "stop", "usage": {"input_tokens": 1, "output_tokens": 1}})
    cache, ws = UsageCache(), Ws(blob(lines) + done.encode())             # the last record has no newline yet
    assert await fold_usage(ws, "p", cache, "k") == session_usage([*lines, done])
    ws.data = blob([*lines, done])                                          # now it is complete: counted once
    assert await fold_usage(ws, "p", cache, "k") == session_usage([*lines, done])


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["rewrite", "shrink", "rewind", "compaction"])
async def test_fold_usage_falls_back_to_the_whole_log_when_the_log_is_not_a_pure_append(how):
    from primer.session.usage import session_usage
    from primer.session.usage_cache import UsageCache, fold_usage

    lines, seq = log(8)
    cache, ws = UsageCache(), Ws(blob(lines))
    await fold_usage(ws, "p", cache, "k")
    if how == "rewrite":
        lines = [l.replace("answer", "ANSWER") for l in lines] + turn(seq, 50)[0]
    elif how == "shrink":
        lines = lines[:10]
    elif how == "rewind":
        lines = [*lines, rec(seq, "rewind_marker", {"to_seq": 9}), *turn(seq + 1, 51)[0]]
    else:
        lines = [*lines, rec(seq, "compaction_marker", {"summary": "s"}), *turn(seq + 1, 52)[0]]
    ws.data = blob(lines)
    assert await fold_usage(ws, "p", cache, "k") == session_usage(lines)
    ws.data = blob([*lines, *turn(seq + 10, 53)[0]])
    assert await fold_usage(ws, "p", cache, "k") == session_usage([*lines, *turn(seq + 10, 53)[0]])


@pytest.mark.asyncio
async def test_fold_usage_on_a_workspace_without_ranges_reads_the_whole_log_each_time():
    from primer.session.usage import session_usage
    from primer.session.usage_cache import UsageCache, fold_usage

    lines = log(5)[0]
    ws = Ws(blob(lines), ranged=False)
    cache = UsageCache()
    assert await fold_usage(ws, "p", cache, "k") == session_usage(lines)
    assert await fold_usage(ws, "p", cache, "k") == session_usage(lines)
