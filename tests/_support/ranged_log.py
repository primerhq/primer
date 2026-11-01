"""A messages.jsonl builder and counting workspace doubles for the stage-3 (reads by range) tests."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from primer.model.except_ import NotFoundError

T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


def rec(seq: int, kind: str, payload: dict | None = None, **extra) -> str:
    return json.dumps({"seq": seq, "kind": kind, "payload": payload or {}, "created_at": T0.isoformat(), **extra})


def message(role: str, text: str) -> str:
    return json.dumps({"role": role, "parts": [{"type": "text", "text": text}]})


def turn(seq: int, n: int, *, pad: int = 0, usage=(10, 5), open_turn: bool = False) -> tuple[list[str], int]:
    """One turn's lines starting at ``seq`` (user input, a message line, tokens, done); ``open_turn`` leaves it unanswered."""
    lines = [rec(seq, "user_input", {"text": f"question {n}"}), message("user", f"question {n}")]
    seq += 1
    if open_turn:
        return lines, seq
    lines.append(rec(seq, "assistant_token", {"text": f"answer {n} " + "x" * pad}))
    seq += 1
    lines.append(message("assistant", f"answer {n}"))
    lines.append(rec(seq, "done", {"stop_reason": "stop", "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}))
    return lines, seq + 1


def log(turns: int, *, pad: int = 0, open_last: bool = False, start_seq: int = 1) -> tuple[list[str], int]:
    lines: list[str] = []
    seq = start_seq
    for n in range(turns):
        got, seq = turn(seq, n, pad=pad, open_turn=open_last and n == turns - 1)
        lines.extend(got)
    return lines, seq


def blob(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode() if lines else b""


class Ws:
    """A workspace double that serves one file and counts the bytes it hands out. ``ranged=False`` offers ``read_file`` only."""

    state_path = ".state"

    def __init__(self, data: bytes | None, *, ranged: bool = True, mtime: int = 1):
        self.data, self.mtime, self.bytes_read, self.reads = data, mtime, 0, 0
        if not ranged:
            self.read_file_tail = None
            self.read_file_from = None

    def _need(self) -> bytes:
        if self.data is None:
            raise NotFoundError("messages.jsonl")
        return self.data

    async def read_file(self, path: str) -> bytes:
        data = self._need()
        self.bytes_read += len(data)
        self.reads += 1
        return data

    async def read_file_from(self, path: str, offset: int) -> bytes:
        data = self._need()[offset:]
        self.bytes_read += len(data)
        self.reads += 1
        return data

    async def read_file_tail(self, path: str, max_bytes: int):
        data = self._need()
        tail = data[-max_bytes:]
        self.bytes_read += len(tail)
        self.reads += 1
        return tail, len(tail) == len(data)

    async def file_info(self, path: str):
        return SimpleNamespace(size_bytes=len(self._need()), modified_at=self.mtime)
