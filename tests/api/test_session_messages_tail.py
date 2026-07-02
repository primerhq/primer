"""`_read_workspace_turn_log` tail-paging (#3/#7).

The session-messages endpoint gained a ``tail`` flag so the console can load a
long transcript newest-page-first (most-recent ``limit`` rows) and page older
rows lazily, instead of pulling the whole ``messages.jsonl`` at once. These
unit-test the reader's windowing directly against a fake workspace IO handle —
no app fixtures needed.
"""

from __future__ import annotations

import json

from primer.api.routers.sessions import _read_workspace_turn_log


class _FakeWorkspace:
    """Minimal workspace stub exposing the async ``read_file`` the reader uses."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def read_file(self, _relative_path: str) -> bytes:
        return self._raw


def _log(n: int) -> bytes:
    """A messages.jsonl with rows seq=1..n (ascending, as written on disk)."""
    lines = [json.dumps({"seq": i, "payload": {"text": f"m{i}"}}) for i in range(1, n + 1)]
    return ("\n".join(lines)).encode("utf-8")


async def test_tail_returns_most_recent_page_ascending() -> None:
    res = await _read_workspace_turn_log(
        workspace=_FakeWorkspace(_log(10)),
        relative_path="x",
        limit=3,
        offset=0,
        since_seq=None,
        tail=True,
    )
    assert res["total"] == 10
    assert [it["seq"] for it in res["items"]] == [8, 9, 10]


async def test_tail_offset_pages_older() -> None:
    # offset counts rows from the end: the next-older page after the tail.
    res = await _read_workspace_turn_log(
        workspace=_FakeWorkspace(_log(10)),
        relative_path="x",
        limit=3,
        offset=3,
        since_seq=None,
        tail=True,
    )
    assert [it["seq"] for it in res["items"]] == [5, 6, 7]


async def test_non_tail_default_is_unchanged_oldest_first() -> None:
    # Default (no tail) still returns the oldest window from offset 0.
    res = await _read_workspace_turn_log(
        workspace=_FakeWorkspace(_log(10)),
        relative_path="x",
        limit=3,
        offset=0,
        since_seq=None,
    )
    assert [it["seq"] for it in res["items"]] == [1, 2, 3]


async def test_tail_clamps_when_offset_exceeds_total() -> None:
    res = await _read_workspace_turn_log(
        workspace=_FakeWorkspace(_log(5)),
        relative_path="x",
        limit=3,
        offset=10,
        since_seq=None,
        tail=True,
    )
    assert res["items"] == []
    assert res["total"] == 5


async def test_tail_limit_larger_than_total_returns_all() -> None:
    res = await _read_workspace_turn_log(
        workspace=_FakeWorkspace(_log(4)),
        relative_path="x",
        limit=100,
        offset=0,
        since_seq=None,
        tail=True,
    )
    assert [it["seq"] for it in res["items"]] == [1, 2, 3, 4]
