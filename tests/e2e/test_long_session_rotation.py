"""Journey: a long session's log rotates into segments and every way of reading it agrees (segmented session log, design sections 5 and 7).

The e2e server runs with ``worker.session_log_segment_bytes`` small (``scripts/e2e/bringup.sh``), so a session of big answers crosses the threshold several times. A real
server over HTTP and the real OpenChatLLM client against a scripted mock that answers each turn with ~24 KB of text.

1. Ten turns are driven, the first by the session's own instructions and the rest by steers; two steers are sent back to back, around a rotation.
2. The workspace holds more than one ``messages*.jsonl`` segment (the raw file view of the state directory, an admin's).
3. The messages route returns every record once and in seq order; each steer and each answer appears exactly once; the SSE tap saw the same records, none twice.
4. The session's usage and a turn's timeline are the same before and after the rotations as a single-file log would give: one turn per input, one model call per turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import make_local_workspace, make_scripted_agent, start_agent_session

TURNS = 10
_FILLER = " ".join(["segment"] * 4000)          # ~28 KB: with the log's own records a turn is ~60 KB, past a 64 KiB threshold every turn or two
_ANSWER_WAIT_S = 45.0


def _answer(n: int) -> str:
    return f"ANSWER-{n:02d} {_FILLER}"


async def _follow_tap(client: httpx.AsyncClient, workspace_id: str, frames: list[dict]) -> None:
    async with client.stream("GET", f"/v1/workspaces/{workspace_id}/tap", timeout=None) as response:
        assert response.status_code == 200, response.status_code
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                with contextlib.suppress(ValueError):
                    frames.append(json.loads(line[6:]))


async def _records(client: httpx.AsyncClient, session_id: str) -> list[dict]:
    items: list[dict] = []
    offset = 0
    while True:
        page = (await client.get(f"/v1/sessions/{session_id}/messages", params={"limit": 500, "offset": offset})).json()["items"]
        items += page
        if len(page) < 500:
            return items
        offset += 500


async def _wait_for_answer(client: httpx.AsyncClient, session_id: str, n: int) -> None:
    marker = f"ANSWER-{n:02d}"
    for _ in range(int(_ANSWER_WAIT_S / 0.5)):
        if any(marker in str(r.get("payload", {}).get("text", "")) for r in await _records(client, session_id) if r.get("kind") == "assistant_token"):
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"session {session_id} never gave {marker}")


async def _segments(client: httpx.AsyncClient, workspace_id: str, session_id: str) -> list[str]:
    listing = (await client.get(f"/v1/workspaces/{workspace_id}/files", params={"path": f".state/sessions/{session_id}"})).json()
    return sorted(str(item["path"]).rsplit("/", 1)[-1] for item in listing["items"] if str(item["path"]).rsplit("/", 1)[-1].startswith("messages"))


async def _steer(client: httpx.AsyncClient, workspace_id: str, session_id: str, text: str) -> None:
    resp = await client.post(f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer", json={"instruction": text})
    assert resp.status_code in (200, 201, 202), resp.text


@pytest.mark.asyncio
async def test_a_long_session_rotates_and_every_reader_agrees(client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path) -> None:
    registry, base_url = mock_llm
    scenario = f"scripted:long-session-{unique_suffix}"
    agent = await make_scripted_agent(
        client, registry, base_url, suffix=unique_suffix, scenario=scenario,
        rules=[Rule(when_last_user_contains=f"turn-{n:02d}", emit_text=_answer(n)) for n in range(TURNS)],
    )
    workspace_id = await make_local_workspace(client, suffix=unique_suffix, root=tmp_path)
    frames: list[dict] = []
    follower = asyncio.create_task(_follow_tap(client, workspace_id, frames))
    try:
        await asyncio.sleep(1.5)      # the live tap holds its headers until the first frame; subscribe before the session writes anything
        session_id = await start_agent_session(
            client, workspace_id=workspace_id, agent_id=agent["agent_id"], instructions="turn-00 begin",
        )
        await _wait_for_answer(client, session_id, 0)
        before = (await client.get(f"/v1/sessions/{session_id}/turns/0/timeline")).json()

        # turns 1 to 7 one at a time, then 8 and 9 back to back (the second steer lands while the first turn's records are being written)
        for n in range(1, 8):
            await _steer(client, workspace_id, session_id, f"turn-{n:02d} please")
            await _wait_for_answer(client, session_id, n)
        await _steer(client, workspace_id, session_id, "turn-08 please")
        await _steer(client, workspace_id, session_id, "turn-09 please")
        await _wait_for_answer(client, session_id, 9)
        await _wait_for_answer(client, session_id, 8)

        names = await _segments(client, workspace_id, session_id)
        assert "messages.jsonl" in names and len(names) >= 3, f"the log never rotated: {names}"

        records = await _records(client, session_id)
        seqs = [r["seq"] for r in records if isinstance(r.get("seq"), int)]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), "every record once, in seq order, across the segments"
        inputs = [r["payload"]["text"] for r in records if r["kind"] == "user_input"]
        assert sorted(inputs) == sorted(["turn-00 begin", *(f"turn-{n:02d} please" for n in range(1, TURNS))]), f"each steer exactly once: {inputs}"
        answers = [r for r in records if r["kind"] == "assistant_token"]
        for n in range(TURNS):
            assert sum(f"ANSWER-{n:02d}" in str(r["payload"]["text"]) for r in answers) == 1, f"answer {n} not exactly once"

        # the live tap saw the same records as the messages route, none twice
        await asyncio.sleep(2.0)
        tapped = [f["seq"] for f in frames if f.get("session_id") == session_id and isinstance(f.get("seq"), int)]
        assert len(tapped) == len(set(tapped)), "the tap repeated a record across a rotation"
        assert set(tapped) == set(seqs), f"the tap and the messages route disagree: only in tap {sorted(set(tapped) - set(seqs))}, only in route {sorted(set(seqs) - set(tapped))}"

        # usage: one turn per input; the first turn's timeline is the same after the rotations as before them
        row = (await client.get(f"/v1/sessions/{session_id}")).json()
        assert row["usage"]["turns"] == TURNS, row["usage"]
        assert row["usage"]["model_calls"] == TURNS, row["usage"]
        after = (await client.get(f"/v1/sessions/{session_id}/turns/0/timeline")).json()
        assert after == before, "turn 0's timeline changed when the log rotated"
    finally:
        follower.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await follower
