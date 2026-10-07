"""Journey: End on a turn that is blocked in a slow model call reaches every client, over the workspace tap
(console review 2026-10-08, C-011).

The Cancel route flags a RUNNING session's row and signals the pool, which HARD-cancels the turn task. A model call that is
still streaming never gives dispatch's cooperative cancel check a look, so for a slow turn this is how a Cancel always arrives.
The hard cancel used to end the row with no CANCELLED record, tick or terminal event: a client following the tap never heard the
turn had ended and showed "running: thinking" until a reload (and the transcript never gained its "cancelled" marker).

A real server over HTTP, the real OpenChatLLM client against a slow scripted mock. It fails on the old code (the row ends, the
tap and the log never gain a ``cancelled`` record).
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import make_local_workspace, make_scripted_agent, start_agent_session

_FRAME_WAIT_S = 20.0
_LONG_ANSWER = ("A deliberately long answer, so the model call is still streaming when the Cancel arrives. " * 12).strip()


async def _follow_tap(client: httpx.AsyncClient, workspace_id: str, frames: list[dict]) -> None:
    async with client.stream("GET", f"/v1/workspaces/{workspace_id}/tap", timeout=None) as response:
        assert response.status_code == 200, response.status_code
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                with contextlib.suppress(ValueError):
                    frames.append(json.loads(line[6:]))


async def _wait_for_frame(frames: list[dict], session_id: str, frame_class: str) -> dict | None:
    try:
        async with asyncio.timeout(_FRAME_WAIT_S):
            while True:
                for frame in frames:
                    if frame.get("session_id") == session_id and frame.get("class") == frame_class:
                        return frame
                await asyncio.sleep(0.1)
    except TimeoutError:
        return None


@pytest.mark.asyncio
async def test_ending_a_session_whose_model_call_is_streaming_tells_the_tap_and_the_log(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    registry, base_url = mock_llm
    scenario = f"scripted:slow-cancel-{unique_suffix}"
    agent = await make_scripted_agent(
        client, registry, base_url, suffix=unique_suffix, scenario=scenario,
        rules=[Rule(emit_text=_LONG_ANSWER, chunk_delay_s=0.4, text_chunk_words=3)],
    )
    workspace_id = await make_local_workspace(client, suffix=unique_suffix, root=tmp_path)

    frames: list[dict] = []
    follower = asyncio.create_task(_follow_tap(client, workspace_id, frames))
    try:
        # The live server holds the response headers until the first frame, so there is no "connected" to await; the route
        # subscribes while it handles the request and a tick published before that is dropped.
        await asyncio.sleep(1.5)
        assert not follower.done(), follower.exception() if not follower.cancelled() else "cancelled"
        session_id = await start_agent_session(
            client, workspace_id=workspace_id, agent_id=agent["agent_id"], instructions="go on at length",
        )

        streaming = await _wait_for_frame(frames, session_id, "text_delta")
        assert streaming is not None, "the model call never started streaming"
        ended = await client.post(f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel")
        assert ended.status_code == 200, ended.text

        cancelled = await _wait_for_frame(frames, session_id, "cancelled")
        assert cancelled is not None, (
            f"the tap never heard the session end; it delivered "
            f"{[(f.get('class'), f.get('seq')) for f in frames if f.get('session_id') == session_id and f.get('seq') is not None]}"
        )
        assert cancelled["payload"]["reason"] == "operator_cancel", cancelled

        row = (await client.get(f"/v1/sessions/{session_id}")).json()
        assert row["status"] == "ended" and row["ended_reason"] == "cancelled", row
        records = (await client.get(f"/v1/sessions/{session_id}/messages")).json()["items"]
        kinds = [r["kind"] for r in records]
        assert kinds[-1] == "cancelled", f"the transcript must end in the cancelled marker, got {kinds}"
    finally:
        follower.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await follower
