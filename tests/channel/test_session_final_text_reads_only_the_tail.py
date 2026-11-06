"""``read_session_final_text`` parses only the last window of ``messages.jsonl``, and gets the same answer as parsing all of it.

A relay-every-turn session read the whole file and ``json.loads``-ed EVERY line after every turn, although
Ordinary turns need only the records from the second-to-last terminal on. A graph-final turn needs its whole current invocation,
because independent End results may precede later node terminals. The reader walks backward to the applicable boundary and a
ranged workspace widens its tail only until that boundary is present.
"""

from __future__ import annotations

import json
import random
from types import SimpleNamespace

import pytest

from primer.channel import session_relay
from primer.channel.session_relay import derive_session_final_text, read_session_final_text

SID = "sess-1"


class _BytesIO:
    state_path = ".state"

    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    async def read_file(self, path: str) -> bytes:
        assert path == f".state/sessions/{SID}/messages.jsonl"
        return self.raw


class _StrIO(_BytesIO):
    async def read_file(self, path: str):  # type: ignore[override]
        return self.raw.decode("utf-8")


class _LinesIO:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def read_lines(self, session_id: str) -> list[str]:
        return list(self.lines)


class _RangedIO:
    state_path = ".state"

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.tail_sizes: list[int] = []
        self.full_reads = 0

    async def file_info(self, path: str):
        assert path == f".state/sessions/{SID}/messages.jsonl"
        return SimpleNamespace(size_bytes=len(self.raw))

    async def read_file(self, path: str) -> bytes:
        assert path == f".state/sessions/{SID}/messages.jsonl"
        self.full_reads += 1
        return self.raw

    async def read_file_tail(self, path: str, max_bytes: int) -> tuple[bytes, bool]:
        assert path == f".state/sessions/{SID}/messages.jsonl"
        self.tail_sizes.append(max_bytes)
        return self.raw[-max_bytes:], len(self.raw) <= max_bytes


def _tok(text: str, **payload) -> dict:
    return {"kind": "assistant_token", "payload": {"text": text, **payload}}


def _done(stop_reason: str = "stop") -> dict:
    return {"kind": "done", "payload": {"stop_reason": stop_reason}}


def _end(text: str, end_node_id: str) -> dict:
    return _tok(text, end_node_id=end_node_id)


def _graph_done() -> dict:
    return {"kind": "done", "payload": {"stop_reason": "stop", "graph_end": True}}


USER = {"kind": "user_input", "payload": {"text": "hi"}}


def _line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False)


def _random_log(rng: random.Random) -> list[str]:
    """Lines of a random session log: records of every kind the window logic looks at, plus noise."""
    lines: list[str] = []
    for _ in range(rng.randint(0, 40)):
        roll = rng.random()
        if roll < 0.30:
            lines.append(_line(_tok(rng.choice(["a", "bb", "ccc", " d", ""]))))
        elif roll < 0.38:
            lines.append(_line(_tok("end", end_node_id="end-1", **({"nested": True} if rng.random() < 0.4 else {}))))
        elif roll < 0.55:
            lines.append(_line(_done(rng.choice(["stop", "tool_use", "error", "max_tokens", "end_turn"]))))
        elif roll < 0.62:
            lines.append(_line({"kind": "cancelled", "payload": {"reason": "operator_interrupt"}}))
        elif roll < 0.68:
            lines.append(_line({"kind": "error", "payload": {"message": "boom"}}))
        elif roll < 0.85:
            lines.append(_line(rng.choice([USER, {"kind": "tool_result", "payload": {"text": "x" * 5}}])))
        elif roll < 0.92:
            lines.append(rng.choice(["{not json", "", "   ", "[1, 2]", "42"]))
        else:
            lines.append(_line({"kind": "compaction_marker", "payload": {}}))
    return lines


def _whole_file_answer(lines: list[str]) -> str | None:
    """The reference: parse every line, then derive (what the reader did before)."""
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, TypeError):
            continue
    return derive_session_final_text([r for r in records if isinstance(r, dict)])


def _surface(kind: str, lines: list[str], rng: random.Random):
    if kind == "lines":
        return _LinesIO(lines)
    terminator = rng.choice(["\n", "\r\n"])
    raw = terminator.join(lines) + rng.choice(["", terminator])
    return (_BytesIO if kind == "bytes" else _StrIO)(raw.encode("utf-8"))


@pytest.mark.parametrize("surface", ["bytes", "str", "lines"])
async def test_the_answer_is_the_one_parsing_the_whole_file_gives(surface):
    rng = random.Random(20261006)
    answers = set()
    for _ in range(400):
        lines = _random_log(rng)
        got = await read_session_final_text(_surface(surface, lines, rng), SID)
        want = _whole_file_answer(lines)
        assert got == want, f"{lines!r}: tail gave {got!r}, the whole file gives {want!r}"
        answers.add(want)
    assert None in answers and len(answers) > 5, "the generator must exercise relayed text and no text alike"


