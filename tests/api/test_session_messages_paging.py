"""Paged session message reads, raw and folded (S1 P4 Task 25).

tail-first paging was already shipped; this pins it and adds the
visible filter. The two answers are deliberately both available: the
audit and trace views need every line ever written, while a transcript
wants what the conversation currently shows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest


class _FakeWorkspace:
    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def write(self, path: str, content: str) -> None:
        self._files[path] = content.encode("utf-8")

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(f"{path!r} not found")
        return self._files[path]


def _rec(seq: int, kind: str, **payload) -> str:
    return json.dumps({"seq": seq, "kind": kind, "payload": payload,
                       "created_at": "2026-08-17T00:00:00+00:00"})


async def _seed(app, sid: str, lines: list[str]):
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        WorkspaceSession(
            id=sid, workspace_id="ws-1",
            binding=AgentSessionBinding(agent_id="ag1"),
            status=SessionStatus.WAITING, created_at=datetime.now(UTC),
            turn_status="idle",
        )
    )
    ws = _FakeWorkspace()
    ws.write(f".state/sessions/{sid}/messages.jsonl", "\n".join(lines) + "\n")

    async def _get(wid):
        return ws if wid == "ws-1" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]
    return ws


_REWOUND = [
    _rec(1, "user_input", text="first"),
    _rec(2, "done"),
    _rec(3, "user_input", text="second"),
    _rec(4, "done"),
    _rec(5, "rewind_marker", to_seq=2),
]

_COMPACTED = [
    _rec(1, "user_input", text="old"),
    _rec(2, "done"),
    _rec(3, "compaction_marker", summary="folded", replaced_to_seq=2),
    _rec(4, "user_input", text="new"),
]


@pytest.mark.asyncio
async def test_default_returns_every_line_including_rewound(client, app):
    """The audit view must still see what was cut."""
    await _seed(app, "p-1", _REWOUND)
    r = await client.get("/v1/sessions/p-1/messages")
    assert r.status_code == 200, r.text
    seqs = [it["seq"] for it in r.json()["items"]]
    assert seqs == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_visible_drops_rewound_rows(client, app):
    await _seed(app, "p-2", _REWOUND)
    r = await client.get("/v1/sessions/p-2/messages?visible=true")
    assert r.status_code == 200, r.text
    seqs = [it["seq"] for it in r.json()["items"]]
    assert seqs == [1, 2]


@pytest.mark.asyncio
async def test_visible_collapses_a_compacted_span_to_its_marker(client, app):
    await _seed(app, "p-3", _COMPACTED)
    r = await client.get("/v1/sessions/p-3/messages?visible=true")
    assert r.status_code == 200, r.text
    seqs = [it["seq"] for it in r.json()["items"]]
    assert seqs == [3, 4]


@pytest.mark.asyncio
async def test_visible_total_counts_the_folded_set(client, app):
    """Paging must describe what the caller asked to see, so the fold
    happens before the window is cut."""
    await _seed(app, "p-4", _REWOUND)
    raw = await client.get("/v1/sessions/p-4/messages")
    folded = await client.get("/v1/sessions/p-4/messages?visible=true")
    assert raw.json()["total"] == 5
    assert folded.json()["total"] == 2


@pytest.mark.asyncio
async def test_tail_paging_is_anchored_to_the_end(client, app):
    """Pre-existing behaviour, pinned before it was built upon."""
    await _seed(app, "p-5", [_rec(n, "assistant_token") for n in range(1, 8)])
    r = await client.get("/v1/sessions/p-5/messages?tail=true&limit=3")
    assert [it["seq"] for it in r.json()["items"]] == [5, 6, 7]

    older = await client.get(
        "/v1/sessions/p-5/messages?tail=true&limit=3&offset=3"
    )
    assert [it["seq"] for it in older.json()["items"]] == [2, 3, 4]


@pytest.mark.asyncio
async def test_visible_and_tail_compose(client, app):
    await _seed(app, "p-6", _REWOUND)
    r = await client.get(
        "/v1/sessions/p-6/messages?visible=true&tail=true&limit=1"
    )
    assert [it["seq"] for it in r.json()["items"]] == [2]