async def _count_parses(monkeypatch, io) -> tuple[str | None, int]:
    calls = 0
    real_loads = json.loads

    def counting_loads(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(session_relay.json, "loads", counting_loads)
    text = await read_session_final_text(io, SID)
    return text, calls


def _history(turns: int) -> list[str]:
    lines: list[str] = []
    for i in range(turns):
        lines += [_line(USER), _line(_tok(f"answer {i}")), _line(_done())]
    return lines + [_line(USER), _line(_tok("the last answer")), _line(_done())]


@pytest.mark.parametrize("surface", ["bytes", "lines"])
async def test_the_number_of_lines_parsed_does_not_grow_with_the_history(monkeypatch, surface):
    rng = random.Random(1)
    small = await _count_parses(monkeypatch, _surface(surface, _history(10), rng))
    large = await _count_parses(monkeypatch, _surface(surface, _history(5000), rng))
    assert small[0] == large[0] == "the last answer"
    assert small[1] == large[1] <= 6, f"parsed {small[1]} lines of a short history and {large[1]} of a long one"


async def test_a_file_with_one_terminal_record_is_parsed_whole():
    lines = [_line(USER), _line(_tok("only ")), _line(_tok("answer")), _line(_done())]
    assert await read_session_final_text(_surface("bytes", lines, random.Random(2)), SID) == "only answer"


async def test_a_graph_read_reaches_its_input_past_later_node_terminals_and_malformed_lines():
    lines = [
        _line(USER),
        _line(_end("OLD", "old-end")),
        _line(_graph_done()),
        _line({"kind": "invocation_divider", "payload": {"invocation": 2}}),
        _line(USER),
        _line(_end("A", "e1")),
        "{malformed",
        _line(_tok("node one")),
        _line(_done()),
        _line(_tok("node two")),
        _line(_done()),
        _line(_tok("node three")),
        _line(_done()),
        _line(_end("B", "e2")),
        _line(_graph_done()),
    ]

    assert await read_session_final_text(_LinesIO(lines), SID) == "A\n\nB"


async def test_a_ranged_graph_read_widens_past_64k_until_the_invocation_boundary():
    lines = [
        _line(USER),
        _line(_end("OLD", "old-end")),
        _line(_graph_done()),
        _line({"kind": "tool_result", "payload": {"text": "h" * 1_200_000}}),
        _line(USER),
        _line(_end("A", "e1")),
        "{malformed",
        _line(_tok("x" * 300_000)),
        _line(_done()),
        _line(_tok("node two")),
        _line(_done()),
        _line(_tok("node three")),
        _line(_done()),
        _line(_end("B", "e2")),
        _line(_graph_done()),
    ]
    raw = ("\n".join(lines) + "\n").encode()
    io = _RangedIO(raw)

    assert raw.find(_line(_end("A", "e1")).encode()) < len(raw) - 64 * 1024
    assert await read_session_final_text(io, SID) == "A\n\nB"
    assert io.tail_sizes == [64 * 1024, 256 * 1024, 1024 * 1024]
    assert io.full_reads == 0, "the widening read must stop at the current invocation, not read the old history"


async def test_a_graph_with_unfinished_ordinary_text_after_its_last_node_still_relays_nothing():
    lines = [
        _line(USER),
        _line(_end("A", "e1")),
        _line(_tok("completed node")),
        _line(_done()),
        _line(_end("B", "e2")),
        _line(_tok("still streaming")),
        _line(_graph_done()),
    ]

    assert await read_session_final_text(_LinesIO(lines), SID) is None


async def test_a_record_whose_text_holds_a_unicode_line_separator_is_not_cut_in_two():
    """``str.splitlines`` also breaks at U+2028, U+0085 and the form feed, and ``model_dump_json`` writes them unescaped: the
    record was cut in the middle, did not parse, and its text was dropped from the relayed answer."""
    text = "first second\x85third\x0cfourth"
    lines = [_line(USER), _line(_tok(text)), _line(_done())]
    assert await read_session_final_text(_surface("bytes", lines, random.Random(3)), SID) == text


async def test_a_missing_file_and_a_failing_read_still_relay_nothing():
    from primer.model.except_ import NotFoundError

    class _Missing:
        async def read_file(self, path):
            raise NotFoundError("no messages.jsonl yet")

    class _Broken:
        async def read_file(self, path):
            raise OSError("the runtime connection dropped")

    assert await read_session_final_text(_Missing(), SID) is None
    assert await read_session_final_text(_Broken(), SID) is None
